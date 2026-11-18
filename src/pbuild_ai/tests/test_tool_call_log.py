"""Tests for the '[AI] tool call: ...' echo: the log line must show the
arguments (file name, grep pattern/regex, ...) and not just the tool name."""

import io
import json
import os
import sys
import types
import unittest
from unittest.mock import patch

# Mock yaml before any pbuild_ai import to prevent ModuleNotFoundError
if 'yaml' not in sys.modules:
    _yaml = types.ModuleType('yaml')
    _yaml.YAMLError = Exception
    sys.modules['yaml'] = _yaml

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.llm_client import (
    _StreamAggregator, _parse_tool_args, _primary_arg, _tool_call_label,
    _summarize_round_calls,
)


class TestToolCallLabel(unittest.TestCase):
    def test_read_file_shows_path(self):
        self.assertEqual(_tool_call_label("read_file", {"path": "foo.spec"}),
                         "read_file foo.spec")

    def test_grep_shows_pattern_and_flags(self):
        label = _tool_call_label("grep", {"pattern": "protoc", "regex": True,
                                          "include": "*.spec", "path": "src"})
        self.assertEqual(label, "grep protoc (regex, include=*.spec, in src)")

    def test_grep_plain_pattern_defaults(self):
        label = _tool_call_label("grep", {"pattern": "nothing provides", "path": "."})
        self.assertEqual(label, "grep nothing provides")

    def test_grep_case_sensitive_flag(self):
        label = _tool_call_label("grep", {"pattern": "X", "ignore_case": False})
        self.assertIn("case-sensitive", label)

    def test_search_package_shows_query_and_mode(self):
        self.assertEqual(_tool_call_label("search_package", {"query": "/usr/bin/protoc",
                                                             "search_by": "file"}),
                         "search_package /usr/bin/protoc (search_by=file)")
        self.assertEqual(_tool_call_label("search_package", {"query": "protobuf"}),
                         "search_package protobuf")

    def test_json_string_arguments(self):
        label = _tool_call_label("grep", '{"pattern": "cmake", "regex": false}')
        self.assertEqual(label, "grep cmake")

    def test_broken_or_missing_arguments_degrade_to_name(self):
        self.assertEqual(_tool_call_label("grep", "{not json"), "grep")
        self.assertEqual(_tool_call_label("grep", None), "grep")
        self.assertEqual(_tool_call_label("grep", {}), "grep")

    def test_primary_arg_prefers_pattern_and_query_over_path(self):
        self.assertEqual(_primary_arg("grep", {"path": "src", "pattern": "protoc"}), "protoc")
        self.assertEqual(_primary_arg("search_package", {"query": "protobuf"}), "protobuf")
        # read_file unaffected
        self.assertEqual(_primary_arg("read_file", {"path": "foo.spec"}), "foo.spec")

    def test_round_summary_includes_pattern(self):
        s = _summarize_round_calls([("grep", {"pattern": "protoc"}, "src/x:1: hit")])
        self.assertIn("grep protoc", s)


class TestStreamEcho(unittest.TestCase):
    def _ollama_echo(self, tool_call):
        agg = _StreamAggregator(openai_mode=False)
        with patch('sys.stderr', new_callable=io.StringIO) as err:
            agg.feed_ollama({"message": {"role": "assistant", "content": "",
                                         "tool_calls": [tool_call]}})
        return err.getvalue()

    def _openai_stream(self, chunks):
        agg = _StreamAggregator(openai_mode=True)
        with patch('sys.stderr', new_callable=io.StringIO) as err:
            for chunk in chunks:
                agg.feed_openai_chunk(chunk)
                got = err.getvalue()
            out = agg.finish(is_chat=True)
            return got, err.getvalue(), out

    def test_ollama_native_echo_includes_arguments(self):
        out = self._ollama_echo({"function": {"name": "read_file",
                                              "arguments": '{"path": "foo.spec"}'}})
        self.assertIn("[AI] tool call: read_file foo.spec", out)

    def test_ollama_native_dict_arguments(self):
        out = self._ollama_echo({"function": {"name": "grep",
                                              "arguments": {"pattern": "protoc", "regex": True}}})
        self.assertIn("[AI] tool call: grep protoc (regex)", out)

    def test_openai_delta_echo_defers_until_full_arguments(self):
        name_chunk = {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "grep",
                                                  "arguments": '{"pat'}}]}}]}
        arg_chunk = {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": 'tern": "cmake"}'}}]}}]}
        mid, final, result = self._openai_stream([name_chunk, arg_chunk])
        # while streaming only the name is known — no echo yet
        self.assertNotIn("tool call", mid)
        # finish echoes once, with the complete arguments
        self.assertEqual(final.count("[AI] tool call:"), 1)
        self.assertIn("[AI] tool call: grep cmake", final)
        # aggregation itself is unaffected
        self.assertEqual(result["choices"][0]["message"]["tool_calls"][0]
                         ["function"]["arguments"], '{"pattern": "cmake"}')

    def test_openai_complete_message_echoes_immediately(self):
        chunk = {"choices": [{"message": {"role": "assistant", "content": "hi",
                                          "tool_calls": [{"id": "c", "function": {
                                              "name": "search_package",
                                              "arguments": '{"query": "protobuf", "search_by": "file"}'}}]}}]}
        _, final, _ = self._openai_stream([chunk])
        self.assertIn("[AI] tool call: search_package protobuf (search_by=file)", final)

    def test_openai_delta_broken_json_still_echoes_name(self):
        chunk = {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "write_file",
                                                  "arguments": '{"path":'}}]}}]}
        _, final, _ = self._openai_stream([chunk])
        self.assertIn("[AI] tool call: write_file\n", final)


class TestParseToolArgs(unittest.TestCase):
    def test_dict_passthrough(self):
        self.assertEqual(_parse_tool_args({"a": 1}), {"a": 1})

    def test_json_string(self):
        self.assertEqual(_parse_tool_args('{"a": 1}'), {"a": 1})

    def test_invalid_or_non_dict_yields_empty(self):
        self.assertEqual(_parse_tool_args("{oops"), {})
        self.assertEqual(_parse_tool_args('[1,2]'), {})
        self.assertEqual(_parse_tool_args(None), {})
        self.assertEqual(_parse_tool_args(""), {})


if __name__ == "__main__":
    unittest.main()
