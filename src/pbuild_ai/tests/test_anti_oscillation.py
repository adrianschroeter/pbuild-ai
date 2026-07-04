"""Tests for anti-oscillation logic in call_with_tools: revert detection, no-op skipping, file blocking."""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Mock yaml before any pbuild_ai import
_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules['yaml'] = _yaml

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.ollama_client import OllamaAnalyzer
from pbuild_ai.workspace import RpmSourceManager


def _make_tool_call(name, arguments):
    """Build a tool_calls entry as returned by Ollama chat API."""
    return {
        "function": {
            "name": name,
            "arguments": arguments,
        }
    }


def _make_response(tool_calls=None, content=""):
    """Build a mock Ollama /api/chat response."""
    msg = {"content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {"message": msg}


class TestAntiOscillation(unittest.TestCase):
    """Test the anti-oscillation logic in call_with_tools."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="pbuild_anti_osc_")
        self.spec_name = "testpkg.spec"
        self.spec_path = Path(self.tmpdir) / self.spec_name
        self.initial_content = "Name: testpkg\nVersion: 1.0\n\n%description\nTest.\n\n%files\n%{_bindir}/testpkg\n"
        self.spec_path.write_text(self.initial_content)

        self.manager = RpmSourceManager(self.tmpdir)
        self.ollama = OllamaAnalyzer(model="test-model")
        self.ollama.manager = self.manager
        self.ollama._chat_supported = True

        # Minimal tools list (only what we test)
        self.tools = []
        # Minimal messages list
        self.messages = [
            {"role": "system", "content": "You are a test assistant."},
            {"role": "user", "content": "Fix the build."},
        ]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_call_with_tools(self, response_sequence, max_rounds=15):
        """Run call_with_tools with a sequence of mock _request responses.

        Each response is either a dict (returned as-is) or a list of tool_calls.
        The mock _request returns responses[0], responses[1], ... for each round.
        """
        call_count = [0]

        def mock_request(url, payload):
            idx = call_count[0]
            call_count[0] += 1
            if idx >= len(response_sequence):
                # Return a response with no tool calls to stop the loop
                return _make_response(content="Done.")
            resp = response_sequence[idx]
            if isinstance(resp, list):
                return _make_response(tool_calls=resp)
            return resp

        with patch.object(self.ollama, '_request', side_effect=mock_request):
            results = self.ollama.call_with_tools(
                self.messages, self.tools, self.manager,
                workspace_dir=self.tmpdir, max_rounds=max_rounds,
            )
        return results, call_count[0]

    # -- write_file revert detection --

    def test_write_file_revert_to_initial_blocked(self):
        """write_file that reverts to the initial spec content is blocked."""
        content_b = "Name: testpkg\nVersion: 1.0\n\n%description\nChanged.\n\n%files\n%{_bindir}/testpkg\n"
        responses = [
            # Round 1: write content B (different from initial)
            [_make_tool_call("write_file", {"path": self.spec_name, "content": content_b})],
            # Round 2: write back to initial content (revert)
            [_make_tool_call("write_file", {"path": self.spec_name, "content": self.initial_content})],
            # Round 3: try another edit (should be blocked)
            [_make_tool_call("write_file", {"path": self.spec_name, "content": content_b})],
        ]
        results, n_calls = self._run_call_with_tools(responses)

        # Round 1 should have succeeded
        self.assertTrue(any("OK: Wrote" in r for r in results))
        # Round 2 should have been skipped (revert to initial)
        self.assertTrue(any("SKIP" in r and "reverts" in r for r in results))
        # Round 3 should have been skipped (file blocked)
        self.assertTrue(any("SKIP" in r and "blocked" in r for r in results))
        # File on disk should still have content_b (from round 1)
        self.assertEqual(self.spec_path.read_text(), content_b)

    def test_write_file_revert_between_edits_blocked(self):
        """write_file that reverts to a previous edit version is blocked."""
        content_b = "Name: testpkg\nVersion: 2.0\n\n%description\nV2.\n"
        content_c = "Name: testpkg\nVersion: 3.0\n\n%description\nV3.\n"
        responses = [
            # Round 1: write content B
            [_make_tool_call("write_file", {"path": self.spec_name, "content": content_b})],
            # Round 2: write content C
            [_make_tool_call("write_file", {"path": self.spec_name, "content": content_c})],
            # Round 3: write back to content B (revert to round 1 version)
            [_make_tool_call("write_file", {"path": self.spec_name, "content": content_b})],
        ]
        results, n_calls = self._run_call_with_tools(responses)

        self.assertTrue(any("OK: Wrote" in r for r in results))
        self.assertTrue(any("SKIP" in r and "reverts" in r for r in results))
        # File should still have content_c (from round 2)
        self.assertEqual(self.spec_path.read_text(), content_c)

    def test_write_file_noop_skipped(self):
        """write_file with identical content to current disk is skipped as no-op."""
        responses = [
            [_make_tool_call("write_file", {"path": self.spec_name, "content": self.initial_content})],
        ]
        results, n_calls = self._run_call_with_tools(responses)

        self.assertTrue(any("File unchanged" in r for r in results))
        self.assertNotIn("OK: Wrote", " ".join(results))

    # -- edit_file revert detection --

    def test_edit_file_revert_blocked(self):
        """edit_file that would revert to a previous version is blocked."""
        responses = [
            # Round 1: edit Version from 1.0 to 2.0
            [_make_tool_call("edit_file", {
                "path": self.spec_name,
                "old_string": "Version: 1.0",
                "new_string": "Version: 2.0",
            })],
            # Round 2: edit Version back from 2.0 to 1.0 (revert)
            [_make_tool_call("edit_file", {
                "path": self.spec_name,
                "old_string": "Version: 2.0",
                "new_string": "Version: 1.0",
            })],
        ]
        results, n_calls = self._run_call_with_tools(responses)

        self.assertTrue(any("OK: Edited" in r for r in results))
        self.assertTrue(any("SKIP" in r and "reverts" in r for r in results))
        # File should still have Version: 2.0
        self.assertIn("Version: 2.0", self.spec_path.read_text())

    def test_edit_file_revert_to_initial_blocked(self):
        """edit_file that would revert to the initial spec content is blocked."""
        responses = [
            # Round 1: edit Version from 1.0 to 2.0
            [_make_tool_call("edit_file", {
                "path": self.spec_name,
                "old_string": "Version: 1.0",
                "new_string": "Version: 2.0",
            })],
            # Round 2: edit Version back from 2.0 to 1.0 (revert to initial)
            [_make_tool_call("edit_file", {
                "path": self.spec_name,
                "old_string": "Version: 2.0",
                "new_string": "Version: 1.0",
            })],
        ]
        results, n_calls = self._run_call_with_tools(responses)
        self.assertTrue(any("SKIP" in r and "reverts" in r for r in results))

    # -- blocked file propagation --

    def test_blocked_file_blocks_future_edits(self):
        """Once a file is blocked, all future write_file and edit_file calls are skipped."""
        content_b = "Name: testpkg\nVersion: 2.0\n\n%description\nV2.\n"
        responses = [
            # Round 1: write content B (success)
            [_make_tool_call("write_file", {"path": self.spec_name, "content": content_b})],
            # Round 2: revert to initial (file gets blocked)
            [_make_tool_call("write_file", {"path": self.spec_name, "content": self.initial_content})],
            # Round 3: try edit_file (should be blocked)
            [_make_tool_call("edit_file", {
                "path": self.spec_name,
                "old_string": "Version: 2.0",
                "new_string": "Version: 3.0",
            })],
        ]
        results, n_calls = self._run_call_with_tools(responses)

        self.assertTrue(any("OK: Wrote" in r for r in results))
        self.assertTrue(any("reverts" in r for r in results))
        self.assertTrue(any("blocked" in r for r in results))

    # -- loop termination --

    def test_loop_terminates_when_all_skipped(self):
        """If all tool calls in a round are skipped, the loop breaks."""
        content_b = "Name: testpkg\nVersion: 2.0\n\n%description\nV2.\n"
        responses = [
            # Round 1: successful write
            [_make_tool_call("write_file", {"path": self.spec_name, "content": content_b})],
            # Round 2: revert (skipped, file blocked)
            [_make_tool_call("write_file", {"path": self.spec_name, "content": self.initial_content})],
            # Round 3: another attempt on blocked file (skipped)
            [_make_tool_call("write_file", {"path": self.spec_name, "content": content_b})],
            # Round 4: this should never be reached
            [_make_tool_call("write_file", {"path": self.spec_name, "content": "never"})],
        ]
        results, n_calls = self._run_call_with_tools(responses, max_rounds=10)

        # Should not have made 4 _request calls (loop should have terminated)
        self.assertLess(n_calls, 4)
        # "never" content should not appear in results
        self.assertNotIn("never", " ".join(results))

    # -- non-write tools not affected --

    def test_read_file_not_affected(self):
        """read_file calls should always pass through, even alongside a blocked write."""
        content_b = "Name: testpkg\nVersion: 2.0\n\n%description\nV2.\n"
        responses = [
            # Round 1: write content B (success)
            [_make_tool_call("write_file", {"path": self.spec_name, "content": content_b})],
            # Round 2: revert + read_file in same round (read should work, revert should be blocked)
            [_make_tool_call("write_file", {"path": self.spec_name, "content": self.initial_content}),
             _make_tool_call("read_file", {"path": self.spec_name})],
        ]
        results, n_calls = self._run_call_with_tools(responses)

        # read_file should have produced content
        self.assertTrue(any("read_file:" in r and "Version: 2.0" in r for r in results))

    # -- multiple files independent --

    def test_multiple_files_independent_blocking(self):
        """Blocking file A should not affect edits to file B in the same round."""
        spec_a = "testpkg.spec"
        spec_b = "other.spec"
        path_b = Path(self.tmpdir) / spec_b
        path_b.write_text("Name: other\nVersion: 1.0\n\n%description\nOther.\n")

        content_a_v2 = "Name: testpkg\nVersion: 2.0\n\n%description\nV2.\n"
        content_b_v2 = "Name: other\nVersion: 2.0\n\n%description\nV2.\n"

        responses = [
            # Round 1: edit spec_a (success)
            [_make_tool_call("write_file", {"path": spec_a, "content": content_a_v2})],
            # Round 2: revert spec_a (blocked) + edit spec_b (should work, not blocked)
            [_make_tool_call("write_file", {"path": spec_a, "content": self.initial_content}),
             _make_tool_call("write_file", {"path": spec_b, "content": content_b_v2})],
        ]
        results, n_calls = self._run_call_with_tools(responses)

        # spec_a revert should be blocked
        self.assertTrue(any("reverts" in r for r in results))
        # spec_b edit should succeed
        self.assertTrue(any("OK: Wrote" in r and "other.spec" in r for r in results))
        self.assertEqual(path_b.read_text(), content_b_v2)

    # -- edit_file with non-matching old_string passes through --

    def test_edit_file_non_matching_old_string_passes_through(self):
        """edit_file where old_string doesn't match should pass through to execute_tool_calls."""
        responses = [
            [_make_tool_call("edit_file", {
                "path": self.spec_name,
                "old_string": "NonExistentString",
                "new_string": "Something",
            })],
        ]
        results, n_calls = self._run_call_with_tools(responses)

        # Should get an error from execute_tool_calls, not a SKIP
        self.assertTrue(any("old_string not found" in r for r in results))
        self.assertFalse(any("SKIP" in r for r in results))

    # -- edit_file with missing old_string passes through --

    def test_edit_file_missing_old_string_passes_through(self):
        """edit_file without old_string should pass through to execute_tool_calls."""
        responses = [
            [_make_tool_call("edit_file", {
                "path": self.spec_name,
                "new_string": "Something",
            })],
        ]
        results, n_calls = self._run_call_with_tools(responses)

        # Should get an error from execute_tool_calls about missing old_string
        self.assertTrue(any("missing" in r and "old_string" in r for r in results))

    # -- mixed read and write in same round --

    def test_mixed_read_and_write_in_same_round(self):
        """A round with both read_file and write_file should execute both."""
        content_b = "Name: testpkg\nVersion: 2.0\n\n%description\nV2.\n"
        responses = [
            [_make_tool_call("read_file", {"path": self.spec_name}),
             _make_tool_call("write_file", {"path": self.spec_name, "content": content_b})],
        ]
        results, n_calls = self._run_call_with_tools(responses)

        self.assertTrue(any("read_file:" in r for r in results))
        self.assertTrue(any("OK: Wrote" in r for r in results))
        self.assertEqual(self.spec_path.read_text(), content_b)

    # -- successful edit recorded as version --

    def test_successful_edit_recorded_as_version(self):
        """After a successful edit, the new content hash is recorded for revert detection."""
        responses = [
            # Round 1: edit Version 1.0 -> 2.0
            [_make_tool_call("edit_file", {
                "path": self.spec_name,
                "old_string": "Version: 1.0",
                "new_string": "Version: 2.0",
            })],
            # Round 2: edit Summary (add a new line) - should succeed (different content)
            [_make_tool_call("edit_file", {
                "path": self.spec_name,
                "old_string": "Version: 2.0",
                "new_string": "Version: 3.0",
            })],
            # Round 3: edit back to Version 2.0 (revert to round 1 result)
            [_make_tool_call("edit_file", {
                "path": self.spec_name,
                "old_string": "Version: 3.0",
                "new_string": "Version: 2.0",
            })],
        ]
        results, n_calls = self._run_call_with_tools(responses)

        # First two edits should succeed
        edit_count = sum(1 for r in results if "OK: Edited" in r)
        self.assertEqual(edit_count, 2)
        # Third edit should be blocked (revert)
        self.assertTrue(any("SKIP" in r and "reverts" in r for r in results))


if __name__ == '__main__':
    unittest.main()
