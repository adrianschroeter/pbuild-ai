"""Tests for the text-only spec-rewrite fallback and the tool-capability guard."""

import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock

# Mock yaml before any pbuild_ai import
_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules['yaml'] = _yaml

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.llm_client import LlmAnalyzer
from pbuild_ai.pbuild_ai import _extract_rewritten_spec

_HEADER = ("#\n# spec file for package testpkg\n#\n"
           "# Copyright (c) 2026 SUSE LLC\n#\n\n")


def _spec_body(version="2.0", name="testpkg"):
    return (
        "Name:           %s\n"
        "Version:        %s\n"
        "Release:        0\n"
        "\n"
        "%%description\n"
        "Test package.\n"
        "\n"
        "%%prep\n"
        "%%setup -q\n"
        "\n"
        "%%build\n"
        "make\n"
        "\n"
        "%%install\n"
        "make install DESTDIR=%%{buildroot}\n"
        "\n"
        "%%files\n"
        "%%{_bindir}/%s\n" % (name, version, name)
    )


class TestExtractRewrittenSpec(unittest.TestCase):

    def test_fenced_spec_extraction_restores_header(self):
        current = _HEADER + _spec_body("0.1")
        text = ("Here is the corrected file:\n\n"
                "```spec\n" + _spec_body() + "```\n\nLet me know if it works.")
        result = _extract_rewritten_spec(text, current)
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith(_HEADER))
        self.assertIn("Version:        2.0", result)
        self.assertTrue(result.endswith("%{_bindir}/testpkg\n"))

    def test_bare_name_block_extraction(self):
        current = _HEADER + _spec_body("0.1")
        text = "Here is my update:\n" + _spec_body()
        result = _extract_rewritten_spec(text, current)
        self.assertIsNotNone(result)
        self.assertIn("Version:        2.0", result)

    def test_trailing_prose_rejected(self):
        current = _HEADER + _spec_body("0.1")
        text = "Here is my update:\n" + _spec_body() + "\nPlease let me know if this works"
        self.assertIsNone(_extract_rewritten_spec(text, current))

    def test_dist_restyle_rejected(self):
        current = _HEADER + _spec_body("0.1")
        candidate = _spec_body() + "%{?dist}\n"
        self.assertIsNone(_extract_rewritten_spec(candidate, current))

    def test_remote_asset_dropped_rejected(self):
        current = (_HEADER + _spec_body("0.1")
                   + "#!RemoteAsset: git+https://example.com/repo#tag\n")
        self.assertIsNone(_extract_rewritten_spec(_spec_body(), current))

    def test_name_mismatch_rejected(self):
        current = _HEADER + _spec_body("0.1")
        self.assertIsNone(_extract_rewritten_spec(_spec_body(name="otherpkg"), current))

    def test_missing_section_rejected(self):
        current = _HEADER + _spec_body("0.1")
        body = _spec_body().replace("%install\nmake install DESTDIR=%{buildroot}\n\n", "")
        self.assertIsNone(_extract_rewritten_spec("```spec\n" + body + "```", current))

    def test_unchanged_identity_rejected(self):
        current = _HEADER + _spec_body()
        self.assertIsNone(_extract_rewritten_spec(current, current))

    def test_empty_input_rejected(self):
        self.assertIsNone(_extract_rewritten_spec("", _spec_body()))
        self.assertIsNone(_extract_rewritten_spec(_spec_body(), ""))


