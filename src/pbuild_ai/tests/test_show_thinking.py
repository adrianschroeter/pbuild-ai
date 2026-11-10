"""Tests for --show-thinking: live streaming of AI thinking and content.

The stream aggregator must assemble exactly the result shape the
corresponding non-streaming response would have produced, so all downstream
handling (message extraction, context handling, transcripts, retries)
stays unchanged."""

import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import MagicMock, patch

_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules.setdefault('yaml', _yaml)

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.llm_client import LlmAnalyzer, _StreamAggregator, _wants_streaming


class FakeStreamResponse:
    """Context-manager response yielding fixed lines; supports iteration
    (streaming) and read() (non-streaming)."""

    def __init__(self, lines):
        self._lines = [ln.encode("utf-8") if isinstance(ln, str) else ln
                       for ln in lines]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return b"".join(self._lines)


class FakeOpener:
    """Replacement for LlmAnalyzer._opener recording every request."""

    def __init__(self, lines):
        self.lines = lines
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return FakeStreamResponse(self.lines)


def _make_ai(show_thinking, lines):
    ai = LlmAnalyzer(host="http://localhost:11434", model="test",
                     show_thinking=show_thinking)
    opener = FakeOpener(lines)
    ai._opener = opener
    return ai, opener


class TestShowThinkingFlag(unittest.TestCase):
    """The flag exists, defaults to off, and is wired through strictly."""

    def test_analyzer_attribute_default(self):
        ai = LlmAnalyzer(host="http://localhost:11434", model="test")
        self.assertFalse(ai.show_thinking)

    def test_analyzer_attribute_enabled(self):
        ai = LlmAnalyzer(host="http://localhost:11434", model="test",
                         show_thinking=True)
        self.assertTrue(ai.show_thinking)

    def test_wants_streaming_strict(self):
        """A truthy Mock (MagicMock auto-attribute) must not enable streaming."""
        self.assertFalse(_wants_streaming(MagicMock()))
        self.assertTrue(_wants_streaming(MagicMock(show_thinking=True)))


