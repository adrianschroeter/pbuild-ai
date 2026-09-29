"""Tests for forcing tool calls: the finish tool, tool_choice=required on
OpenAI-compatible servers and the grammar-constrained retry that replaces the
free-text nudge."""

import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# Mock yaml before any pbuild_ai import
_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules.setdefault('yaml', _yaml)

from pbuild_ai.llm_client import LlmAnalyzer, _finish_text, _tool_call_schema, _tool_signatures
from pbuild_ai.pbuild_ai import _ai_requested_abort
from pbuild_ai.tools import FINISH_TOOL_NAME
from pbuild_ai.workspace import RpmSourceManager

_EDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": "edit a file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "old_string": {"type": "string"},
                           "new_string": {"type": "string"}},
            "required": ["path", "old_string", "new_string"],
        },
    },
}


def _call(name, arguments):
    return {"function": {"name": name, "arguments": arguments}}


def _reply(content="", tool_calls=None):
    msg = {"content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {"message": msg}


_BUMP = {"path": "testpkg.spec", "old_string": "Version: 1.0", "new_string": "Version: 2.0"}


class TestHelpers(unittest.TestCase):

    def test_finish_text_markers(self):
        self.assertEqual(_finish_text({"status": "nothing-to-do", "summary": "x"}), "nothing-to-do")
        self.assertEqual(_finish_text({"status": "already-at-version"}), "already-at-version")
        self.assertEqual(_finish_text({"status": "done", "summary": "Bumped [REBUILD: foo]"}),
                         "Bumped [REBUILD: foo]")
        self.assertEqual(_finish_text({}), "done")
        self.assertEqual(_finish_text(None), "done")

    def test_finish_text_abort_is_parseable(self):
        text = _finish_text({"status": "abort", "summary": "script [x] missing\nsecond line"})
        self.assertEqual(_ai_requested_abort([{"role": "assistant", "content": text}]),
                         "script (x) missing second line")
        self.assertEqual(_finish_text({"status": "abort"}), "[ABORT: aborted by AI]")

    def test_tool_call_schema(self):
        schema = _tool_call_schema([_EDIT_TOOL, {"type": "function", "function": {"name": "finish"}}])
        names = [v["properties"]["name"]["const"] for v in schema["anyOf"]]
        self.assertEqual(names, ["edit_file", "finish"])
        self.assertEqual(schema["anyOf"][0]["properties"]["arguments"],
                         _EDIT_TOOL["function"]["parameters"])
        self.assertEqual(schema["anyOf"][1]["properties"]["arguments"], {"type": "object"})

    def test_tool_signatures(self):
        tool = {"function": {"name": "finish", "parameters": {
            "properties": {"status": {}, "summary": {}}, "required": ["status"]}}}
        self.assertEqual(_tool_signatures([tool]), "- finish(status, summary?)")


class _ToolLoopCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="pbuild_forced_")
        self.spec_path = Path(self.tmpdir) / "testpkg.spec"
        self.spec_path.write_text("Name: testpkg\nVersion: 1.0\n")
        self.manager = RpmSourceManager(self.tmpdir)
        self.ai = LlmAnalyzer(model="test-model")
        self.ai.manager = self.manager
        self.ai._chat_supported = True
        self.ai._openai_mode = False
        self.messages = [
            {"role": "system", "content": "You are a test assistant."},
            {"role": "user", "content": "Update the package."},
        ]
        self.payloads = []

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, responses, max_rounds=10):
        """Each response is a reply dict or an Exception to raise; payloads
        are recorded (deep-copied) in self.payloads."""
        def handler(url, payload):
            self.payloads.append(json.loads(json.dumps(payload)))
            idx = len(self.payloads) - 1
            resp = responses[idx] if idx < len(responses) else _reply("Done.")
            if isinstance(resp, Exception):
                raise resp
            return resp

        with patch.object(self.ai, '_request', side_effect=handler):
            return self.ai.call_with_tools(self.messages, [_EDIT_TOOL], self.manager,
                                           workspace_dir=self.tmpdir, max_rounds=max_rounds)


