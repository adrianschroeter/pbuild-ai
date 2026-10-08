import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import patch

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, SRC_DIR)
_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules.setdefault('yaml', _yaml)

from pbuild_ai import pbuild_ai as pa
from pbuild_ai.skills.changelog_skill import last_changelog_version, release_notes_changelog_body

RELEASES = [
    {"tag_name": "v0.8.0", "body": "* eight"},
    {"tag_name": "v0.7.0", "body": "* seven"},
    {"tag_name": "v0.6.0", "body": "* six"},
    {"tag_name": "v0.5.0", "body": "* five"},
]
HEADER = ("-------------------------------------------------------------------\n"
          "Mon Jan  1 00:00:00 UTC 2026 - A <a@b.c>\n\n")


class TestLastChangelogVersion(unittest.TestCase):
    def test_first_update_line_wins(self):
        text = HEADER + "- Updated to version 0.5.0\n  * x\n\n" + HEADER + "- Update to 0.4.0\n"
        self.assertEqual(last_changelog_version(text), "0.5.0")

    def test_initial_package(self):
        self.assertEqual(last_changelog_version(HEADER + "- Initial package of gufo 0.4.0\n"), "0.4.0")

    def test_none(self):
        self.assertIsNone(last_changelog_version(HEADER + "- Fix build\n"))
        self.assertIsNone(last_changelog_version(""))


class TestUpstreamReleaseNotes(unittest.TestCase):
    def _fetch(self, spec):
        resp = io.BytesIO(json.dumps(RELEASES).encode())
        with patch.object(pa.urllib.request, "urlopen", return_value=resp) as m:
            notes = pa._upstream_release_notes(spec, "0.5.0", "0.7.0")
        return notes, m

    def test_range_from_url_tag(self):
        notes, m = self._fetch("Name: gufo\nVersion: 0.7.0\nURL: https://github.com/gufo-org/gufo\n"
                               "Source: %name-%{version}.tar.gz\n")
        self.assertIn("api.github.com/repos/gufo-org/gufo/releases", m.call_args[0][0].full_url)
        self.assertIn("## 0.7.0", notes)
        self.assertIn("## 0.6.0", notes)
        self.assertNotIn("0.8.0", notes)
        self.assertNotIn("## 0.5.0", notes)

    def test_source_url_with_fragment(self):
        _, m = self._fetch("Name: gufo\nVersion: 0.7.0\nSource0: https://github.com/o/gufo/archive/"
                           "refs/tags/v%{version}.tar.gz#/%{name}-%{version}.tar.gz\n")
        self.assertIn("repos/o/gufo/releases", m.call_args[0][0].full_url)

    def test_url_tag_when_source_is_pypi(self):
        _, m = self._fetch("Name: python-av\nVersion: 19.0.0\nURL: https://github.com/PyAV-Org/PyAV\n"
                           "Source: https://files.pythonhosted.org/packages/source/a/av/av-%{version}.tar.gz\n")
        self.assertIn("repos/PyAV-Org/PyAV/releases", m.call_args[0][0].full_url)

    def test_no_project(self):
        self.assertEqual(pa._upstream_release_notes("Name: x\nSource: x.tar.gz\n", "1", "2"), "")


class TestReleasePleaseNotes(unittest.TestCase):
    """Headings of release-please notes are not changes."""

    def test_headings_dropped(self):
        notes = ("## 0.6.0\n\n## [0.6.0](https://x/compare) (2026-10-03)\n\n### Features\n\n"
                 "* share in-flight prefixes between concurrent requests ([#382](https://x/382))\n\n"
                 "### Bug Fixes\n\n* exit and report device_lost when the GPU context is lost, "
                 "which happened on some cards ([#390](https://x/390))\n")
        body = release_notes_changelog_body("0.6.0", "0.5.0", notes)
        self.assertEqual(body[:2], ["- Updated to version 0.6.0",
                                    "  * share in-flight prefixes between concurrent requests (#382)"])
        self.assertFalse(any("Features" in l or "2026" in l or "Bug Fixes" in l for l in body))
        self.assertTrue(all(len(l) <= 70 for l in body))


class _FakeManager:
    def read_file_safe(self, path):
        return path.read_text()


class TestAiRoundSalvage(unittest.TestCase):
    """An AI edit that replaced the old first entry keeps its new entry only."""

    def test_old_entry_restored(self):
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        changes = tmp / "gufo.changes"
        old = HEADER + "- Updated to version 0.4.0\n  * four\n\n"
        changes.write_text(old)
        new_entry = HEADER + "- Updated to version 0.5.0\n  * five\n"

        class AI:
            def call_with_tools(self, *a, **k):
                changes.write_text(new_entry + "\n")
                return ["done"]
        ok = pa._changelog_ai_round(AI(), changes, "0.4.0", "0.5.0", "## 0.5.0\n* five",
                                    "T <t@e.st>", _FakeManager(), [], str(tmp), False)
        self.assertTrue(ok)
        text = changes.read_text()
        self.assertTrue(text.endswith("- Updated to version 0.5.0\n  * five\n\n" + old), text)
        self.assertNotIn("Jan  1", text.split("0.5.0")[0])


class TestChangelogMode(unittest.TestCase):
    """--changelog covers every upstream release since the last .changes version."""

    def test_entries_for_intermediate_releases(self):
        tmpdir = tempfile.mkdtemp(prefix="pbuild_changelog_")
        self.addCleanup(__import__("shutil").rmtree, tmpdir, True)
        with open(os.path.join(tmpdir, "gufo.spec"), "w") as f:
            f.write("Name: gufo\nVersion: 0.7.0\nURL: https://github.com/gufo-org/gufo\n"
                    "Source: %name-%{version}.tar.gz\n\n%description\nTest.\n")
        with open(os.path.join(tmpdir, "gufo.changes"), "w") as f:
            f.write(HEADER + "- Updated to version 0.5.0\n  * five\n\n")
        script = textwrap.dedent(f"""\
        import io, json, sys, types
        from unittest.mock import patch
        _yaml = types.ModuleType('yaml'); _yaml.YAMLError = Exception
        sys.modules['yaml'] = _yaml
        sys.path.insert(0, {SRC_DIR!r})
        sys.argv = ["pbuild-ai", "--changelog", "--email", "T <t@e.st>", {tmpdir!r}]
        from pbuild_ai.llm_client import LlmAnalyzer
        import pbuild_ai.pbuild_ai as pa
        resp = io.BytesIO(json.dumps({RELEASES!r}).encode())
        with patch.object(pa.urllib.request, 'urlopen', return_value=resp), \\
                patch.object(LlmAnalyzer, 'call_with_tools', return_value=[]):
            pa.main()
        """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                timeout=30, cwd=tmpdir,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with open(os.path.join(tmpdir, "gufo.changes")) as f:
            changes = f.read()
        self.assertIn("ends at 0.5.0", result.stdout)
        self.assertIn("- Updated to version 0.7.0", changes)
        self.assertIn("- Updated to version 0.6.0", changes)
        self.assertIn("* seven", changes)
        self.assertIn("* six", changes)
        self.assertNotIn("0.8.0", changes)
        self.assertLess(changes.index("0.7.0"), changes.index("0.6.0"))
        self.assertLess(changes.index("0.6.0"), changes.index("version 0.5.0"))


if __name__ == "__main__":
    unittest.main()
