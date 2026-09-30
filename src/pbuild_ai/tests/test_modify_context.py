"""--modify must not continue a --fix conversation, and a file the model
only saw truncated (or failed to edit) must stay readable."""

import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

if 'yaml' not in sys.modules:
    _yaml = types.ModuleType('yaml')
    _yaml.YAMLError = Exception
    sys.modules['yaml'] = _yaml

from pbuild_ai.context import PbuildContext
from pbuild_ai.utils import (ReadCoverageTracker, READ_RESULT_LIMIT, TOOL_RESULT_LIMIT,
                             truncate_tool_result)

SPEC = "Name: gufo\nVersion: 1.0\n\n%description\nTest.\n"


class TestTruncateToolResult(unittest.TestCase):
    def test_reads_get_more_room(self):
        text = "x" * 10000
        self.assertEqual(truncate_tool_result("read_file", text), text)
        self.assertLessEqual(len(truncate_tool_result("list_files", text)), TOOL_RESULT_LIMIT + 30)

    def test_long_read_is_cut(self):
        out = truncate_tool_result("read_file", "a" * (READ_RESULT_LIMIT * 2))
        self.assertIn("(truncated)", out)
        self.assertLessEqual(len(out), READ_RESULT_LIMIT + 30)


class TestReadTrackerRereads(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        (Path(self.ws) / "gufo.spec").write_text(SPEC)
        self.manager = MagicMock()
        self.manager.read_file_safe.side_effect = lambda p: Path(p).read_text()
        self.read = [("read_file", {"path": "gufo.spec"})]

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _skipped(self, tracker):
        return tracker.filter_reads(self.read, self.ws, self.manager)[1]

    def test_full_read_is_skipped_next_time(self):
        t = ReadCoverageTracker()
        t.update_from_results(self.read, [SPEC], self.ws, self.manager)
        self.assertTrue(self._skipped(t))

    def test_truncated_read_is_not_recorded(self):
        t = ReadCoverageTracker()
        t.update_from_results(self.read, ["x" * (READ_RESULT_LIMIT + 1)], self.ws, self.manager)
        self.assertFalse(self._skipped(t))

    def test_failed_edit_allows_reread(self):
        t = ReadCoverageTracker()
        t.update_from_results(self.read, [SPEC], self.ws, self.manager)
        t.update_from_results([("edit_file", {"path": "gufo.spec"})],
                              ["Error: edit_file: old_string not found in gufo.spec"],
                              self.ws, self.manager)
        self.assertFalse(self._skipped(t))


class TestModifySavedContext(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.spec = Path(self.ws) / "gufo.spec"
        self.spec.write_text(SPEC)
        self.ctx_file = Path(self.ws) / ".pai.context"

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _ctx(self, prompt="add a gufo user"):
        ctx = PbuildContext(workspace_dir=self.ws, modify_prompt=prompt)
        ctx.ai = MagicMock()
        ctx.ai.model = "m"
        ctx.ai.count_tokens.return_value = 0
        ctx.ai.max_tokens = 1024
        ctx.manager = MagicMock()
        ctx.manager.read_file_safe.return_value = SPEC
        ctx.skill_manager = MagicMock()
        ctx.skill_manager.get_skills_for.return_value = []
        ctx.tools = []
        ctx.spec_files = [self.spec]
        return ctx

    def _run(self, ctx):
        import pbuild_ai.modify_mode as mm
        sent = []

        def fake_chat(ai, messages, tools, **kw):
            sent.append([dict(m) for m in messages])
            return {"message": {"content": "nothing to do"}}

        with patch.object(mm, "chat_completion", side_effect=fake_chat), \
                redirect_stdout(io.StringIO()) as out:
            mm.run_modify_mode(ctx)
        return sent[0], out.getvalue()

    def _save(self, **data):
        base = {"version": 1, "spec_path": "gufo.spec",
                "messages": [{"role": "system", "content": "old"},
                             {"role": "user", "content": "BUILD LOG ANALYSIS"}]}
        base.update(data)
        self.ctx_file.write_text(json.dumps(base))

    def test_fix_context_is_ignored_and_kept(self):
        self._save(mode="fix")
        messages, out = self._run(self._ctx())
        self.assertNotIn("BUILD LOG ANALYSIS", json.dumps(messages))
        self.assertIn("Ignoring saved fix context", out)
        self.assertEqual(json.loads(self.ctx_file.read_text())["mode"], "fix")

    def test_modify_context_for_other_request_is_discarded(self):
        self._save(mode="modify", modify_prompt="something else")
        messages, _ = self._run(self._ctx())
        self.assertNotIn("BUILD LOG ANALYSIS", json.dumps(messages))

    def test_same_request_resumes(self):
        self._save(mode="modify", modify_prompt="add a gufo user")
        messages, _ = self._run(self._ctx())
        self.assertIn("BUILD LOG ANALYSIS", json.dumps(messages))


if __name__ == "__main__":
    unittest.main()