class TestFinishTool(_ToolLoopCase):

    def test_finish_tool_offered(self):
        self._run([_reply(tool_calls=[_call("finish", {"status": "done"})])])
        names = [t["function"]["name"] for t in self.payloads[0]["tools"]]
        self.assertEqual(names, ["edit_file", FINISH_TOOL_NAME])

    def test_finish_not_offered_with_force_tools_off(self):
        self.ai._force_tools = False
        self._run([_reply(tool_calls=[_call("edit_file", _BUMP)]), _reply("nothing-to-do")])
        names = [t["function"]["name"] for t in self.payloads[0]["tools"]]
        self.assertEqual(names, ["edit_file"])

    def test_finish_abort_is_reported(self):
        results = self._run([_reply(tool_calls=[_call("finish", {"status": "abort",
                                                                 "summary": "script missing"})])])
        self.assertEqual(results, [])
        self.assertEqual(len(self.payloads), 1)
        self.assertEqual(self.ai.last_text_response, "[ABORT: script missing]")
        self.assertEqual(_ai_requested_abort(self.messages), "script missing")

    def test_finish_after_edit_in_same_round(self):
        results = self._run([_reply(tool_calls=[_call("edit_file", _BUMP),
                                                _call("finish", {"status": "done", "summary": "bumped"})])])
        self.assertEqual(len(self.payloads), 1, "finish must end the loop without another round")
        self.assertIn("Version: 2.0", self.spec_path.read_text())
        self.assertTrue(any(r.startswith("edit_file: OK") for r in results))
        self.assertEqual(self.ai.last_text_response, "bumped")
        for m in self.messages:
            for tc in m.get("tool_calls") or []:
                self.assertNotEqual(tc["function"]["name"], FINISH_TOOL_NAME)

    def test_nothing_to_do_marker(self):
        self._run([_reply(tool_calls=[_call("finish", {"status": "nothing-to-do"})])])
        self.assertEqual(self.ai.last_text_response, "nothing-to-do")
        self.assertEqual(self.messages[-1], {"role": "assistant", "content": "nothing-to-do"})


class TestArgumentsRoundTrip(_ToolLoopCase):

    def test_text_extracted_call_round_trips_arguments_as_object(self):
        """Calls extracted from text carry JSON-string arguments; the next
        request must send them as an object or Ollama rejects it with HTTP 400."""
        content = json.dumps({"name": "edit_file", "arguments": _BUMP})
        self._run([_reply(content), _reply(tool_calls=[_call("finish", {"status": "done"})])])
        calls = [tc for m in self.payloads[1]["messages"] for tc in m.get("tool_calls") or []]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["arguments"], _BUMP)


class TestConstrainedRetry(_ToolLoopCase):

    def test_text_reply_gets_constrained_retry(self):
        forced = json.dumps({"name": "edit_file", "arguments": _BUMP})
        results = self._run([
            _reply("I will now bump the version."),
            _reply(forced),
            _reply(tool_calls=[_call("finish", {"status": "done"})]),
        ])
        self.assertIn("Version: 2.0", self.spec_path.read_text())
        self.assertTrue(any(r.startswith("edit_file: OK") for r in results))
        retry = self.payloads[1]
        self.assertNotIn("tools", retry)
        names = [v["properties"]["name"]["const"] for v in retry["format"]["anyOf"]]
        self.assertEqual(names, ["edit_file", FINISH_TOOL_NAME])
        self.assertIn("finish(status, summary?)", retry["messages"][-1]["content"])
        self.assertEqual(retry["messages"][-2], {"role": "assistant",
                                                 "content": "I will now bump the version."})
        self.assertEqual(len(self.payloads), 3)

    def test_constrained_retry_disables_thinking(self):
        self.ai._model_thinking_capable = True
        forced = json.dumps({"name": "finish", "arguments": {"status": "done"}})
        self._run([_reply("prose"), _reply(forced)])
        self.assertIs(self.payloads[0]["think"], True)
        self.assertIs(self.payloads[1]["think"], False)

    def test_terminal_text_not_retried(self):
        self._run([_reply("nothing-to-do")])
        self.assertEqual(len(self.payloads), 1)

    def test_retries_are_capped(self):
        results = self._run([_reply("prose")] * 10)
        self.assertEqual(results, [])
        # round 1 text + forced retry (text again) ends the call: no plain nudge
        self.assertEqual(len(self.payloads), 2)
        self.assertFalse(any("MUST now respond" in (m.get("content") or "") for m in self.messages))

    def test_failed_constrained_request_falls_back_to_nudge(self):
        results = self._run([
            _reply("prose"),
            RuntimeError("HTTP Error 400: Bad Request — invalid format"),
            _reply(tool_calls=[_call("edit_file", _BUMP)]),
            _reply(tool_calls=[_call("finish", {"status": "done"})]),
        ])
        self.assertTrue(any(r.startswith("edit_file: OK") for r in results))
        self.assertTrue(any("MUST now respond" in (m.get("content") or "") for m in self.messages))
        self.assertEqual(len(self.payloads), 4)