class TestNativeStreaming(unittest.TestCase):
    """Ollama native NDJSON streaming aggregation via _request_once."""

    def _generate_lines(self):
        return [
            json.dumps({"model": "test", "response": "",
                        "thinking": "I should "}) + "\n",
            json.dumps({"model": "test", "response": "",
                        "thinking": "check the spec."}) + "\n",
            json.dumps({"model": "test", "response": "Version is 1.2",
                        "done": False}) + "\n",
            json.dumps({"model": "test", "response": "", "done": True,
                        "done_reason": "stop", "context": [1, 2, 3]}) + "\n",
        ]

    def test_generate_streams_and_aggregates(self):
        ai, opener = _make_ai(True, self._generate_lines())
        payload = {"model": "test", "prompt": "hello", "stream": False}
        with patch('sys.stderr', new_callable=io.StringIO) as err:
            result = ai._request_once(ai.api_url, payload)
        self.assertEqual(result["response"], "Version is 1.2")
        self.assertEqual(result["thinking"], "I should check the spec.")
        self.assertEqual(result["context"], [1, 2, 3])
        self.assertTrue(result["done"])
        sent = json.loads(opener.requests[-1].data.decode())
        self.assertTrue(sent["stream"])
        err_text = err.getvalue()
        self.assertIn("[AI] thinking:", err_text)
        self.assertIn("I should check the spec.", err_text)

    def test_default_stays_non_streaming(self):
        ai, opener = _make_ai(False, [
            json.dumps({"model": "test", "response": "ok", "done": True})])
        payload = {"model": "test", "prompt": "hello", "stream": False}
        result = ai._request_once(ai.api_url, payload)
        self.assertEqual(result["response"], "ok")
        sent = json.loads(opener.requests[-1].data.decode())
        self.assertFalse(sent["stream"])

    def test_chat_aggregates_message_and_tool_calls(self):
        """The done=true chunk repeats the full message (as Ollama does):
        aggregation from the incremental chunks must not duplicate it."""
        tc = {"function": {"name": "edit_file", "arguments": {"path": "x.spec"}}}
        lines = [
            json.dumps({"model": "test",
                        "message": {"role": "assistant", "content": "",
                                    "thinking": "Need a tool."},
                        "done": False}) + "\n",
            json.dumps({"model": "test",
                        "message": {"role": "assistant", "content": "",
                                    "tool_calls": [tc]},
                        "done": False}) + "\n",
            json.dumps({"model": "test",
                        "message": {"role": "assistant", "content": "",
                                    "tool_calls": [tc]},
                        "done": True}) + "\n",
        ]
        ai, opener = _make_ai(True, lines)
        payload = {"model": "test",
                   "messages": [{"role": "user", "content": "go"}],
                   "stream": False}
        with patch('sys.stderr', new_callable=io.StringIO) as err:
            result = ai._request_once(ai.chat_api_url, payload)
        self.assertEqual(result["message"]["thinking"], "Need a tool.")
        self.assertEqual(result["message"]["content"], "")
        self.assertEqual(len(result["message"]["tool_calls"]), 1)
        self.assertEqual(result["message"]["tool_calls"][0]["function"]["name"],
                         "edit_file")
        self.assertEqual(result["message"]["tool_calls"][0]["function"]["arguments"],
                         {"path": "x.spec"})
        sent = json.loads(opener.requests[-1].data.decode())
        self.assertTrue(sent["stream"])
        self.assertIn("[AI] tool call: edit_file", err.getvalue())

    def test_chat_fallback_to_final_message(self):
        """A server that only sends the complete message in the done chunk."""
        lines = [json.dumps({"model": "test",
                             "message": {"role": "assistant",
                                         "content": "all done"},
                             "done": True})]
        ai, _opener = _make_ai(True, lines)
        payload = {"model": "test",
                   "messages": [{"role": "user", "content": "go"}],
                   "stream": False}
        with patch('sys.stderr', new_callable=io.StringIO):
            result = ai._request_once(ai.chat_api_url, payload)
        self.assertEqual(result["message"]["content"], "all done")

    def test_pretty_json_body_fallback(self):
        """A native server that ignored stream=true and sent one pretty
        printed JSON body still aggregates into the generate shape."""
        body = json.dumps({"model": "test", "response": "plain text",
                           "done": True}, indent=2)
        ai, _opener = _make_ai(True, body.splitlines(keepends=True))
        payload = {"model": "test", "prompt": "hello", "stream": False}
        with patch('sys.stderr', new_callable=io.StringIO):
            result = ai._request_once(ai.api_url, payload)
        self.assertEqual(result["response"], "plain text")

    def test_error_chunk_raises_for_retry_logic(self):
        """Ollama reports 'token repeat limit reached' as an error chunk; it
        must surface as RuntimeError so _request's retry logic keeps working."""
        lines = [
            json.dumps({"model": "test", "response": "", "thinking": "hm"}) + "\n",
            json.dumps({"error": "token repeat limit reached"}) + "\n",
        ]
        ai, _opener = _make_ai(True, lines)
        payload = {"model": "test", "prompt": "hello", "stream": False}
        with patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(RuntimeError) as cm:
                ai._request_once(ai.api_url, payload)
        self.assertIn("repeat limit", str(cm.exception))


