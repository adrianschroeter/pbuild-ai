"""Tests for the version-update prompt templates and the AI abort signal.

Guards two regressions:
  * bare documentation placeholders such as {repo} were once passed as empty
    strings, which silently rewrote the guidance into nonsense like
    "delivers the file as -0.33.2.tar.gz" and "append #/ to the Source URL".
  * VERSION_RESEARCH_SYSTEM_PROMPT raised KeyError because its call site never
    supplied the placeholders the template used.
"""

import os
import sys
import types
import unittest

_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules['yaml'] = _yaml

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.pbuild_ai import _ai_requested_abort, _check_update_hints
from pbuild_ai.skills.version_research_skill import (
    VERSION_RESEARCH_SYSTEM_PROMPT,
    VERSION_UPDATE_PROMPT,
)

# Exactly what pbuild_ai.py passes at each call site.
RESEARCH_KWARGS = dict(
    spec="ollama/ollama.spec",
    spec_content="Name: ollama\nVersion: 0.33.1\n",
    full_context="CTX",
    changelog_prompt="CHANGELOG",
    prefetched_context="",
    release_notes="",
)
UPDATE_KWARGS = dict(
    target_version="0.33.2",
    full_context="CTX",
    changelog_prompt="CHANGELOG",
    release_notes="",
    prefetched_context="",
)


class TestVersionPromptsRender(unittest.TestCase):
    def test_research_prompt_renders_without_keyerror(self):
        try:
            VERSION_RESEARCH_SYSTEM_PROMPT.format(**RESEARCH_KWARGS)
        except KeyError as exc:  # pragma: no cover - failure path
            self.fail(f"research prompt raised KeyError: {exc}")

    def test_update_prompt_renders_without_keyerror(self):
        try:
            VERSION_UPDATE_PROMPT.format(**UPDATE_KWARGS)
        except KeyError as exc:  # pragma: no cover - failure path
            self.fail(f"update prompt raised KeyError: {exc}")

    def test_documentation_placeholders_stay_literal(self):
        for name, template, kwargs in (
            ("research", VERSION_RESEARCH_SYSTEM_PROMPT, RESEARCH_KWARGS),
            ("update", VERSION_UPDATE_PROMPT, UPDATE_KWARGS),
        ):
            rendered = template.format(**kwargs)
            for needle in ("{repo}-{version}.tar.gz", "{actual_filenames}"):
                self.assertIn(needle, rendered, f"{name} lost literal {needle!r}")

    def test_rpm_macros_render_single_brace(self):
        for name, template, kwargs in (
            ("research", VERSION_RESEARCH_SYSTEM_PROMPT, RESEARCH_KWARGS),
            ("update", VERSION_UPDATE_PROMPT, UPDATE_KWARGS),
        ):
            rendered = template.format(**kwargs)
            self.assertIn("%{version}", rendered, name)
            self.assertIn("%{name}", rendered, name)

    def test_no_unrendered_double_braces(self):
        for name, template, kwargs in (
            ("research", VERSION_RESEARCH_SYSTEM_PROMPT, RESEARCH_KWARGS),
            ("update", VERSION_UPDATE_PROMPT, UPDATE_KWARGS),
        ):
            rendered = template.format(**kwargs)
            for leaked in ("{{repo}}", "{{version}}", "{{actual_filenames}}"):
                self.assertNotIn(leaked, rendered, f"{name} leaked {leaked!r}")

    def test_mandatory_project_steps_block_present(self):
        for name, template, kwargs in (
            ("research", VERSION_RESEARCH_SYSTEM_PROMPT, RESEARCH_KWARGS),
            ("update", VERSION_UPDATE_PROMPT, UPDATE_KWARGS),
        ):
            rendered = template.format(**kwargs)
            self.assertIn("MANDATORY PROJECT STEPS", rendered, name)
            self.assertIn("[ABORT: reason]", rendered, name)


class TestAiRequestedAbort(unittest.TestCase):
    def test_no_abort_returns_none(self):
        self.assertIsNone(_ai_requested_abort([
            {"role": "assistant", "content": "Updated the spec."}
        ]))

    def test_empty_messages_returns_none(self):
        self.assertIsNone(_ai_requested_abort([]))
        self.assertIsNone(_ai_requested_abort(None))

    def test_abort_reason_extracted(self):
        messages = [{"role": "assistant",
                     "content": "done\n[ABORT: tool-script post-update.sh blocked]"}]
        self.assertEqual(
            _ai_requested_abort(messages),
            "tool-script post-update.sh blocked",
        )

    def test_abort_without_reason_gets_default(self):
        messages = [{"role": "assistant", "content": "[ABORT:]"}]
        self.assertIsNotNone(_ai_requested_abort(messages))

    def test_user_turns_are_ignored(self):
        messages = [{"role": "user", "content": "[ABORT: not from the assistant]"}]
        self.assertIsNone(_ai_requested_abort(messages))


class TestCheckUpdateHintsAbort(unittest.TestCase):
    def test_abort_short_circuits_and_returns_true(self):
        messages = [{"role": "assistant", "content": "[ABORT: blocked script]"}]
        self.assertTrue(
            _check_update_hints([], messages, [], {}, set(), None)
        )

    def test_no_abort_returns_false(self):
        self.assertFalse(
            _check_update_hints([], [{"role": "assistant", "content": "ok"}],
                                [], {}, set(), None)
        )


if __name__ == "__main__":
    unittest.main()