class TestServerRejections(_ToolLoopCase):

    def test_openai_mode_requires_tool_choice(self):
        self.ai._openai_mode = True
        self._run([_reply(tool_calls=[_call("finish", {"status": "done"})])])
        self.assertEqual(self.payloads[0]["tool_choice"], "required")

    def test_openai_constrained_retry_uses_response_format(self):
        self.ai._openai_mode = True
        forced = json.dumps({"name": "finish", "arguments": {"status": "done"}})
        self._run([_reply("prose"), _reply(forced)])
        self.assertEqual(self.payloads[1]["response_format"]["type"], "json_schema")
        self.assertNotIn("format", self.payloads[1])

    def test_rejected_tool_choice_dropped_and_remembered(self):
        self.ai._openai_mode = True
        self._run([
            RuntimeError("HTTP Error 400: Bad Request — unsupported value"),
            _reply(tool_calls=[_call("finish", {"status": "done"})]),
        ])
        self.assertIn("tool_choice", self.payloads[0])
        self.assertNotIn("tool_choice", self.payloads[1])
        self.assertFalse(self.ai._tool_choice_supported)

    def test_rejected_think_dropped_and_remembered(self):
        self.ai._model_thinking_capable = True
        self._run([
            RuntimeError('HTTP Error 400: Bad Request — {"error":"\\"m\\" does not support thinking"}'),
            _reply(tool_calls=[_call("finish", {"status": "done"})]),
        ])
        self.assertIs(self.payloads[0]["think"], True)
        self.assertNotIn("think", self.payloads[1])
        self.assertTrue(self.ai._think_unsupported)

    def test_other_http_errors_still_fatal(self):
        with self.assertRaises(SystemExit) as cm:
            self._run([RuntimeError("HTTP Error 404: Not Found — model not found")])
        self.assertEqual(cm.exception.code, 2)


class TestOpenAIRequestPassthrough(unittest.TestCase):

    def test_tool_choice_and_response_format_forwarded(self):
        ai = LlmAnalyzer(model="m")
        ai._openai_mode = True
        sent = []

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

        def fake_open(req, timeout=None):
            sent.append(json.loads(req.data.decode()))
            return _Resp()

        ai._opener = types.SimpleNamespace(open=fake_open)
        ai._request(ai.chat_api_url, {"model": "m", "messages": [{"role": "user", "content": "hi"}],
                                      "tools": [_EDIT_TOOL], "tool_choice": "required"})
        ai._request(ai.chat_api_url, {"model": "m", "messages": [{"role": "user", "content": "hi"}],
                                      "response_format": {"type": "json_schema"}})
        self.assertEqual(sent[0]["tool_choice"], "required")
        self.assertEqual(sent[1]["response_format"], {"type": "json_schema"})
        self.assertNotIn("tools", sent[1])


if __name__ == '__main__':
    unittest.main()
