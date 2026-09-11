import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Mock yaml before any pbuild_ai import (headless test host has no yaml)
_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules['yaml'] = _yaml

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.skills.changelog_skill import (
    has_changelog_version, sanitize_release_notes,
    would_duplicate_changelog_entry, write_changelog_entry,
)
from pbuild_ai.pbuild_ai import _changelog_ai_round, _extract_changelog_author, _norm_ws

OLDER_ENTRIES = (
    "-------------------------------------------------------------------\n"
    "Mon Jun 29 12:00:00 UTC 2026 - Maintainer <maint@opensuse.org>\n"
    "\n"
    "- Update to 0.33.1\n"
    "  * Some older fix\n"
    "\n"
    "-------------------------------------------------------------------\n"
    "Tue Nov  8 10:21:12 UTC 2022 - Previous Author <old@opensuse.org>\n"
    "\n"
    "- Older change\n"
)

NEW_ENTRY = "-------------------------------------------------------------------\n" \
    "Fri Sep 11 09:00:00 UTC 2026 - pbuild-ai <maint@opensuse.org>\n" \
    "\n" \
    "- Updated to version 0.34.0\n" \
    "  * Fixed the crash\n" \
    "- Update generated using pbuild-ai\n" \
    "\n"


class _FakeManager:
    def __init__(self, files=None):
        self.files = dict(files or {})

    def read_file_safe(self, path):
        return self.files.get(str(path), "")


class _FakeAI:
    """Fake LLM client whose call_with_tools simulates the tool round.

    The behavior hook is invoked with the resolved changes path; it may mutate
    the manager's file store to mimic edit_file/write_file side effects.
    """

    def __init__(self, manager, behavior=None):
        self.manager = manager
        self.behavior = behavior
        self.last_tools = None
        self.last_messages = None
        self.results = []

    def call_with_tools(self, messages, tools, manager, workspace_dir,
                        allow_tool_scripts=False, interactive=False,
                        max_rounds=3, task=""):
        self.last_tools = tools
        self.last_messages = messages
        self.last_task = task
        manager = manager or self.manager
        if self.behavior is not None:
            self.behavior(manager)
        return self.results


class _FakeSkills:
    def __init__(self):
        self.activated_skills = set()

    def note_skill_used(self, name):
        self.activated_skills.add(name)


class TestWriteChangelogEntryPreservesHistory(unittest.TestCase):
    def _write(self, tmp, content=OLDER_ENTRIES, notes=None):
        path = Path(tmp) / "ollama.changes"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        ok = write_changelog_entry(path, "0.33.1", "0.34.0",
                                   "Maintainer <maint@opensuse.org>",
                                   release_notes=notes)
        return path, ok

    def test_prepend_keeps_all_older_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, ok = self._write(tmp, notes=None)
            self.assertTrue(ok)
            text = path.read_text(encoding="utf-8")
            self.assertIn("Updated to version 0.34.0", text)
            for fragment in ("Some older fix", "Older change"):
                self.assertIn(fragment, text)
            # Oldest entry must still be the tail
            self.assertTrue(text.rstrip().endswith("- Older change"))

    def test_release_notes_added_as_subbullets_without_touching_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, ok = self._write(
                tmp,
                notes="## What's Changed\n- **Fixed** the crash\n- Added `gguf` splits\n")
            self.assertTrue(ok)
            text = path.read_text(encoding="utf-8")
            self.assertIn("  * Fixed the crash", text)
            self.assertIn("  * Added gguf splits", text)
            self.assertIn("Some older fix", text)
            self.assertTrue(text.rstrip().endswith("- Older change"))

    def test_duplicate_version_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ollama.changes"
            path.write_text(
                "-------------------------------------------------------------------\n"
                "Mon Sep 08 12:00:00 UTC 2026 - Maintainer <maint@opensuse.org>\n"
                "\n"
                "- Update to 0.34.0\n"
                "  * Some feature\n"
                "\n",
                encoding="utf-8")
            ok = write_changelog_entry(path, "0.33.1", "0.34.0",
                                       "Maintainer <maint@opensuse.org>")
            self.assertFalse(ok)
            self.assertIn("- Update to 0.34.0",
                          path.read_text(encoding="utf-8"))

    def test_duplicate_version_skipped_in_canonical_style_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ollama.changes"
            path.write_text(
                "-------------------------------------------------------------------\n"
                "Fri Sep 11 09:00:00 UTC 2026 - pbuild-ai <maint@opensuse.org>\n"
                "\n"
                "- Updated to version 0.34.0\n"
                "  * Fixed the crash\n"
                "\n",
                encoding="utf-8")
            ok = write_changelog_entry(path, "0.33.1", "0.34.0",
                                       "Maintainer <maint@opensuse.org>")
            self.assertFalse(ok)


