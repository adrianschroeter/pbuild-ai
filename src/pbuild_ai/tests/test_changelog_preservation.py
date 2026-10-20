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
    is_new_package_changes, sanitize_package_description, spec_description,
    collapse_repeated_separators, has_changelog_version, sanitize_release_notes,
    split_release_notes, versioned_release_versions,
    release_notes_changelog_body,
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
    """Fake LLM client whose analyze() returns a canned entry text."""

    def __init__(self, answer=""):
        self.answer = answer
        self.system = None
        self.context = None
        self.task = None

    def analyze(self, system_prompt, context_data, agents_md=None,
                format_json=False, task=""):
        self.system = system_prompt
        self.context = context_data
        self.task = task
        return self.answer


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
    """The AI only writes the entry text; pbuild-ai prepends it."""

    def _round(self, tmp, answer="", content=OLDER_ENTRIES, skill_manager=None):
        path = Path(tmp) / "ollama.changes"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        manager = _FakeManager({str(path): content or ""})
        ai = _FakeAI(answer)
        ok = _changelog_ai_round(
            ai, path, "0.33.1", "0.34.0", "Some release notes",
            "Maintainer <maint@opensuse.org>", manager,
            skill_manager=skill_manager)
        return path, ai, ok

    def test_missing_file_skips_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills = _FakeSkills()
            path, ai, ok = self._round(tmp, content=None, skill_manager=skills)
            self.assertFalse(ok)
            self.assertIsNone(ai.system)
            self.assertNotIn("changelog", skills.activated_skills)

    def test_entry_prepended_with_real_header(self):
        answer = ("Here is the entry:\n\n```\n"
                  "- Updated to version 0.34.0\n"
                  "  * Fixed the crash\n"
                  "- Update generated using pbuild-ai\n"
                  "```\n")
        with tempfile.TemporaryDirectory() as tmp:
            skills = _FakeSkills()
            path, ai, ok = self._round(tmp, answer, skill_manager=skills)
            self.assertTrue(ok)
            self.assertIn("changelog", skills.activated_skills)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.endswith(
                "\n\n- Updated to version 0.34.0\n"
                "  * Fixed the crash\n"
                "- Update generated using pbuild-ai\n\n" + OLDER_ENTRIES), text)
            self.assertRegex(text.splitlines()[1],
                             r"^\w{3} \w{3} [ \d]\d \d\d:\d\d:\d\d UTC \d{4} - "
                             r"Maintainer <maint@opensuse.org>$")
            self.assertEqual(text.count("Update generated using pbuild-ai"), 1)

    def test_header_and_repeated_older_entry_dropped(self):
        """A model that answers with a whole entry, plus the old first entry
        below it, still only gets its new body in."""
        answer = NEW_ENTRY + OLDER_ENTRIES
        with tempfile.TemporaryDirectory() as tmp:
            path, ai, ok = self._round(tmp, answer)
            self.assertTrue(ok)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.endswith(OLDER_ENTRIES))
            self.assertEqual(text.count("0.33.1"), 1)
            self.assertNotIn("Fri Sep 11", text)

    def test_no_answer_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, ai, ok = self._round(tmp, "")
            self.assertFalse(ok)
            self.assertEqual(path.read_text(encoding="utf-8"), OLDER_ENTRIES)

    def test_entry_without_target_version_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, ai, ok = self._round(tmp, "- Fixed some things\n")
            self.assertFalse(ok)
            self.assertEqual(path.read_text(encoding="utf-8"), OLDER_ENTRIES)

    def test_entry_repeating_older_version_is_failure(self):
        answer = ("- Updated to version 0.34.0\n  * New\n"
                  "- Update to 0.33.1\n  * Some older fix\n")
        with tempfile.TemporaryDirectory() as tmp:
            path, ai, ok = self._round(tmp, answer)
            self.assertFalse(ok)
            self.assertEqual(path.read_text(encoding="utf-8"), OLDER_ENTRIES)

    def test_existing_entry_for_version_skips_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, ai, ok = self._round(tmp, "- Updated to version 0.34.0\n",
                                       content=NEW_ENTRY + OLDER_ENTRIES)
            self.assertFalse(ok)
            self.assertIsNone(ai.system)

    def test_long_lines_wrapped(self):
        answer = ("- Updated to version 0.34.0\n"
                  "  * " + "word " * 30 + "\n")
        with tempfile.TemporaryDirectory() as tmp:
            path, ai, ok = self._round(tmp, answer)
            self.assertTrue(ok)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(all(len(l) <= 70 for l in text.splitlines()), text)
            self.assertIn("\n    word", text)

    def test_round_uses_changelog_skill_and_former_release_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, ai, ok = self._round(tmp, "- Updated to version 0.34.0\n  * x\n")
            self.assertTrue(ok)
            # The .changes skill must be part of the system prompt.
            self.assertIn("## .changes file format (openSUSE policy)", ai.system)
            self.assertIn("### Rules:", ai.system)
            self.assertIn("Output ONLY the body lines", ai.system)
            # ... including the former-release-coverage rule from the skill.
            self.assertIn("former upstream releases", ai.system)
            self.assertIn("older than some releases that upstream", ai.system)
            # The context repeats the former-release hint.
            self.assertIn("former releases", ai.context)
            self.assertIn("old version", ai.context)


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


