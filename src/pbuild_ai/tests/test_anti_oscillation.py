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

from pbuild_ai.llm_client import LlmAnalyzer
from pbuild_ai.workspace import RpmSourceManager


def _make_tool_call(name, arguments):
    """Build a tool_calls entry as returned by AI chat API."""
    return {
        "function": {
            "name": name,
            "arguments": arguments,
        }
    }


def _make_response(tool_calls=None, content="", thinking=None):
    """Build a mock AI /api/chat response."""
    msg = {"content": content}
    if thinking is not None:
        msg["thinking"] = thinking
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
        self.ai = LlmAnalyzer(model="test-model")
        self.ai.manager = self.manager
        self.ai._chat_supported = True

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

        with patch.object(self.ai, '_request', side_effect=mock_request):
            results = self.ai.call_with_tools(
                self.messages, self.tools, self.manager,
                workspace_dir=self.tmpdir, max_rounds=max_rounds,
            )
        return results, call_count[0]

    def test_tool_call_recovered_from_thinking_field(self):
        """A tool call the model finalized inside message.thinking (with empty
        content) is extracted and executed instead of being dropped."""
        thinking = ('I should bump the version line. '
                    '{"name": "edit_file", "arguments": {"path": "testpkg.spec", '
                    '"old_string": "Version: 1.0", "new_string": "Version: 2.0"}}')
        responses = [
            _make_response(content="", thinking=thinking),
            _make_response(content="Done."),
        ]
        results, n_calls = self._run_call_with_tools(responses, max_rounds=3)

        self.assertTrue(results, "expected the extracted tool call to execute")
        self.assertIn("Version: 2.0", self.spec_path.read_text())
        self.assertEqual(n_calls, 2, "round 2 should just terminate the loop")

    def test_tool_call_recovered_from_content_text(self):
        """A tool call emitted as JSON text in message.content (no native
        tool_calls) is extracted, round-tripped and executed."""
        content = ('{"name": "edit_file", "arguments": {"path": "testpkg.spec", '
                   '"old_string": "Version: 1.0", "new_string": "Version: 3.0"}}')
        responses = [
            _make_response(content=content),
            _make_response(content="Done."),
        ]
        results, n_calls = self._run_call_with_tools(responses, max_rounds=3)

        self.assertTrue(results, "expected the extracted tool call to execute")
        self.assertIn("Version: 3.0", self.spec_path.read_text())
        self.assertEqual(n_calls, 2, "round 2 should just terminate the loop")

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

    # -- degenerate .spec write guard --

    def test_write_file_degenerate_spec_skipped(self):
        """write_file with truncated/nonsense .spec content (no Name: tag) is skipped."""
        responses = [
            [_make_tool_call("write_file", {"path": self.spec_name, "content": "..."})],
        ]
        results, n_calls = self._run_call_with_tools(responses)

        self.assertTrue(any("SKIP" in r and "invalid .spec" in r for r in results))
        # File on disk must be untouched
        self.assertEqual(self.spec_path.read_text(), self.initial_content)

    def test_write_file_spec_missing_name_tag_skipped(self):
        """A .spec write without a Name: tag is skipped even if it has sections."""
        content = "%description\nBroken.\n\n%files\n%{_bindir}/x\n"
        responses = [
            [_make_tool_call("write_file", {"path": self.spec_name, "content": content})],
        ]
        results, n_calls = self._run_call_with_tools(responses)

        self.assertTrue(any("SKIP" in r and "invalid .spec" in r for r in results))
        self.assertEqual(self.spec_path.read_text(), self.initial_content)

    def test_write_file_valid_spec_not_skipped(self):
        """A valid .spec write (has Name: tag) still proceeds normally."""
        content = "Name: testpkg\nVersion: 9.0\n\n%description\nNew.\n"
        responses = [
            [_make_tool_call("write_file", {"path": self.spec_name, "content": content})],
        ]
        results, n_calls = self._run_call_with_tools(responses)

        self.assertFalse(any("invalid .spec" in r for r in results))
        self.assertTrue(any("OK: Wrote" in r for r in results))
        self.assertEqual(self.spec_path.read_text(), content)

    # -- state shared across fix attempts --

    def _attempt_with_shared_state(self, responses, shared_versions, shared_blocked):
        """Run one call_with_tools invocation with the given shared state,
        falling back to a terminal 'Done.' response once the list is exhausted."""
        def mock_request(url, payload):
            if not responses:
                return _make_response(content="Done.")
            return _make_response(tool_calls=responses.pop(0))
        with patch.object(self.ai, '_request', side_effect=mock_request):
            return self.ai.call_with_tools(
                self.messages, self.tools, self.manager,
                workspace_dir=self.tmpdir, max_rounds=3,
                file_versions=shared_versions, blocked_files=shared_blocked,
            )

    def test_write_file_revert_to_initial_across_attempts_blocked(self):
        """When version state is shared across call_with_tools invocations (one
        call per fix attempt), re-writing the ORIGINAL content on a later attempt
        is blocked just like an in-loop revert."""
        import hashlib
        shared_versions = {self.spec_name: [hashlib.md5(self.initial_content.encode()).hexdigest()]}
        shared_blocked = set()
        content_b = "Name: testpkg\nVersion: 2.0\n\n%description\nV2.\n"

        # Attempt 1: write content_b (fresh call, shared state seeded with the
        # original hash — exactly what run_fix_loop does across attempts)
        res1 = self._attempt_with_shared_state(
            [[_make_tool_call("write_file", {"path": self.spec_name, "content": content_b})]],
            shared_versions, shared_blocked,
        )
        self.assertTrue(any("OK: Wrote" in r for r in res1))

        # Attempt 2 (separate call, shared state): write back the original content
        res2 = self._attempt_with_shared_state(
            [[_make_tool_call("write_file", {"path": self.spec_name, "content": self.initial_content})]],
            shared_versions, shared_blocked,
        )
        # The original-content write must be blocked as a revert across attempts
        self.assertTrue(any("SKIP" in r and "reverts" in r for r in res2))
        self.assertEqual(self.spec_path.read_text(), content_b)

    def test_edit_file_revert_to_initial_across_attempts_blocked(self):
        """edit_file reverting to original content is blocked when the state is
        shared across call_with_tools invocations."""
        import hashlib
        shared_versions = {self.spec_name: [hashlib.md5(self.initial_content.encode()).hexdigest()]}
        shared_blocked = set()

        # Attempt 1: bump Version 1.0 -> 2.0
        res1 = self._attempt_with_shared_state(
            [[_make_tool_call("edit_file", {
                "path": self.spec_name, "old_string": "Version: 1.0", "new_string": "Version: 2.0",
            })]],
            shared_versions, shared_blocked,
        )
        self.assertTrue(any("OK: Edited" in r for r in res1))

        # Attempt 2: revert Version 2.0 -> 1.0 (original) — must be blocked
        res2 = self._attempt_with_shared_state(
            [[_make_tool_call("edit_file", {
                "path": self.spec_name, "old_string": "Version: 2.0", "new_string": "Version: 1.0",
            })]],
            shared_versions, shared_blocked,
        )
        self.assertTrue(any("SKIP" in r and "reverts" in r for r in res2))

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

    # -- edit_file with non-matching old_string is skipped --

    def test_edit_file_non_matching_old_string_skipped(self):
        """edit_file where old_string doesn't match is skipped before execution,
        with the missing match reported back to the model."""
        responses = [
            [_make_tool_call("edit_file", {
                "path": self.spec_name,
                "old_string": "NonExistentString",
                "new_string": "Something",
            })],
        ]
        results, n_calls = self._run_call_with_tools(responses)

        self.assertTrue(any("SKIP" in r and "old_string not found" in r for r in results))
        # The edit must NOT have been applied to the file
        self.assertNotIn("Something", self.spec_path.read_text())

    # -- edit_file with ambiguous old_string is skipped --

    def test_edit_file_ambiguous_old_string_skipped(self):
        """edit_file where old_string matches multiple locations is skipped so the
        first occurrence is not silently replaced."""
        responses = [
            [_make_tool_call("edit_file", {
                "path": self.spec_name,
                "old_string": "testpkg",
                "new_string": "wibble",
            })],
        ]
        results, n_calls = self._run_call_with_tools(responses)

        self.assertTrue(any("SKIP" in r and "found " in r and "times" in r for r in results))
        # Ambiguous edit must not be applied (Name: and %files lines untouched)
        self.assertIn("Name: testpkg", self.spec_path.read_text())
        self.assertNotIn("wibble", self.spec_path.read_text())

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

    # -- one-nudge round on text-only replies --

    def _run_with_nudge_tools(self, response_sequence, max_rounds=15):
        """Run call_with_tools with a non-empty tools list so nudging may trigger."""
        self.tools = [{
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "edit a file",
                "parameters": {"type": "object"},
            },
        }]
        return self._run_call_with_tools(response_sequence, max_rounds=max_rounds)

    def test_text_only_round1_nudged_then_tool_call(self):
        """A text-only round 1 gets one corrective nudge, then tool calls proceed."""
        nudge_before = len(self.messages)
        responses = [
            _make_response(content="I'll fix that for you."),
            [_make_tool_call("edit_file", {
                "path": self.spec_name,
                "old_string": "Version: 1.0",
                "new_string": "Version: 2.0",
            })],
        ]
        results, n_calls = self._run_with_nudge_tools(responses)

        # rounds: 1=text, 2=tool call, 3=loop-termination "Done." text
        self.assertEqual(n_calls, 3)
        self.assertTrue(any("OK" in r for r in results))
        nudge_messages = [m for m in self.messages[nudge_before:]
                          if m.get("role") == "user" and "tool call" in m.get("content", "")]
        self.assertEqual(len(nudge_messages), 1)

    def test_terminal_reply_not_nudged(self):
        """already-at-version text replies are terminal and must not be nudged."""
        nudge_before = len(self.messages)
        responses = [_make_response(content="already-at-version")]
        results, n_calls = self._run_with_nudge_tools(responses)

        self.assertEqual(results, [])
        self.assertEqual(n_calls, 1)
        self.assertEqual(len(self.messages), nudge_before)

    def test_abort_reply_not_nudged(self):
        """[ABORT: reason] text replies are terminal and must not be nudged."""
        nudge_before = len(self.messages)
        responses = [_make_response(content="[ABORT: cannot run update_references.sh]")]
        results, n_calls = self._run_with_nudge_tools(responses)

        self.assertEqual(results, [])
        self.assertEqual(n_calls, 1)
        self.assertEqual(len(self.messages), nudge_before)

    def test_no_nudge_without_tools(self):
        """Without any tools declared there is nothing to nudge towards."""
        nudge_before = len(self.messages)
        responses = [_make_response(content="Just talking.")]
        results, n_calls = self._run_call_with_tools(responses)

        self.assertEqual(results, [])
        self.assertEqual(n_calls, 1)
        self.assertEqual(len(self.messages), nudge_before)

    def test_two_text_rounds_stop_without_infinite_nudge(self):
        """Only one nudge is sent; a second text reply returns without retry."""
        nudge_before = len(self.messages)
        responses = [
            _make_response(content="Round one prose."),
            _make_response(content="Round two prose still."),
        ]
        results, n_calls = self._run_with_nudge_tools(responses)

        self.assertEqual(results, [])
        self.assertEqual(n_calls, 2)
        nudge_messages = [m for m in self.messages[nudge_before:]
                          if m.get("role") == "user" and "tool call" in m.get("content", "")]
        self.assertEqual(len(nudge_messages), 1)