class TestSanitizeReleaseNotes(unittest.TestCase):
    def test_strips_markup_and_limits_lines(self):
        notes = ("## What's Changed\n![img](https://x/i.png)\n"
                 "* **Fixed** crash at [link](https://x)\n"
                 "- Added `gguf` support\n"
                 "- Performance boost\n"
                 "-------------------------------\n"
                 "+ Extra trailing line\n"
                 "+ Yet another one\n"
                 "+ And one more\n")
        bullets = sanitize_release_notes(notes, max_bullets=4)
        self.assertLessEqual(len(bullets), 4)
        joined = " ".join(bullets)
        self.assertNotIn("![img]", joined)
        self.assertNotIn("**", joined)
        self.assertNotIn("`", joined)
        self.assertIn("Fixed crash at link", joined)
        self.assertIn("Added gguf support", joined)


class TestChangelogAiRoundPreservesHistory(unittest.TestCase):
    def _round(self, tmp, behavior=None, results=None, create_entry=False,
               skill_manager=None):
        path = Path(tmp) / "ollama.changes"
        if create_entry:
            path.write_text(NEW_ENTRY + OLDER_ENTRIES, encoding="utf-8")
        manager = _FakeManager({str(path): path.read_text(encoding="utf-8")
                                if path.exists() else ""})
        ai = _FakeAI(manager, behavior=behavior)
        if results is not None:
            ai.results = results
        tools = [
            {"type": "function", "function": {"name": n, "parameters": {}}}
            for n in ("edit_file", "write_file", "read_file", "web_fetch")
        ]
        ok = _changelog_ai_round(
            ai, path, "0.33.1", "0.34.0", "Some release notes",
            "Maintainer <maint@opensuse.org>", manager, tools,
            tmp, False, interactive=False, skill_manager=skill_manager)
        return path, manager, ai, ok

    def test_missing_file_skips_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills = _FakeSkills()
            path, manager, ai, ok = self._round(
                Path(tmp), create_entry=False, skill_manager=skills)
            self.assertFalse(ok)
            self.assertIsNone(ai.last_tools)
            self.assertNotIn("changelog", skills.activated_skills)

    def test_skill_recorded_when_round_runs(self):
        def prepend(manager):
            for key, old in list(manager.files.items()):
                manager.files[key] = NEW_ENTRY + old

        with tempfile.TemporaryDirectory() as tmp:
            skills = _FakeSkills()
            path, manager, ai, ok = self._round(
                Path(tmp), behavior=prepend, results=["edit_file: OK"],
                create_entry=True, skill_manager=skills)
            self.assertTrue(ok)
            self.assertIn("changelog", skills.activated_skills)

    def test_no_results_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, manager, ai, ok = self._round(
                Path(tmp), create_entry=True, results=[])
            self.assertFalse(ok)
            # untouched by the failed round
            self.assertEqual(manager.files[str(path)],
                             NEW_ENTRY + OLDER_ENTRIES)

    def test_stripping_older_entries_is_rejected(self):
        def strip_all(manager):
            for key in manager.files:
                manager.files[key] = NEW_ENTRY  # write_file overwrite

        with tempfile.TemporaryDirectory() as tmp:
            path, manager, ai, ok = self._round(Path(tmp), behavior=strip_all,
                                                results=["write_file: OK"],
                                                create_entry=True)
            self.assertFalse(ok)

    def test_prepend_keeping_history_is_accepted(self):
        def prepend(manager):
            for key, old in list(manager.files.items()):
                manager.files[key] = NEW_ENTRY + old

        with tempfile.TemporaryDirectory() as tmp:
            path, manager, ai, ok = self._round(Path(tmp), behavior=prepend,
                                                results=["edit_file: OK"],
                                                create_entry=True)
            self.assertTrue(ok)

    def test_native_style_entry_is_accepted(self):
        native = ("-------------------------------------------------------------------\n"
                  "Mon Sep 08 12:00:00 UTC 2026 - Maintainer <maint@opensuse.org>\n"
                  "\n"
                  "- Update to 0.34.0\n"
                  "  * Some feature\n"
                  "- Update generated using pbuild-ai\n"
                  "\n")

        def prepend_native(manager):
            for key, old in list(manager.files.items()):
                manager.files[key] = native + old

        with tempfile.TemporaryDirectory() as tmp:
            path, manager, ai, ok = self._round(
                Path(tmp), behavior=prepend_native, results=["edit_file: OK"],
                create_entry=True)
            self.assertTrue(ok)

    def test_only_edit_and_read_tools_offered(self):
        def prepend(manager):
            for key, old in list(manager.files.items()):
                manager.files[key] = NEW_ENTRY + old

        with tempfile.TemporaryDirectory() as tmp:
            resolved = Path(tmp) / "ollama.changes"
            resolved.write_text(OLDER_ENTRIES, encoding="utf-8")
            path, manager, ai, ok = self._round(
                Path(tmp), behavior=prepend, results=["edit_file: OK"],
                create_entry=True)
            offered = {(t.get("function", {}) or {}).get("name")
                       for t in ai.last_tools or []}
            self.assertIn("edit_file", offered)
            self.assertIn("read_file", offered)
            self.assertNotIn("write_file", offered)
            self.assertNotIn("web_fetch", offered)

    def test_round_uses_changelog_skill_and_former_release_hint(self):
        def prepend(manager):
            for key, old in list(manager.files.items()):
                manager.files[key] = NEW_ENTRY + old

        with tempfile.TemporaryDirectory() as tmp:
            resolved = Path(tmp) / "ollama.changes"
            resolved.write_text(NEW_ENTRY + OLDER_ENTRIES, encoding="utf-8")
            path, manager, ai, ok = self._round(
                Path(tmp), behavior=prepend, results=["edit_file: OK"],
                create_entry=True)
            self.assertTrue(ok)
            sys_msg = next((m["content"] for m in ai.last_messages
                            if m["role"] == "system"), "")
            user_msg = next((m["content"] for m in ai.last_messages
                             if m["role"] == "user"), "")
            # The .changes skill must be part of the system message.
            self.assertIn("## .changes file format (openSUSE policy)", sys_msg)
            self.assertIn("### Rules:", sys_msg)
            self.assertIn("- Update generated using pbuild-ai", sys_msg)
            # ... including the former-release-coverage rule from the skill.
            self.assertIn("former upstream releases", sys_msg)
            self.assertIn("older than some releases that upstream", sys_msg)
            # The user message repeats the former-release hint.
            self.assertIn("former releases", user_msg)
            self.assertIn("old version", user_msg)


