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

from pbuild_ai.pbuild_ai import _ai_requested_abort, _check_update_hints, _prefetch_summary
from pbuild_ai.skills.version_research_skill import (
    VERSION_RESEARCH_SYSTEM_PROMPT,
    VERSION_RESEARCH_TASK_PROMPT,
    VERSION_UPDATE_SYSTEM_PROMPT,
    VERSION_UPDATE_TASK_PROMPT,
)

# Exactly what pbuild_ai.py passes at each call site.
RESEARCH_SYSTEM_KWARGS = dict(
    full_context="CTX",
    changelog_prompt="CHANGELOG",
)
RESEARCH_TASK_KWARGS = dict(
    spec="ollama/ollama.spec",
    spec_content="Name: ollama\nVersion: 0.33.1\n",
    prefetched_context="",
    release_notes="",
)
UPDATE_SYSTEM_KWARGS = dict(
    full_context="CTX",
)
UPDATE_TASK_KWARGS = dict(
    target_version="0.33.2",
    spec="ollama.spec",
    cur_version="0.33.1",
)


class TestVersionPromptsRender(unittest.TestCase):
    def test_research_prompt_renders_without_keyerror(self):
        try:
            VERSION_RESEARCH_SYSTEM_PROMPT.format(**RESEARCH_SYSTEM_KWARGS)
            VERSION_RESEARCH_TASK_PROMPT.format(**RESEARCH_TASK_KWARGS)
        except KeyError as exc:  # pragma: no cover - failure path
            self.fail(f"research prompt raised KeyError: {exc}")

    def test_update_prompt_renders_without_keyerror(self):
        try:
            VERSION_UPDATE_SYSTEM_PROMPT.format(**UPDATE_SYSTEM_KWARGS)
            VERSION_UPDATE_TASK_PROMPT.format(**UPDATE_TASK_KWARGS)
        except KeyError as exc:  # pragma: no cover - failure path
            self.fail(f"update prompt raised KeyError: {exc}")

    def test_documentation_placeholders_stay_literal(self):
        for name, template, kwargs in (
            ("research", VERSION_RESEARCH_SYSTEM_PROMPT, RESEARCH_SYSTEM_KWARGS),
            ("update", VERSION_UPDATE_SYSTEM_PROMPT, UPDATE_SYSTEM_KWARGS),
        ):
            rendered = template.format(**kwargs)
            for needle in ("{repo}-{version}.tar.gz", "{actual_filenames}"):
                self.assertIn(needle, rendered, f"{name} lost literal {needle!r}")

    def test_rpm_macros_render_single_brace(self):
        for name, template, kwargs in (
            ("research", VERSION_RESEARCH_SYSTEM_PROMPT, RESEARCH_SYSTEM_KWARGS),
            ("update", VERSION_UPDATE_SYSTEM_PROMPT, UPDATE_SYSTEM_KWARGS),
        ):
            rendered = template.format(**kwargs)
            self.assertIn("%{version}", rendered, name)
            self.assertIn("%{name}", rendered, name)

    def test_no_unrendered_double_braces(self):
        for name, template, kwargs in (
            ("research", VERSION_RESEARCH_SYSTEM_PROMPT, RESEARCH_SYSTEM_KWARGS),
            ("update", VERSION_UPDATE_SYSTEM_PROMPT, UPDATE_SYSTEM_KWARGS),
        ):
            rendered = template.format(**kwargs)
            for leaked in ("{{repo}}", "{{version}}", "{{actual_filenames}}"):
                self.assertNotIn(leaked, rendered, f"{name} leaked {leaked!r}")

    def test_mandatory_project_steps_block_present(self):
        for name, template, kwargs in (
            ("research", VERSION_RESEARCH_SYSTEM_PROMPT, RESEARCH_SYSTEM_KWARGS),
            ("update", VERSION_UPDATE_SYSTEM_PROMPT, UPDATE_SYSTEM_KWARGS),
        ):
            rendered = template.format(**kwargs)
            self.assertIn("MANDATORY PROJECT STEPS", rendered, name)
            self.assertIn("[ABORT: reason]", rendered, name)

    def test_update_system_scoped_to_mechanical_upgrade(self):
        rendered = VERSION_UPDATE_SYSTEM_PROMPT.format(**UPDATE_SYSTEM_KWARGS)
        self.assertIn("THIS ROUND IS THE MECHANICAL UPGRADE ONLY", rendered)
        self.assertIn(("pbuild-ai handles the .changes entry and those scripts "
                       "in dedicated rounds that follow this one"), rendered)
        self.assertNotIn("run_tool_script", rendered)

    def test_update_task_scoped_to_mechanical_upgrade(self):
        rendered = VERSION_UPDATE_TASK_PROMPT.format(**UPDATE_TASK_KWARGS)
        self.assertNotIn("check for additional changes", rendered)
        self.assertNotIn("executing tasks", rendered)
        self.assertNotIn("downloading resources", rendered)

    def test_task_prompts_carry_the_spec_and_mandate(self):
        update = VERSION_UPDATE_TASK_PROMPT.format(**UPDATE_TASK_KWARGS)
        research = VERSION_RESEARCH_TASK_PROMPT.format(**RESEARCH_TASK_KWARGS)
        self.assertIn("Change the Version tag of ollama.spec from 0.33.1 to 0.33.2.", update)
        self.assertIn("read_file ollama.spec", update)
        self.assertNotIn("## Release notes", update)
        self.assertIn("## Release notes", research)
        self.assertIn("ollama.spec", update)
        self.assertNotIn("spec_content", update)
        self.assertNotIn("already-at-latest", update)
        self.assertIn("already-at-latest", research)
        self.assertIn("ollama/ollama.spec", research)
        self.assertIn("Name: ollama\nVersion: 0.33.1", research)

    def test_system_prompts_hold_no_volatile_task_data(self):
        update = VERSION_UPDATE_SYSTEM_PROMPT.format(**UPDATE_SYSTEM_KWARGS)
        research = VERSION_RESEARCH_SYSTEM_PROMPT.format(**RESEARCH_SYSTEM_KWARGS)
        for leaked in ("{target_version}", "{spec}", "{spec_content}",
                       "{prefetched_context}", "{release_notes}", "{generate_prompt}"):
            self.assertNotIn(leaked, update, f"update system leaked {leaked!r}")
            self.assertNotIn(leaked, research, f"research system leaked {leaked!r}")


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