class TestCollapseRepeatedSeparators(unittest.TestCase):
    def test_collapses_double_separator(self):
        content = ("Add a bullet\n"
                   "\n"
                   "-------------------------------------------------------------------\n"
                   "-------------------------------------------------------------------\n"
                   "Mon Jun 29 12:00:00 UTC 2026 - Maintainer <m@o>\n")
        out = collapse_repeated_separators(content)
        self.assertEqual(
            out,
            "Add a bullet\n"
            "\n"
            "-------------------------------------------------------------------\n"
            "Mon Jun 29 12:00:00 UTC 2026 - Maintainer <m@o>\n")

    def test_collapses_triple_separator(self):
        sep = "-------------------------------------------------------------------\n"
        out = collapse_repeated_separators(sep + sep + sep + "header\n")
        self.assertEqual(out, sep + "header\n")

    def test_single_separator_untouched(self):
        content = ("-------------------------------------------------------------------\n"
                   "Mon Jun 29 12:00:00 UTC 2026 - Maintainer <m@o>\n")
        self.assertEqual(collapse_repeated_separators(content), content)

    def test_empty_and_blank_inputs(self):
        self.assertEqual(collapse_repeated_separators(""), "")
        self.assertEqual(collapse_repeated_separators("\n\n"), "\n\n")

    def test_short_dashes_are_not_separators(self):
        # A bullet like '- Fix x' must never count as a separator line.
        content = "- Fix one thing\n- Fix another\n"
        self.assertEqual(collapse_repeated_separators(content), content)


MULTI_VERSION_NOTES = (
    "## 0.33.2\n"
    "\n"
    "### What's Changed\n"
    "- Added `gguf` memory mapping\n"
    "- Faster LLM loading\n"
    "\n"
    "## 0.33.3\n"
    "\n"
    "### What's Changed\n"
    "- Improved Windows GPU support\n"
    "\n"
    "## 0.34.0\n"
    "\n"
    "### What's Changed\n"
    "- Use Ollama models directly in ChatGPT Desktop\n"
    "- Improved structured output performance\n"
)


class TestSplitReleaseNotes(unittest.TestCase):
    def test_splits_version_sections(self):
        sections = split_release_notes(MULTI_VERSION_NOTES)
        self.assertEqual([v for v, _ in sections], ["0.33.2", "0.33.3", "0.34.0"])
        self.assertIn("memory mapping", sections[0][1])
        self.assertIn("ChatGPT Desktop", sections[2][1])

    def test_plain_notes_single_section(self):
        sections = split_release_notes("- Just a note\n")
        self.assertEqual(len(sections), 1)
        self.assertIsNone(sections[0][0])

    def test_empty(self):
        self.assertEqual(split_release_notes(""), [])
        self.assertEqual(split_release_notes(None), [])

    def test_versions_list(self):
        self.assertEqual(versioned_release_versions(MULTI_VERSION_NOTES),
                         ["0.33.2", "0.33.3", "0.34.0"])
        self.assertEqual(versioned_release_versions("plain notes"), [])


class TestSanitizeMultiVersionNotes(unittest.TestCase):
    def test_no_version_prefixes(self):
        bullets = sanitize_release_notes(MULTI_VERSION_NOTES, max_bullets=6)
        self.assertEqual(bullets[0], "  * Added gguf memory mapping")
        self.assertFalse(any(": " in b and b.split(":")[0].strip("* ").startswith("0.")
                             for b in bullets))

    def test_plain_notes_unchanged(self):
        bullets = sanitize_release_notes("- Some highlight\n")
        self.assertEqual(bullets, ["  * Some highlight"])


class TestReleaseNotesChangelogBody(unittest.TestCase):
    def test_one_group_per_release_newest_first(self):
        body = release_notes_changelog_body("0.34.0", "0.33.0", MULTI_VERSION_NOTES)
        self.assertEqual(body, [
            "- Updated to version 0.34.0",
            "  * Use Ollama models directly in ChatGPT Desktop",
            "  * Improved structured output performance",
            "- Updated to version 0.33.3",
            "  * Improved Windows GPU support",
            "- Updated to version 0.33.2",
            "  * Added gguf memory mapping",
            "  * Faster LLM loading",
        ])

    def test_caps_bullets_per_release(self):
        notes = ("## 0.33.3\n" + "".join(f"- a{i}\n" for i in range(7)) +
                 "## 0.34.0\n" + "".join(f"- b{i}\n" for i in range(7)))
        body = release_notes_changelog_body("0.34.0", "0.33.1", notes)
        self.assertIn("  * a4", body)
        self.assertNotIn("  * a5", body)
        self.assertNotIn("  * b5", body)
        self.assertIn("- Updated to version 0.33.3", body)

    def test_single_release_gets_more_bullets(self):
        notes = "## 0.34.0\n- one\n- two\n- three\n"
        body = release_notes_changelog_body("0.34.0", "0.33.1", notes)
        self.assertEqual(body, ["- Updated to version 0.34.0",
                                "  * one", "  * two", "  * three"])

    def test_skips_old_and_already_recorded_versions(self):
        existing = "- Updated to version 0.33.3\n"
        body = release_notes_changelog_body("0.34.0", "0.33.2", MULTI_VERSION_NOTES,
                                            existing_content=existing)
        headers = [l for l in body if l.startswith("- ")]
        self.assertEqual(headers, ["- Updated to version 0.34.0"])

    def test_plain_notes_go_under_new_version(self):
        body = release_notes_changelog_body("0.34.0", "0.33.1", "- Single fix\n")
        self.assertEqual(body, ["- Updated to version 0.34.0", "  * Single fix"])

    def test_no_notes(self):
        self.assertEqual(release_notes_changelog_body("0.34.0", "0.33.1", None),
                         ["- Updated to version 0.34.0"])