class TestModelSupportsTools(unittest.TestCase):

    def setUp(self):
        self.ai = LlmAnalyzer(model="toolymodel")
        self.ai._is_ollama = True
        # Default in production is trust_tools=True (guard skipped). These tests
        # exercise the advertisement-based path, so disable the trust shortcut.
        self.ai._trust_tools = False

    @staticmethod
    def _tags_response(caps):
        raw = json.dumps({
            "models": [
                {"name": "toolymodel", "capabilities": caps},
                {"name": "plain", "capabilities": ["completion"]},
            ]
        }).encode()

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return raw

        return _Resp()

    def _stub_opener(self, resp):
        self.ai._opener = MagicMock()
        self.ai._opener.open.return_value = resp

    def test_supported_when_tools_advertised(self):
        self._stub_opener(self._tags_response(["completion", "tools"]))
        self.assertTrue(self.ai._model_supports_tools())
        self.assertTrue(self.ai._tool_capability_cache)

    def test_unsupported_returns_false(self):
        self._stub_opener(self._tags_response(["completion"]))
        self.assertFalse(self.ai._model_supports_tools())

    def test_trust_tools_overrides_unsupported(self):
        self.ai._trust_tools = True
        self._stub_opener(self._tags_response(["completion"]))
        self.assertTrue(self.ai._model_supports_tools())
        self.assertTrue(self.ai._model_thinking_capable)

    def test_trust_tools_defaults_true(self):
        ai = LlmAnalyzer(model="toolymodel")
        ai._is_ollama = True
        ai._opener = MagicMock()
        ai._opener.open.side_effect = Exception("boom")
        self.assertTrue(ai._model_supports_tools())
        self.assertTrue(ai._model_thinking_capable)

    def test_trust_tools_false_restores_gate(self):
        self.ai._trust_tools = False
        self._stub_opener(self._tags_response(["completion"]))
        self.assertFalse(self.ai._model_supports_tools())

    def test_trust_tools_popped_from_options(self):
        ai = LlmAnalyzer(model="toolymodel", options={"trust_tools": True, "temperature": 0.1})
        self.assertTrue(ai._trust_tools)
        self.assertNotIn("trust_tools", ai.options)
        ai2 = LlmAnalyzer(model="toolymodel", options={"trust_tools": False})
        self.assertFalse(ai2._trust_tools)
        self.assertNotIn("trust_tools", ai2.options)
        ai3 = LlmAnalyzer(model="toolymodel")
        self.assertTrue(ai3._trust_tools)

    def test_result_is_cached(self):
        self._stub_opener(self._tags_response(["completion"]))
        self.ai._model_supports_tools()
        self.ai._model_supports_tools()
        self.assertEqual(self.ai._opener.open.call_count, 1)

    def test_thinking_capable_when_advertised(self):
        self._stub_opener(self._tags_response(["completion", "tools", "thinking"]))
        self.assertTrue(self.ai._model_supports_tools())
        self.assertTrue(self.ai._model_thinking_capable)

    def test_not_thinking_capable(self):
        self._stub_opener(self._tags_response(["completion", "tools"]))
        self.assertTrue(self.ai._model_supports_tools())
        self.assertFalse(self.ai._model_thinking_capable)

    def test_thinking_fail_closed_on_error(self):
        self.ai._opener = MagicMock()
        self.ai._opener.open.side_effect = Exception("boom")
        self.assertTrue(self.ai._model_supports_tools())
        self.assertFalse(self.ai._model_thinking_capable)

    def test_thinking_unknown_model_after_query(self):
        self.ai.model = "unknown-model"
        self.ai._opener = MagicMock()
        self.ai._opener.open.side_effect = Exception("boom")
        self.ai._model_supports_tools()
        self.assertFalse(self.ai._model_thinking_capable)

    def test_fail_open_on_request_error(self):
        self.ai._opener = MagicMock()
        self.ai._opener.open.side_effect = Exception("boom")
        self.assertTrue(self.ai._model_supports_tools())
        self.assertTrue(self.ai._tool_capability_cache)

    def test_fail_open_for_unknown_model(self):
        self.ai.model = "unknown-model"
        self._stub_opener(self._tags_response(["completion", "tools"]))
        self.assertTrue(self.ai._model_supports_tools())

    def test_non_ollama_always_supported(self):
        self.ai._is_ollama = False
        self.assertTrue(self.ai._model_supports_tools())
        self.assertFalse(self.ai._model_thinking_capable)


class TestThinkingDefaultOption(unittest.TestCase):

    def setUp(self):
        self.ai = LlmAnalyzer(model="toolymodel")
        self.ai._is_ollama = True
        self.ai._openai_mode = False
        self.ai._model_thinking_capable = True
        self.ai.max_tokens = 32768

    def _payload(self):
        return {"model": "toolymodel", "messages": [], "tools": [], "stream": False}

    def test_thinking_defaulted_on_for_tool_calling(self):
        payload = self._payload()
        self.ai._apply_options_and_format(payload, tool_calling=True)
        self.assertTrue(payload["options"]["thinking"])

    def test_no_default_when_not_thinking_capable(self):
        self.ai._model_thinking_capable = False
        payload = self._payload()
        self.ai._apply_options_and_format(payload, tool_calling=True)
        self.assertNotIn("thinking", payload["options"])

    def test_no_default_for_non_tool_calling(self):
        self.ai._model_thinking_capable = True
        payload = self._payload()
        self.ai._apply_options_and_format(payload, tool_calling=False)
        self.assertNotIn("thinking", payload["options"])
        self.assertEqual(payload["format"], "json")

    def test_explicit_thinking_false_wins(self):
        self.ai.options = {"thinking": False}
        payload = self._payload()
        self.ai._apply_options_and_format(payload, tool_calling=True)
        self.assertIs(payload["options"]["thinking"], False)

    def test_explicit_think_false_wins(self):
        self.ai.options = {"think": False}
        payload = self._payload()
        self.ai._apply_options_and_format(payload, tool_calling=True)
        self.assertIs(payload["options"]["think"], False)
        self.assertNotIn("thinking", payload["options"])

    def test_openai_mode_never_injected(self):
        self.ai._openai_mode = True
        payload = self._payload()
        self.ai._apply_options_and_format(payload, tool_calling=True)
        self.assertNotIn("options", payload)


if __name__ == '__main__':
    unittest.main()