class TestChangelogVersionDetection(unittest.TestCase):
    def test_has_version_matches_native_and_canonical_styles(self):
        content = (
            "-------------------------------------------------------------------\n"
            "Mon Sep 08 12:00:00 UTC 2026 - Adrian Schröter <adrian@suse.de>\n"
            "\n"
            "- Update to 0.34.0\n"
        )
        self.assertTrue(has_changelog_version(content, "0.34.0"))
        self.assertTrue(has_changelog_version(
            "- Updated to version 0.34.0", "0.34.0"))
        self.assertTrue(has_changelog_version(
            "- Update to v0.34.0", "0.34.0"))
        self.assertTrue(has_changelog_version(
            "- Updated to version 0.34.0", "v0.34.0"))
        self.assertFalse(has_changelog_version(content, "0.33.1"))
        self.assertFalse(has_changelog_version(
            "- Update to latest release", "0.34.0"))
        self.assertFalse(has_changelog_version(
            "- Update to the new API", "0.34.0"))

    def test_duplicate_detection_catches_all_styles(self):
        dup_native = (
            "- Update to 0.34.0\n"
            "- Update to 0.34.0\n"
        )
        dup_canonical = (
            "- Updated to version 0.34.0\n"
            "- Updated to version 0.34.0\n"
        )
        dup_mixed = (
            "- Updated to version 0.33.1\n"
            "- Update to 0.34.0\n"
            "- Updated to version 0.34.0\n"
        )
        self.assertTrue(would_duplicate_changelog_entry(dup_native))
        self.assertTrue(would_duplicate_changelog_entry(dup_canonical))
        self.assertTrue(would_duplicate_changelog_entry(dup_mixed))
        self.assertFalse(
            would_duplicate_changelog_entry("- Update to 0.33.1\n- Update to 0.34.0\n"))
        self.assertFalse(would_duplicate_changelog_entry(""))
        self.assertFalse(would_duplicate_changelog_entry(
            "- Update to latest release"))

    def test_historical_duplicates_do_not_block_new_entry(self):
        # ollama.changes (and other packages with long histories) contain
        # duplicate version strings in OLD entries — e.g. "Update to 0.3.6"
        # appears twice.  A fresh 0.34.0 entry must not be flagged just
        # because old entries already have dupes.
        rest_with_dupes = (
            "-------------------------------------------------------------------\n"
            "Sat Aug 15 18:59:48 UTC 2024 - maintainer <m@o>\n"
            "\n"
            "- Update to version 0.3.6:\n"
            "  * first mention\n"
            "\n"
            "-------------------------------------------------------------------\n"
            "Mon Jul 29 09:59:58 UTC 2024 - maintainer <m@o>\n"
            "\n"
            "- Update to version 0.3.6:\n"
            "  * second mention\n"
            "\n"
            "-------------------------------------------------------------------\n"
            "Tue Nov  8 10:21:12 UTC 2022 - maintainer <m@o>\n"
            "\n"
            "- Update to version 0.3.3\n"
        )
        content = NEW_ENTRY + rest_with_dupes
        self.assertFalse(would_duplicate_changelog_entry(content))

    def test_historical_dupes_block_actual_same_version(self):
        rest_with_034 = (
            "-------------------------------------------------------------------\n"
            "Mon Jun 29 12:00:00 UTC 2026 - maintainer <m@o>\n"
            "\n"
            "- Update to version 0.34.0\n"
        )
        content = NEW_ENTRY + rest_with_034
        self.assertTrue(would_duplicate_changelog_entry(content))


