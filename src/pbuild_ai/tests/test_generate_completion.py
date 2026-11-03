"""--generate must not report success without a spec file: the round budget is
announced, the AI is pushed to write the spec before it runs out, and a run that
ends without a .spec file fails."""

import io
import json
import shutil
import sys
import tarfile
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
from pbuild_ai.utils import ReadCoverageTracker
import pbuild_ai.generate_mode as gm

SPEC = "Name: gufo\nVersion: 1.0\n\n%description\nTest.\n"


def _tool_call(name, args):
    return {"id": "call-1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _tool_reply(name, args):
    return {"tool_calls": [_tool_call(name, args)]}


class TestGenerateCompletion(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.ctx = PbuildContext(workspace_dir=self.ws, generate_prompt="package gufo")
        self.ctx.ai = MagicMock()
        self.ctx.ai.model = "m"
        self.ctx.ai.count_tokens.return_value = 0
        self.ctx.ai.max_tokens = 1024
        self.ctx.manager = MagicMock()
        self.ctx.skill_manager = MagicMock()
        self.ctx.skill_manager.get_skill_by_name.return_value = None
        self.ctx.skill_manager.get_skills_for.return_value = []
        self.ctx.tools = []
        self.ctx.max_generate_rounds = 6
        self.sent = []

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _run(self, replies):
        """Run generate mode over the scripted AI replies, no real tools."""
        seq = list(replies)

        def fake_chat(ai, messages, tools, **kw):
            self.sent.append([dict(m) for m in messages])
            if not seq:
                return {"message": {"content": "done"}}
            return {"message": seq.pop(0)}

        def fake_tools(calls, manager, workspace_dir, *a, **kw):
            out = []
            for name, inp in calls:
                if name in ("write_file", "edit_file"):
                    Path(workspace_dir, inp["path"]).write_text(inp.get("content", ""))
                    out.append(f"OK: Wrote {inp['path']}")
                else:
                    out.append("OK")
            return out

        with patch.object(gm, "chat_completion", side_effect=fake_chat), \
                patch.object(gm, "execute_tool_calls", side_effect=fake_tools), \
                redirect_stdout(io.StringIO()) as out:
            ok = gm.run_generate_mode(self.ctx)
        return ok, out.getvalue()

    def _status_messages(self, index):
        return [m["content"] for m in self.sent[index]
                if m.get("role") == "user"
                and str(m.get("content") or "").startswith(gm._STATUS_TAG)]

    def test_no_spec_file_fails(self):
        """Exhausting the budget without a spec is an error, not a success."""
        ok, out = self._run([_tool_reply("list_files", {"path": "."})] * 6)
        self.assertFalse(ok)
        self.assertIn("Stopped after the round budget (of 6)", out)
        self.assertIn("no .spec file was created", out)

    def test_spec_file_succeeds(self):
        ok, out = self._run([_tool_reply("write_file", {"path": "gufo.spec", "content": SPEC}),
                             {"content": "Created gufo.spec"}])
        self.assertTrue(ok)
        self.assertIn("Created gufo.spec", out)
        self.assertNotIn("no .spec file was created", out)

    def test_abort_marker_fails(self):
        ok, out = self._run([{"content": "I cannot do this [ABORT: upstream is gone]"}])
        self.assertFalse(ok)
        self.assertIn("[ABORT] upstream is gone", out)

    def test_status_block_is_injected_every_round(self):
        self._run([_tool_reply("list_files", {"path": "."})] * 3)
        for index in range(3):
            status = self._status_messages(index)
            self.assertEqual(len(status), 1, f"round {index + 1}: {len(status)} status blocks")
            self.assertIn("NOT complete", status[0])

    def test_writing_is_demanded_once_half_the_budget_is_gone(self):
        self._run([_tool_reply("list_files", {"path": "."})] * 5)
        for index in (0, 1, 2):
            self.assertNotIn("ACTION REQUIRED", self._status_messages(index)[0])
        for index in (3, 4):
            self.assertIn("ACTION REQUIRED", self._status_messages(index)[0])

    def test_no_demand_once_a_spec_exists(self):
        (Path(self.ws) / "gufo.spec").write_text(SPEC)
        self._run([_tool_reply("list_files", {"path": "."})] * 6)
        for index in range(6):
            self.assertNotIn("ACTION REQUIRED", self._status_messages(index)[0])
            self.assertIn("A spec file exists: gufo.spec", self._status_messages(index)[0])

    def test_last_round_asks_for_the_spec_only(self):
        self._run([_tool_reply("list_files", {"path": "."})] * 6)
        self.assertIn("THIS IS THE LAST ROUND", self._status_messages(5)[0])


class TestBudgetDirective(unittest.TestCase):
    def test_quiet_while_there_is_time(self):
        self.assertIsNone(gm._budget_directive(0, 50, False))
        self.assertIsNone(gm._budget_directive(24, 50, False))

    def test_escalates(self):
        self.assertIn("Stop researching", gm._budget_directive(25, 50, False))
        self.assertIn("Do NOT read", gm._budget_directive(41, 50, False))
        self.assertIn("LAST ROUND", gm._budget_directive(49, 50, False))

    def test_silent_with_a_spec_or_without_a_budget(self):
        self.assertIsNone(gm._budget_directive(49, 50, True))
        self.assertIsNone(gm._budget_directive(49, 0, False))


class TestStatusBlock(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.tar = Path(self.ws) / "gufo-1.0.tar.gz"
        src = Path(self.ws) / "build" / "gufo-1.0"
        src.mkdir(parents=True)
        (src / "README.md").write_text("Gufo.\n")
        with tarfile.open(self.tar, "w:gz") as tar:
            tar.add(src, arcname="gufo-1.0")
        (Path(self.ws) / "notes.txt").write_text("x" * 2048)
        self.ctx = PbuildContext(workspace_dir=self.ws, generate_prompt="package gufo")
        self.ctx.manager = MagicMock()
        self.ctx.manager.read_file_safe.side_effect = lambda p: Path(p).read_text()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_lists_archive_with_its_directory(self):
        text = gm._status_block(self.ctx, ReadCoverageTracker(), [], None)
        self.assertIn("gufo-1.0.tar.gz", text)
        self.assertIn("extracts to 'gufo-1.0/'", text)
        self.assertIn("notes.txt (2 KB)", text)
        self.assertIn("NOT complete", text)

    def test_lists_fetched_and_read_sources(self):
        tracker = ReadCoverageTracker()
        calls = [("read_file", {"path": "notes.txt"}),
                 ("read_file_from_archive", {"archive_path": "gufo-1.0.tar.gz",
                                             "file_path": "gufo-1.0/README.md"})]
        tracker.update_from_results(calls, ["Gufo contents", "Gufo."], self.ws, self.ctx.manager)
        text = gm._status_block(self.ctx, tracker, ["https://example.org/x"], None)
        self.assertIn("Already fetched", text)
        self.assertIn("https://example.org/x", text)
        self.assertIn("notes.txt", text)
        self.assertIn("gufo-1.0.tar.gz!gufo-1.0/README.md", text)

    def test_partial_reads_are_not_listed(self):
        tracker = ReadCoverageTracker()
        calls = [("read_file", {"path": "notes.txt", "offset": 0, "limit": 10})]
        tracker.update_from_results(calls, ["x" * 10], self.ws, self.ctx.manager)
        self.assertEqual(tracker.fully_read(self.ws), [])


class TestInjectStatus(unittest.TestCase):
    def test_replaces_the_previous_block(self):
        messages = [{"role": "system", "content": "s"},
                    {"role": "user", "content": "the task"},
                    {"role": "user", "content": gm._STATUS_TAG + " old"}]
        gm._inject_status(messages, gm._STATUS_TAG + " new")
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[-1]["content"], gm._STATUS_TAG + " new")
        self.assertEqual(messages[1]["content"], "the task")


if __name__ == "__main__":
    unittest.main()