class TestOpenAiStreamAggregation(unittest.TestCase):
    """OpenAI-compatible SSE streaming aggregation."""

    def _sse_lines(self):
        return [
            'data: {"choices":[{"delta":{"reasoning_content":"Hmm "}}]}\n',
            '\n',
            'data: {"choices":[{"delta":{"reasoning":"deep"}}]}\n',
            'data: {"choices":[{"delta":{"content":"Hi"}}]}\n',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
            '"function":{"name":"edit_file","arguments":"{\\"path\\":"}}]}}]}\n',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"function":{"arguments":"\\"x.spec\\"}"}}]}}]}\n',
            'data: [DONE]\n',
        ]

    def test_sse_aggregation(self):
        agg = _StreamAggregator(openai_mode=True)
        with patch('sys.stderr', new_callable=io.StringIO):
            result = agg.consume(self._sse_lines(), is_chat=True)
        msg = result["choices"][0]["message"]
        self.assertEqual(msg["content"], "Hi")
        self.assertEqual(msg["reasoning_content"], "Hmm deep")
        tc = msg["tool_calls"][0]
        self.assertEqual(tc["id"], "c1")
        self.assertEqual(tc["function"]["name"], "edit_file")
        self.assertEqual(tc["function"]["arguments"], '{"path":"x.spec"}')

    def test_sse_result_transforms_to_ai_format(self):
        """The aggregated OpenAI shape must survive _ai_response_from_openai:
        reasoning becomes 'thinking' and tool call arguments are parsed."""
        agg = _StreamAggregator(openai_mode=True)
        with patch('sys.stderr', new_callable=io.StringIO):
            result = agg.consume(self._sse_lines(), is_chat=True)
        ai = LlmAnalyzer(host="http://localhost:11434", model="test")
        msg = ai._ai_response_from_openai(result)["message"]
        self.assertEqual(msg["thinking"], "Hmm deep")
        self.assertEqual(msg["content"], "Hi")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "edit_file")
        self.assertEqual(msg["tool_calls"][0]["function"]["arguments"],
                         {"path": "x.spec"})

    def test_keep_alive_comments_skipped(self):
        lines = [
            ': keep-alive\n',
            'data: {"choices":[{"delta":{"content":"Hi"}}]}\n',
            'data: [DONE]\n',
        ]
        agg = _StreamAggregator(openai_mode=True)
        with patch('sys.stderr', new_callable=io.StringIO):
            result = agg.consume(lines, is_chat=True)
        self.assertEqual(result["choices"][0]["message"]["content"], "Hi")

    def test_server_ignoring_stream_fallback(self):
        body = json.dumps({"model": "gpt", "choices": [
            {"message": {"role": "assistant", "content": "plain",
                         "reasoning_content": "why"}}]})
        agg = _StreamAggregator(openai_mode=True)
        with patch('sys.stderr', new_callable=io.StringIO):
            result = agg.consume([body + "\n"], is_chat=True)
        self.assertEqual(result["choices"][0]["message"]["content"], "plain")
        self.assertEqual(result["choices"][0]["message"]["reasoning_content"],
                         "why")

    def test_error_field_raises(self):
        lines = ['data: {"error": "token repeat limit reached"}\n']
        agg = _StreamAggregator(openai_mode=True)
        with patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(RuntimeError) as cm:
                agg.consume(lines, is_chat=True)
        self.assertIn("repeat limit", str(cm.exception))


class TestChatCompletionStreaming(unittest.TestCase):
    """The module-level chat_completion() used by --generate/--modify."""

    def test_chat_completion_streams_when_enabled(self):
        ai = LlmAnalyzer(host="http://localhost:11434", model="test",
                         show_thinking=True)

        def fake_urlopen(req, timeout=None):
            data = json.loads(req.data.decode())
            self.assertTrue(data["stream"])
            return FakeStreamResponse([
                json.dumps({"model": "test",
                            "message": {"role": "assistant", "content": "",
                                        "thinking": "Let me see."},
                            "done": False}) + "\n",
                json.dumps({"model": "test",
                            "message": {"role": "assistant",
                                        "content": "Hello"},
                            "done": False}) + "\n",
                json.dumps({"model": "test",
                            "message": {"role": "assistant", "content": ""},
                            "done": True}) + "\n",
            ])

        with patch('sys.stderr', new_callable=io.StringIO) as err:
            with patch('urllib.request.urlopen', side_effect=fake_urlopen):
                from pbuild_ai.llm_client import chat_completion
                result = chat_completion(ai, [{"role": "user", "content": "go"}],
                                         [], debug=False)
        self.assertEqual(result["message"]["content"], "Hello")
        self.assertEqual(result["message"]["thinking"], "Let me see.")
        self.assertIn("Let me see.", err.getvalue())

    def test_chat_completion_default_non_streaming(self):
        ai = LlmAnalyzer(host="http://localhost:11434", model="test")

        def fake_urlopen(req, timeout=None):
            data = json.loads(req.data.decode())
            self.assertFalse(data["stream"])
            return FakeStreamResponse([json.dumps(
                {"model": "test",
                 "message": {"role": "assistant", "content": "Hello"},
                 "done": True})])

        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            from pbuild_ai.llm_client import chat_completion
            result = chat_completion(ai, [{"role": "user", "content": "go"}],
                                     [], debug=False)
        self.assertEqual(result["message"]["content"], "Hello")