class TestDuplicateChangelogGuard(unittest.TestCase):
    """The .changes anti-oscillation guard must block edits that would leave
    two or more '- Updated to version <ver>' entries for the same version,
    without touching ordinary (non-changes) files."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="pbuild_dup_chg_")
        self.changes_name = "testpkg.changes"
        self.changes_path = Path(self.tmpdir) / self.changes_name

        self.manager = RpmSourceManager(self.tmpdir)
        self.ai = LlmAnalyzer(model="test-model")
        self.ai.manager = self.manager
        self.ai._chat_supported = True
        self.tools = []
        self.messages = [
            {"role": "system", "content": "You are a test assistant."},
            {"role": "user", "content": "Update the changelog."},
        ]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _entry(version, author="Test Author <t@example.com>"):
        return ("-------------------------------------------------------------------\n"
                "Fri Sep 11 09:00:00 UTC 2026 - " + author + "\n"
                "\n"
                f"- Updated to version {version}\n"
                "\n")

    def _run_call_with_tools(self, response_sequence, max_rounds=15):
        call_count = [0]

        def mock_request(url, payload):
            idx = call_count[0]
            call_count[0] += 1
            if idx >= len(response_sequence):
                return _make_response(content="Done.")
            resp = response_sequence[idx]
            if isinstance(resp, list):
                return _make_response(tool_calls=resp)
            return resp

        with patch.object(self.ai, '_request', side_effect=mock_request):
            results = self.ai.call_with_tools(
                self.messages, self.tools, self.manager,
                workspace_dir=self.tmpdir, max_rounds=max_rounds,
            )
        return results

    def test_edit_file_creating_duplicate_changelog_entry_blocked(self):
        """edit_file that would leave two entries for the same version is blocked."""
        self.changes_path.write_text(self._entry("2.0"))
        responses = [
            # Prepend the exact same entry block again -> duplicate version
            [_make_tool_call("edit_file", {
                "path": self.changes_name,
                "old_string": self._entry("2.0"),
                "new_string": self._entry("2.0") + self._entry("2.0"),
            })],
        ]
        results = self._run_call_with_tools(responses)
        self.assertTrue(any("duplicate changelog entries" in r for r in results))
        self.assertIn("blocked", " ".join(results))
        # File must be untouched on disk
        self.assertEqual(self.changes_path.read_text(), self._entry("2.0"))

    def test_edit_file_creating_duplicate_then_blocked_further_edits(self):
        """After a duplicate attempt the file is blocked for the whole call."""
        self.changes_path.write_text(self._entry("2.0"))
        dup_new = self._entry("2.0") + self._entry("2.0")
        responses = [
            # Round 1: duplicate prepend (blocked, blocks the file)
            [_make_tool_call("edit_file", {
                "path": self.changes_name,
                "old_string": self._entry("2.0"),
                "new_string": dup_new,
            })],
            # Round 2: a legit single-entry attempt (must still be blocked)
            [_make_tool_call("edit_file", {
                "path": self.changes_name,
                "old_string": self._entry("2.0"),
                "new_string": self._entry("2.0"),
            })],
        ]
        results = self._run_call_with_tools(responses)
        self.assertTrue(any("duplicate changelog entries" in r for r in results))
        self.assertTrue(any("blocked" in r and "No further edits" in r for r in results))
        self.assertEqual(self.changes_path.read_text(), self._entry("2.0"))

    def test_write_file_creating_duplicate_changelog_entry_blocked(self):
        """write_file that provides two entries for the same version is blocked."""
        self.changes_path.write_text(self._entry("1.0"))
        responses = [
            [_make_tool_call("write_file", {
                "path": self.changes_name,
                "content": self._entry("2.0") + self._entry("2.0"),
            })],
        ]
        results = self._run_call_with_tools(responses)
        self.assertTrue(any("duplicate changelog entries" in r for r in results))
        self.assertEqual(self.changes_path.read_text(), self._entry("1.0"))

    def test_single_changelog_entry_not_blocked(self):
        """A write_file with exactly one entry per version proceeds normally."""
        self.changes_path.write_text(self._entry("1.0"))
        responses = [
            [_make_tool_call("write_file", {
                "path": self.changes_name,
                "content": self._entry("2.0"),
            })],
        ]
        results = self._run_call_with_tools(responses)
        self.assertFalse(any("duplicate changelog entries" in r for r in results))
        self.assertEqual(self.changes_path.read_text(), self._entry("2.0"))

    def test_distinct_versions_not_blocked(self):
        """Two different version entries ('1.0' and '2.0') are perfectly valid."""
        self.changes_path.write_text(self._entry("1.0"))
        responses = [
            [_make_tool_call("write_file", {
                "path": self.changes_name,
                "content": self._entry("2.0") + self._entry("1.0"),
            })],
        ]
        results = self._run_call_with_tools(responses)
        self.assertFalse(any("duplicate changelog entries" in r for r in results))
        self.assertEqual(self.changes_path.read_text(),
                         self._entry("2.0") + self._entry("1.0"))

    def test_spec_file_with_repeated_version_line_not_affected(self):
        """The guard is .changes-specific: repeated Version: lines in a .spec
        must not be treated as duplicated changelog entries."""
        spec_name = "testpkg.spec"
        spec_path = Path(self.tmpdir) / spec_name
        spec_path.write_text("Name: testpkg\nVersion: 1.0\nVersion: 1.0\n")
        responses = [
            [_make_tool_call("write_file", {
                "path": spec_name,
                "content": "Name: testpkg\nVersion: 2.0\nVersion: 1.0\n",
            })],
        ]
        results = self._run_call_with_tools(responses)
        self.assertFalse(any("duplicate changelog entries" in r for r in results))
        self.assertEqual(spec_path.read_text(),
                         "Name: testpkg\nVersion: 2.0\nVersion: 1.0\n")


if __name__ == '__main__':
    unittest.main()
