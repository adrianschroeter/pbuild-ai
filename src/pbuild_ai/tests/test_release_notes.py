import io
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from pbuild_ai.release_notes import archive_release_notes, changelog_sections
from pbuild_ai.skills.changelog_skill import release_notes_changelog_body

CHANGES_RST = """\
================
Cython Changelog
================

3.3.0 (2026-10-01)
==================

Other changes
-------------

* No functional changes since 3.3.0 rc 1.


3.3.0 rc 1 (2026-09-20)
=======================

Features added
--------------

* Declarations for C++ ``std::stop_token`` were added as ``libcpp.stop_token`` to provide
  additional low-level synchronisation primitives.
  (Github issue :issue:`6820`)

Bugs fixed
----------

* Functions with many default values could generate invalid C code.


3.2.10 (2026-09-01)
===================

* A refcount leak was fixed.


3.2.9 (2026-08-01)
==================

* Old fix.
"""

CHANGELOG_MD = """\
# Changelog

## [2.1.0] - 2026-02-01
### Added
- feature two

## [2.0.0] - 2026-01-01
- feature one
"""


class TestChangelogSections(unittest.TestCase):
    def test_rst_range_and_prerelease_folding(self):
        notes = changelog_sections(CHANGES_RST, "3.2.9", "3.3.0")
        self.assertLess(notes.index("## 3.2.10"), notes.index("## 3.3.0"))
        self.assertIn("A refcount leak", notes)
        self.assertIn("std::stop_token", notes.split("## 3.3.0")[1])
        self.assertNotIn("Old fix", notes)
        self.assertNotIn("rc 1 (", notes)

    def test_markdown(self):
        notes = changelog_sections(CHANGELOG_MD, "2.0.0", "2.1.0")
        self.assertEqual(notes, "## 2.1.0\n\n### Added\n- feature two")

    def test_nothing_newer(self):
        self.assertEqual(changelog_sections(CHANGELOG_MD, "2.1.0", "2.1.0"), "")

    def test_budget(self):
        text = "".join(f"## 1.{i}\n\n" + "x" * 5000 + "\n\n" for i in range(1, 5))
        notes = changelog_sections(text, "1.0", "1.4", budget=4000)
        self.assertEqual(notes.count("## 1."), 4)
        self.assertLess(len(notes), 4200)

    def test_fallback_body(self):
        body = release_notes_changelog_body("3.3.0", "3.2.9", changelog_sections(CHANGES_RST, "3.2.9", "3.3.0"))
        self.assertEqual(body[0], "- Updated to version 3.3.0")
        self.assertEqual(body[1], "  * Declarations for C++ std::stop_token were added as")
        self.assertIn("    primitives. (Github issue 6820)", body)
        self.assertIn("- Updated to version 3.2.10", body)
        self.assertFalse(any("Features added" in l or "No functional" in l or "---" in l for l in body))


class TestArchiveReleaseNotes(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)

    def _tar(self, name, files):
        with tarfile.open(self.dir / name, "w:gz") as t:
            for path, text in files.items():
                data = text.encode()
                info = tarfile.TarInfo(path)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))

    def test_top_level_changes_preferred(self):
        self._tar("cython-3.3.0.tar.gz", {"cython-3.3.0/docs/src/changes.rst": "3.3.0\n=====\n\n* docs copy\n",
                                          "cython-3.3.0/CHANGES.rst": CHANGES_RST})
        self._tar("cython-3.2.9.tar.gz", {"cython-3.2.9/CHANGES.rst": "3.3.0\n=====\n\n* wrong archive\n"})
        notes = archive_release_notes(self.dir, "3.2.9", "3.3.0", log=lambda m: None)
        self.assertIn("std::stop_token", notes)
        self.assertNotIn("docs copy", notes)

    def test_zip(self):
        with zipfile.ZipFile(self.dir / "pkg-2.1.0.zip", "w") as z:
            z.writestr("pkg-2.1.0/CHANGELOG.md", CHANGELOG_MD)
        self.assertIn("feature two", archive_release_notes(self.dir, "2.0.0", "2.1.0", log=lambda m: None))

    def test_no_changelog(self):
        self._tar("pkg-1.1.tar.gz", {"pkg-1.1/README": "hi"})
        self.assertEqual(archive_release_notes(self.dir, "1.0", "1.1", log=lambda m: None), "")


if __name__ == "__main__":
    unittest.main()