class TestCliWiring(unittest.TestCase):
    """--show-thinking reaches the LlmAnalyzer constructor; default is off."""

    def _spec_tmpdir(self):
        tmpdir = tempfile.mkdtemp(prefix="showthink_cli_")
        self.addCleanup(__import__("shutil").rmtree, tmpdir, True)
        with open(os.path.join(tmpdir, "demo.spec"), "w") as f:
            f.write("Name: demo\nVersion: 1.0\nRelease: 0\nSummary: x\n"
                    "License: MIT\n\n%description\nTest.\n")
        return tmpdir

    def _run_main(self, argv):
        """Run pa.main() in a subprocess with a spying LlmAnalyzer.__init__."""
        tmpdir = self._spec_tmpdir()
        script = textwrap.dedent(f"""\
        import sys, types
        from unittest.mock import patch
        _yaml = types.ModuleType('yaml'); _yaml.YAMLError = Exception
        sys.modules['yaml'] = _yaml
        sys.path.insert(0, {SRC_DIR!r})
        sys.argv = ["pbuild-ai"] + {argv!r} + [{tmpdir!r}]
        from pbuild_ai.llm_client import LlmAnalyzer
        import pbuild_ai.pbuild_ai as pa
        seen = []
        _real_init = LlmAnalyzer.__init__
        def spying_init(self, *args, **kwargs):
            seen.append(kwargs.get("show_thinking"))
            _real_init(self, *args, **kwargs)
        with patch.object(LlmAnalyzer, "__init__", spying_init), \\
                patch.object(LlmAnalyzer, "analyze", return_value=""), \\
                patch.object(LlmAnalyzer, "call_with_tools", return_value=[]), \\
                patch.object(LlmAnalyzer, "print_stats"):
            try:
                pa.main()
            except BaseException:
                pass
        print("SHOW_THINKING:", seen[-1] if seen else "<never constructed>")
        """)
        result = subprocess.run([sys.executable, "-c", script],
                                capture_output=True, text=True,
                                timeout=120, cwd=tmpdir,
                                env={**os.environ,
                                     "PYTHONDONTWRITEBYTECODE": "1"})
        return result.stdout + result.stderr

    def test_flag_enables_show_thinking(self):
        out = self._run_main(["--show-thinking"])
        self.assertIn("SHOW_THINKING:", out)
        self.assertIn("SHOW_THINKING: True", out)

    def test_default_is_false(self):
        out = self._run_main([])
        self.assertIn("SHOW_THINKING:", out)
        self.assertIn("SHOW_THINKING: False", out)

    def test_help_lists_option(self):
        tmpdir = self._spec_tmpdir()
        script = textwrap.dedent(f"""\
        import sys, types
        _yaml = types.ModuleType('yaml'); _yaml.YAMLError = Exception
        sys.modules['yaml'] = _yaml
        sys.path.insert(0, {SRC_DIR!r})
        sys.argv = ["pbuild-ai", "--help"]
        import pbuild_ai.pbuild_ai as pa
        try:
            pa.main()
        except SystemExit:
            pass
        """)
        result = subprocess.run([sys.executable, "-c", script],
                                capture_output=True, text=True,
                                timeout=60, cwd=tmpdir,
                                env={**os.environ,
                                     "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertIn("--show-thinking", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()