class TestWriteChangelogEntryIntermediate(unittest.TestCase):
    def test_intermediate_releases_get_own_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ollama.changes"
            if OLDER_ENTRIES:
                path.write_text(OLDER_ENTRIES, encoding="utf-8")
            ok = write_changelog_entry(path, "0.33.0", "0.34.0",
                                       "Maintainer <maint@opensuse.org>",
                                       release_notes=MULTI_VERSION_NOTES)
            self.assertTrue(ok)
            text = path.read_text(encoding="utf-8")
            self.assertIn("- Updated to version 0.34.0\n"
                          "  * Use Ollama models directly", text)
            self.assertIn("- Updated to version 0.33.3\n"
                          "  * Improved Windows GPU support\n"
                          "- Updated to version 0.33.2\n", text)
            self.assertNotIn("also covers intermediate", text)
            self.assertNotIn("* 0.3", text)
            self.assertFalse(would_duplicate_changelog_entry(text))
            # history intact
            self.assertTrue(text.rstrip().endswith("- Older change"))

    def test_plain_notes_single_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ollama.changes"
            path.write_text(OLDER_ENTRIES, encoding="utf-8")
            ok = write_changelog_entry(path, "0.33.1", "0.34.0",
                                       "Maintainer <maint@opensuse.org>",
                                       release_notes="- Single release notes")
            self.assertTrue(ok)
            text = path.read_text(encoding="utf-8")
            self.assertIn("- Updated to version 0.34.0\n  * Single release notes", text)


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


class TestNewPackageChangelog(unittest.TestCase):
    SPEC = ("Name: gufo\nVersion: 0.2.0\nSummary: Gufo tool\n\n"
            "%description\nGufo is a tool\nfor owls.\n\n%prep\n%setup -q\n")

    def test_first_entry_says_initial_package_with_description(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "gufo.changes"
            ok = write_changelog_entry(path, "", "0.2.0", "Jane <jane@example.org>",
                                       package_description="**Gufo** is a network automation "
                                                           "framework. It talks to devices.")
            text = path.read_text()
        self.assertTrue(ok)
        self.assertIn("- Initial package of gufo 0.2.0\n", text)
        self.assertIn("  Gufo is a network automation framework.", text)
        self.assertNotIn("Update", text)
        self.assertNotIn("**", text)

    def test_existing_changes_still_get_update_wording(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "gufo.changes"
            path.write_text("-" * 67 + "\nMon Jan  1 00:00:00 UTC 2026 - a <a@b.c>\n\n"
                            "- Initial package of gufo 0.1.0\n\n")
            write_changelog_entry(path, "", "0.2.0", "a <a@b.c>")
            text = path.read_text()
        self.assertIn("- Updated to version 0.2.0", text)
        self.assertEqual(text.count("Initial package"), 1)

    def test_is_new_package_changes(self):
        self.assertTrue(is_new_package_changes(""))
        self.assertTrue(is_new_package_changes("\n"))
        self.assertFalse(is_new_package_changes("-" * 67 + "\nMon ...\n"))

    def test_spec_description_fallback(self):
        self.assertEqual(spec_description(self.SPEC), "Gufo is a tool\nfor owls.")
        self.assertEqual(spec_description("Summary: Only summary\n"), "Only summary")

    def test_sanitize_description(self):
        self.assertEqual(sanitize_package_description("(model returned empty response)"), [])
        lines = sanitize_package_description("- Initial package\n" + "Word " * 200)
        self.assertTrue(all(l.startswith("  ") and len(l) <= 67 for l in lines))
        self.assertLessEqual(len(" ".join(lines)), 420)
        self.assertNotIn("Initial", " ".join(lines))

    def test_ai_description_and_fallback(self):
        from pbuild_ai.pbuild_ai import _new_package_description
        ai = MagicMock()
        ai.analyze.return_value = "Gufo automates networks."
        self.assertEqual(_new_package_description(ai, self.SPEC), "Gufo automates networks.")
        ai.analyze.return_value = "(model returned empty response)"
        self.assertEqual(_new_package_description(ai, self.SPEC), "Gufo is a tool\nfor owls.")


if __name__ == "__main__":
    unittest.main()