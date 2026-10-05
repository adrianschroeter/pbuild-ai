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

from pbuild_ai.pbuild_ai import (
    _ai_requested_abort,
    _check_update_hints,
    _is_prerelease,
    _notes_from_releases,
    _prefetch_summary,
    _version_from_research_data,
)
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


class TestNotesFromReleases(unittest.TestCase):
    RELEASES = [
        {"tag_name": "v0.34.0", "body": "New ChatGPT Desktop support"},
        {"tag_name": "0.33.3", "body": "Windows GPU fix"},
        {"tag_name": "v0.33.2", "body": "gguf memory mapping"},
        {"tag_name": "0.33.0", "body": "Current version noted"},
    ]

    def test_latest_and_sections_oldest_first(self):
        latest, notes = _notes_from_releases(self.RELEASES, "0.33.0", "body")
        self.assertEqual(latest, "0.34.0")
        self.assertIn("## 0.33.2", notes)
        self.assertIn("## 0.33.3", notes)
        self.assertIn("## 0.34.0", notes)
        # oldest first (32, 33, 34) so the target's notes are last
        self.assertLess(notes.index("## 0.33.2"), notes.index("## 0.33.3"))
        self.assertLess(notes.index("## 0.33.3"), notes.index("## 0.34.0"))

    def test_current_version_releases_excluded(self):
        latest, notes = _notes_from_releases(self.RELEASES, "0.33.0", "body")
        self.assertNotIn("Current version noted", notes)
        self.assertNotIn("## 0.33.0", notes)

    def test_already_at_latest_returns_empty_notes(self):
        latest, notes = _notes_from_releases(
            [{"tag_name": "0.33.0", "body": "current"}], "0.33.0", "body")
        self.assertEqual(latest, "0.33.0")
        self.assertEqual(notes, "")

    def test_v_prefix_normalized_for_order(self):
        # 'v0.9.9' must sort below '0.33.x' hence included.
        releases = [{"tag_name": "v0.34.0", "body": "new"},
                    {"tag_name": "v0.33.2", "body": "intermediate"},
                    {"tag_name": "v0.9.9", "body": "old"}]
        latest, notes = _notes_from_releases(releases, "0.9.9", "body")
        self.assertEqual(latest, "0.34.0")
        self.assertIn("## 0.33.2", notes)

    def test_empty_and_non_list_inputs(self):
        self.assertEqual(_notes_from_releases(None, "0.33.0", "body"),
                         ("", ""))
        self.assertEqual(_notes_from_releases([], "0.33.0", "body"),
                         ("", ""))
        self.assertEqual(_notes_from_releases([{"x": 1}], "0.33.0", "body"),
                         ("", ""))

    def test_gitlab_description_key(self):
        releases = [{"tag_name": "0.34.0", "description": "GL notes"}]
        latest, notes = _notes_from_releases(releases, "0.33.0", "description")
        self.assertEqual(latest, "0.34.0")
        self.assertIn("## 0.34.0", notes)
        self.assertIn("GL notes", notes)


class TestPrereleaseFiltering(unittest.TestCase):
    def test_parse_flags_prereleases(self):
        for v in ("0.34.4-rc0", "1.2.3-beta", "1.2.3alpha", "0.9pre1",
                  "2.0.0.dev1", "1.0~rc1", "3.2.0-milestone2", "0.5.0-beta2",
                  "1.2.3beta"):
            self.assertTrue(_is_prerelease(v), f"{v} should be prerelease")

    def test_parse_accepts_stable(self):
        for v in ("0.34.4", "1.2.3", "0.33.0", "v1.2.3", "10.11.12",
                  "1.1.1a", "1.1.1b", "1.1.1m", "2.4.1c"):
            self.assertFalse(_is_prerelease(v), f"{v} should be stable")
        self.assertFalse(_is_prerelease(None))

    def test_latest_skips_prerelease(self):
        releases = [
            {"tag_name": "0.34.4-rc0", "body": "release candidate"},
            {"tag_name": "v0.34.2", "body": "stable fix"},
            {"tag_name": "0.34.0", "body": "current"},
        ]
        latest, notes = _notes_from_releases(releases, "0.34.0", "body")
        self.assertEqual(latest, "0.34.2")
        self.assertIn("## 0.34.2", notes)
        self.assertNotIn("rc0", notes)

    def test_latest_skips_draft(self):
        releases = [
            {"tag_name": "0.35.0", "body": "draft", "draft": True},
            {"tag_name": "0.34.1", "body": "stable"},
        ]
        latest, notes = _notes_from_releases(releases, "0.34.0", "body")
        self.assertEqual(latest, "0.34.1")
        self.assertNotIn("draft", notes)

    def test_only_prereleases_available_yields_nothing(self):
        releases = [{"tag_name": "0.35.0-rc1", "body": "candidate"}]
        latest, notes = _notes_from_releases(releases, "0.34.0", "body")
        self.assertEqual(latest, "")
        self.assertEqual(notes, "")


class TestVersionFromResearchData(unittest.TestCase):
    def test_crates_io_uses_max_stable_version(self):
        data = {"crate": {"max_stable_version": "1.4.2", "max_version": "1.5.0-rc1"}}
        self.assertEqual(_version_from_research_data(data), "1.4.2")

    def test_pypi_info_version(self):
        self.assertEqual(_version_from_research_data({"info": {"version": "2.1.0"}}), "2.1.0")

    def test_github_latest_release(self):
        self.assertEqual(_version_from_research_data({"tag_name": "v3.0.1"}), "v3.0.1")

    def test_prerelease_flagged_release_skipped(self):
        self.assertIsNone(_version_from_research_data({"tag_name": "v3.1.0", "prerelease": True}))

    def test_prerelease_tag_falls_back_to_stable_field(self):
        data = {"version": "2.0.0-rc1", "crate": {"max_stable_version": "1.9.0"}}
        self.assertEqual(_version_from_research_data(data), "1.9.0")

    def test_prerelease_tag_without_alternative_returned_as_is(self):
        v = _version_from_research_data({"tag_name": "2.0.0-beta1"})
        self.assertEqual(v, "2.0.0-beta1")
        self.assertTrue(_is_prerelease(v))

    def test_list_skips_prereleases_and_drafts(self):
        data = [
            {"tag_name": "0.35.0", "draft": True},
            {"tag_name": "0.34.4-rc0"},
            {"tag_name": "0.34.3", "prerelease": True},
            {"tag_name": "0.34.2"},
        ]
        self.assertEqual(_version_from_research_data(data), "0.34.2")

    def test_unrecognized_data(self):
        self.assertIsNone(_version_from_research_data({"foo": "bar"}))
        self.assertIsNone(_version_from_research_data([]))
        self.assertIsNone(_version_from_research_data("1.0"))


class TestNotesBudget(unittest.TestCase):
    def test_long_notes_keep_target_release(self):
        releases = [{"tag_name": "v0.7.0", "body": "seven " * 2000},
                    {"tag_name": "v0.6.0", "body": "six " * 2000},
                    {"tag_name": "v0.5.0", "body": "five " * 2000}]
        latest, notes = _notes_from_releases(releases, "0.4.0", "body")
        self.assertEqual(latest, "0.7.0")
        for v in ("0.5.0", "0.6.0", "0.7.0"):
            self.assertIn(f"## {v}", notes)
        self.assertIn("seven", notes)
        self.assertLess(len(notes), 10100)


if __name__ == "__main__":
    unittest.main()