class TestPrefetchSummary(unittest.TestCase):
    def test_drops_assets_body_author_keeps_version_fields(self):
        payload = {
            "tag_name": "v0.34.0",
            "html_url": "https://github.com/ollama/ollama/releases/tag/v0.34.0",
            "body": "Use Ollama models in ChatGPT Desktop",
            "author": {"login": "bot", "id": 1},
            "assets": [{"name": "big.bin", "url": "https://x"}],
        }
        out = _prefetch_summary(payload)
        self.assertEqual(out["tag_name"], "v0.34.0")
        self.assertNotIn("body", out)
        self.assertNotIn("author", out)
        self.assertNotIn("assets", out)

    def test_nested_dict_carrying_version_is_kept(self):
        payload = {"info": {"version": "0.33.1", "home_page": "https://pypi.org"}}
        out = _prefetch_summary(payload)
        self.assertEqual(out["info"]["version"], "0.33.1")

    def test_huge_releases_map_is_dropped(self):
        payload = {"info": {"version": "1.0"},
                   "releases": {"1.0": [{"x": 1} for _ in range(100)]}}
        out = _prefetch_summary(payload)
        self.assertIn("info", out)
        self.assertNotIn("releases", out)

    def test_top_level_list_is_capped(self):
        out = _prefetch_summary(["a" * 50 for _ in range(100)])
        self.assertEqual(len(out), 20)

    def test_long_text_is_truncated(self):
        out = _prefetch_summary({"tag_name": "y" * 5000})
        self.assertLess(len(out["tag_name"]), 2000)

    def test_scalars_pass_through(self):
        self.assertEqual(_prefetch_summary("plain"), "plain")
        self.assertEqual(_prefetch_summary(42), 42)
        self.assertEqual(_prefetch_summary(None), None)


if __name__ == "__main__":
    unittest.main()