class TestNormWs(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(_norm_ws("a\n  b\tc"), "a b c")
        self.assertEqual(_norm_ws(""), "")


class TestExtractChangelogAuthor(unittest.TestCase):
    def test_extracts_author_from_top_entry(self):
        text = ("-------------------------------------------------------------------\n"
                "Thu Aug 27 04:51:49 UTC 2026 - Adrian Schröter <adrian@suse.de>\n"
                "\n"
                "- Updated to version 0.34.0\n")
        self.assertEqual(_extract_changelog_author(text),
                         "Adrian Schröter <adrian@suse.de>")

    def test_skips_separators_and_picks_first_header(self):
        text = ("-------------------------------------------------------------------\n"
                "-------------------------------------------------------------------\n"
                "Mon Jun 29 12:00:00 UTC 2026 - Maintainer <maint@opensuse.org>\n"
                "\n"
                "- Update to 0.33.1\n")
        self.assertEqual(_extract_changelog_author(text),
                         "Maintainer <maint@opensuse.org>")

    def test_returns_none_on_unknown_header(self):
        text = ("-------------------------------------------------------------------\n"
                "Unknown:\n"
                "\n"
                "- no author here\n")
        self.assertIsNone(_extract_changelog_author(text))

    def test_returns_none_for_empty_or_separator_only(self):
        self.assertIsNone(_extract_changelog_author(""))
        self.assertIsNone(_extract_changelog_author("-------------------------------------------------------------------\n"))
        self.assertIsNone(_extract_changelog_author(None))


if __name__ == "__main__":
    unittest.main()