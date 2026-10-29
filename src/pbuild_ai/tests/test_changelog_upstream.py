import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, SRC_DIR)
_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules.setdefault('yaml', _yaml)

from pbuild_ai import pbuild_ai as pa
from pbuild_ai.skills.changelog_skill import (
    add_release_url_bullet, clean_release_notes_body, last_changelog_version,
    release_notes_changelog_body, write_changelog_entry,
)

RELEASES = [
    {"tag_name": "v0.8.0", "body": "* eight",
     "html_url": "https://github.com/gufo-org/gufo/releases/tag/v0.8.0"},
    {"tag_name": "v0.7.0", "body": "* seven",
     "html_url": "https://github.com/gufo-org/gufo/releases/tag/v0.7.0"},
    {"tag_name": "v0.6.0", "body": "* six",
     "html_url": "https://github.com/gufo-org/gufo/releases/tag/v0.6.0"},
    {"tag_name": "v0.5.0", "body": "* five",
     "html_url": "https://github.com/gufo-org/gufo/releases/tag/v0.5.0"},
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
    def _fetch(self, spec, old="0.5.0", new="0.7.0"):
        resp = io.BytesIO(json.dumps(RELEASES).encode())
        with patch.object(pa.urllib.request, "urlopen", return_value=resp) as m:
            notes, url = pa._upstream_release_notes(spec, old, new)
        return notes, url, m

    def test_range_from_url_tag(self):
        notes, url, m = self._fetch("Name: gufo\nVersion: 0.7.0\nURL: https://github.com/gufo-org/gufo\n"
                                    "Source: %name-%{version}.tar.gz\n")
        self.assertIn("api.github.com/repos/gufo-org/gufo/releases", m.call_args[0][0].full_url)
        self.assertIn("## 0.7.0", notes)
        self.assertIn("## 0.6.0", notes)
        self.assertNotIn("0.8.0", notes)
        self.assertNotIn("## 0.5.0", notes)
        self.assertEqual(url, "https://github.com/gufo-org/gufo/releases/tag/v0.7.0")

    def test_source_url_with_fragment(self):
        _, _, m = self._fetch("Name: gufo\nVersion: 0.7.0\nSource0: https://github.com/o/gufo/archive/"
                              "refs/tags/v%{version}.tar.gz#/%{name}-%{version}.tar.gz\n")
        self.assertIn("repos/o/gufo/releases", m.call_args[0][0].full_url)

    def test_url_tag_when_source_is_pypi(self):
        _, _, m = self._fetch("Name: python-av\nVersion: 19.0.0\nURL: https://github.com/PyAV-Org/PyAV\n"
                              "Source: https://files.pythonhosted.org/packages/source/a/av/av-%{version}.tar.gz\n")
        self.assertIn("repos/PyAV-Org/PyAV/releases", m.call_args[0][0].full_url)

    def test_releases_page_when_no_release_for_version(self):
        _, url, _ = self._fetch("Name: gufo\nVersion: 0.7.0\nURL: https://github.com/gufo-org/gufo\n",
                                new="0.9.0")
        self.assertEqual(url, "https://github.com/gufo-org/gufo/releases")

    def test_no_project(self):
        self.assertEqual(pa._upstream_release_notes("Name: x\nSource: x.tar.gz\n", "1", "2"),
                         ("", ""))


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
                patch.object(LlmAnalyzer, 'analyze', return_value=''):
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
        self.assertIn("Release notes: https://github.com/gufo-org/gufo/releases/tag/v0.7.0",
                      changes)
        self.assertNotIn("0.8.0", changes)
        self.assertLess(changes.index("0.7.0"), changes.index("0.6.0"))
        self.assertLess(changes.index("0.6.0"), changes.index("version 0.5.0"))


PYTORCH_STYLE_BODY = (
    "# PyTorch 1.0.0 Release Notes\n\n"
    "- [Highlights](#highlights)\n"
    "- [Backwards Incompatible Changes](#bic)\n"
    "- [Deprecations](#deprecations)\n\n"
    "# Highlights\n\n"
    "<table>\n  <tr><td><strong>Flight recorder</strong> for the NCCL "
    "backend becomes the default</td><td>Debug distributed hangs</td></tr>\n"
    "</table>\n\n"
    "# Improvements\n\n- Reduce memory use of torch.compile graphs\n")


class TestCleanReleaseNotesBody(unittest.TestCase):
    def test_toc_dropped_and_html_flattened(self):
        cleaned = clean_release_notes_body(PYTORCH_STYLE_BODY)
        self.assertNotIn("(#highlights)", cleaned)
        self.assertNotIn("[Backwards Incompatible Changes]", cleaned)
        self.assertNotIn("<", cleaned)
        self.assertIn("Flight recorder for the NCCL backend becomes the default",
                      cleaned)

    def test_plain_text_unchanged(self):
        self.assertEqual(clean_release_notes_body("Just a plain line"), "Just a plain line")

    def test_empty(self):
        self.assertEqual(clean_release_notes_body(""), "")
        self.assertEqual(clean_release_notes_body(None), "")

    def test_truncation_keeps_content_behind_toc(self):
        toc = "".join(f"- [Section {i}](#section-{i})\n" for i in range(700))
        body = toc + "\n# Highlights\n\nFix silent corruption on NVMe\n"
        self.assertGreater(len(toc), 10000)
        latest, notes = pa._notes_from_releases(
            [{"tag_name": "v2.0.0", "body": body}], "1.0.0", "body")
        self.assertIn("Fix silent corruption on NVMe", notes)
        self.assertNotIn("(#section-1)", notes)


class TestTocNeverBullets(unittest.TestCase):
    def test_real_highlights_replace_toc(self):
        body = release_notes_changelog_body("1.0.0", "0.9.0",
                                            "## 1.0.0\n\n" + PYTORCH_STYLE_BODY)
        self.assertTrue(any("Flight recorder" in l for l in body), body)
        self.assertTrue(any("memory use of torch.compile" in l for l in body), body)
        self.assertFalse(any("Deprecations" in l or "Highlights" in l for l in body), body)


class TestReleaseNotesUrl(unittest.TestCase):
    URL = "https://github.com/o/p/releases/tag/v2.0"

    def _changes(self, tmp, text):
        path = Path(tmp) / "p.changes"
        path.write_text(text, encoding="utf-8")
        return path

    def test_url_bullet_after_highlights(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._changes(tmp, HEADER + "- Updated to version 1.0\n\n")
            self.assertTrue(write_changelog_entry(path, "1.0", "2.0", "A <a@b.c>",
                                                  release_notes="* Big feature",
                                                  release_url=self.URL))
            text = path.read_text(encoding="utf-8")
            self.assertIn(f"  * Release notes: {self.URL}", text)
            self.assertLess(text.index("* Big feature"), text.index("Release notes:"))

    def test_no_notes_still_links_to_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._changes(tmp, HEADER + "- Updated to version 1.0\n\n")
            self.assertTrue(write_changelog_entry(path, "1.0", "2.0", "A <a@b.c>",
                                                  release_notes="", release_url=self.URL))
            text = path.read_text(encoding="utf-8")
            self.assertIn("- Updated to version 2.0", text)
            self.assertIn(f"  * Release notes: {self.URL}", text)

    def test_add_release_url_bullet(self):
        body = ["- Updated to version 2.0", "  * fix a", "  * fix b",
                "- Updated to version 1.5", "  * old fix"]
        out = add_release_url_bullet(body, "u")
        self.assertEqual(out[3], "  * Release notes: u")
        self.assertEqual(add_release_url_bullet(out, "u"), out)
        self.assertEqual(add_release_url_bullet(body, ""), body)


class TestServiceFileProjectUrl(unittest.TestCase):
    SERVICE_XML = ('<services>\n'
                   '  <service name="tar_scm" mode="manual">\n'
                   '    <param name="scm">git</param>\n'
                   '    <param name="url">https://github.com/pytorch/pytorch</param>\n'
                   '    <param name="revision">v2.14.0</param>\n'
                   '  </service>\n'
                   '</services>\n')

    def _pkg(self, name, url):
        tmp = tempfile.mkdtemp(prefix="pbuild_service_")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, name), "w") as f:
            f.write(self.SERVICE_XML.replace("https://github.com/pytorch/pytorch", url))
        return tmp

    def test_forge_url_from_service_file(self):
        urls = pa._forge_urls_from_service_files(
            self._pkg("_service", "https://github.com/pytorch/pytorch"))
        self.assertEqual(urls, ["https://github.com/pytorch/pytorch"])

    def test_dot_service_file_and_non_forge(self):
        self.assertEqual(
            pa._forge_urls_from_service_files(
                self._pkg(".service", "https://gitlab.com/o/p")),
            ["https://gitlab.com/o/p"])
        self.assertEqual(
            pa._forge_urls_from_service_files(
                self._pkg("_service", "https://code.example.org/o/p")), [])
        self.assertEqual(pa._forge_urls_from_service_files(None), [])

    def test_release_notes_via_service_url(self):
        tmp = self._pkg("_service", "https://github.com/pytorch/pytorch")
        spec = ("Name: python-torch\nVersion: 2.15.0\nURL: https://pytorch.org\n"
                "Source0: pytorch-v%{version}.tar.gz\n")
        releases = [{"tag_name": "v2.15.0", "body": "* big",
                     "html_url": "https://github.com/pytorch/pytorch/releases/tag/v2.15.0"}]
        resp = io.BytesIO(json.dumps(releases).encode())
        with patch.object(pa.urllib.request, "urlopen", return_value=resp) as m:
            notes, url = pa._upstream_release_notes(spec, "2.14.1", "2.15.0", tmp)
        self.assertIn("repos/pytorch/pytorch/releases", m.call_args[0][0].full_url)
        self.assertIn("## 2.15.0", notes)
        self.assertEqual(url, "https://github.com/pytorch/pytorch/releases/tag/v2.15.0")


class TestFetchedUrlAndForgeHints(unittest.TestCase):
    def test_fetched_url(self):
        self.assertEqual(
            pa._fetched_url("web_fetch: [Fetched 12 bytes from https://x/y]"),
            "https://x/y")
        self.assertEqual(pa._fetched_url("[Fetched 12 bytes]"), "")

    def test_forge_hint_api_urls(self):
        self.assertEqual(pa._forge_hint_from_url(
            "https://api.github.com/repos/pytorch/pytorch/releases?per_page=100"),
            "https://github.com/pytorch/pytorch")
        self.assertEqual(pa._forge_hint_from_url(
            "https://gitlab.com/api/v4/projects/o%2Fp/releases"),
            "https://gitlab.com/o/p")
        self.assertEqual(pa._forge_hint_from_url(
            "https://gitlab.com/api/v4/projects/12345/releases"), "")
        self.assertEqual(pa._forge_hint_from_url(
            "https://example.com/api/v3/repos/o/r"), "")

    def test_forge_hint_page_urls(self):
        self.assertEqual(pa._forge_hint_from_url(
            "https://github.com/pytorch/pytorch/releases/tag/v2.15.0"),
            "https://github.com/pytorch/pytorch")
        self.assertEqual(pa._forge_hint_from_url(
            "https://gitlab.com/o/p/-/releases/1.0"), "https://gitlab.com/o/p")
        self.assertEqual(pa._forge_hint_from_url("https://pytorch.org/"), "")
        self.assertEqual(pa._forge_hint_from_url(""), "")

    def test_forge_hint_from_payload(self):
        data = [{"tag_name": "v2.15.0",
                 "html_url": "https://github.com/pytorch/pytorch/releases/tag/v2.15.0"}]
        self.assertEqual(pa._forge_hint_from_payload(data),
                         "https://github.com/pytorch/pytorch")
        self.assertEqual(pa._forge_hint_from_payload({"version": "1.0"}), "")

    def test_extra_urls_used_when_spec_has_none(self):
        spec = "Name: gufo\nVersion: 0.7.0\nURL: https://example.org\n"
        resp = io.BytesIO(json.dumps(RELEASES).encode())
        with patch.object(pa.urllib.request, "urlopen", return_value=resp) as m:
            notes, url = pa._upstream_release_notes(
                spec, "0.5.0", "0.7.0", None,
                extra_urls=["https://github.com/gufo-org/gufo"])
        self.assertIn("repos/gufo-org/gufo/releases", m.call_args[0][0].full_url)
        self.assertIn("## 0.7.0", notes)
        self.assertEqual(url, "https://github.com/gufo-org/gufo/releases/tag/v0.7.0")


class TestResearchReleaseData(unittest.TestCase):
    GH = [
        {"tag_name": "v2.15.0", "body": "* big thing",
         "html_url": "https://github.com/pytorch/pytorch/releases/tag/v2.15.0"},
        {"tag_name": "v2.14.1", "body": "* small fix",
         "html_url": "https://github.com/pytorch/pytorch/releases/tag/v2.14.1"},
    ]

    def test_releases_list(self):
        notes, url = pa._notes_url_from_research_data(self.GH, "2.14.1", "2.15.0")
        self.assertIn("## 2.15.0", notes)
        self.assertIn("big thing", notes)
        self.assertNotIn("## 2.14.1", notes)
        self.assertEqual(url, "https://github.com/pytorch/pytorch/releases/tag/v2.15.0")

    def test_gitlab_list(self):
        data = [{"tag_name": "2.0", "description": "* nice",
                 "html_url": "https://gitlab.com/o/p/-/releases/2.0"}]
        notes, url = pa._notes_url_from_research_data(
            data, "1.0", "2.0", "https://gitlab.com/api/v4/projects/o%2Fp/releases")
        self.assertIn("nice", notes)
        self.assertEqual(url, "https://gitlab.com/o/p/-/releases/2.0")

    def test_single_release(self):
        data = self.GH[0]
        notes, url = pa._notes_url_from_research_data(data, "2.14.1", "2.15.0")
        self.assertIn("big thing", notes)
        self.assertEqual(url, "https://github.com/pytorch/pytorch/releases/tag/v2.15.0")

    def test_non_release_json(self):
        self.assertEqual(
            pa._notes_url_from_research_data({"version": "1.0"}, "0.9", "1.0"),
            ("", ""))
        self.assertEqual(pa._notes_url_from_research_data("text", "0.9", "1.0"),
                         ("", ""))


class TestReleaseHtmlUrl(unittest.TestCase):
    def test_matches_with_and_without_v(self):
        rel = [{"tag_name": "v2.0", "html_url": "u2"}, {"tag_name": "3.0", "html_url": "u3"}]
        self.assertEqual(pa._release_html_url(rel, "2.0"), "u2")
        self.assertEqual(pa._release_html_url(rel, "v3.0"), "u3")

    def test_no_match(self):
        self.assertEqual(pa._release_html_url([{"tag_name": "v1.0"}], "2.0"), "")
        self.assertEqual(pa._release_html_url(None, "2.0"), "")
        self.assertEqual(pa._release_html_url([], ""), "")


class TestForgeHintFromPayload(unittest.TestCase):
    PYPI = {
        "info": {
            "version": "2.15.0",
            "home_page": "",
            "project_urls": {
                "Documentation": "https://pytorch.org/docs",
                "Forum": "https://discuss.pytorch.org",
                "Homepage": "https://pytorch.org",
                "Issue Tracker": "https://github.com/pytorch/pytorch/issues",
                "Repository": "https://github.com/pytorch/pytorch",
            },
        }
    }

    def test_pypi_project_urls(self):
        self.assertEqual(pa._forge_hint_from_payload(self.PYPI),
                         "https://github.com/pytorch/pytorch")

    def test_npm_vcs_url(self):
        self.assertEqual(pa._forge_hint_from_payload(
            {"repository": {"url": "git+https://github.com/o/r.git"}}),
            "https://github.com/o/r")

    def test_preferred_keys_win(self):
        data = {"homepage": "https://gitlab.com/a/b",
                "misc": "https://github.com/other/proj"}
        self.assertEqual(pa._forge_hint_from_payload(data),
                         "https://gitlab.com/a/b")

    def test_no_url_anywhere(self):
        self.assertEqual(pa._forge_hint_from_payload(
            {"version": "1.0", "maintainers": ["someone"]}), "")
        self.assertEqual(pa._forge_hint_from_payload(
            {"home_page": "https://example.org"}), "")


HOME_LISTING = (
    '<directory name="python-torch">'
    '<linkinfo project="science:machinelearning" package="python-torch" '
    'srcmd5="0da8"/>'
    '<entry name="python-torch.spec" md5="48" size="23981"/>'
    '</directory>')
SCIENCE_LISTING = (
    '<directory name="python-torch">'
    '<linkinfo project="openSUSE:Factory" package="python-torch"/>'
    '<entry name="_service" md5="44" size="451"/>'
    '</directory>')
FACTORY_LISTING = (
    '<directory name="python-torch">'
    '<entry name="python-torch.spec" md5="aa" size="1"/>'
    '</directory>')
SERVICE_XML = (
    '<services>\n<service name="tar_scm">\n<param name="url">'
    'https://github.com/pytorch/pytorch.git</param>\n</service>\n</services>')


def _obs_router(responses):
    """urlopen replacement routing requests by full URL."""
    import urllib.error

    def _open(req, timeout=None):
        url = req.full_url
        for frag in sorted(responses, key=len, reverse=True):
            if frag in url:
                return io.BytesIO(responses[frag].encode())
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    return _open


class TestObsOriginUrls(unittest.TestCase):
    def test_service_xml_parsing(self):
        self.assertEqual(
            pa._forge_urls_from_service_xml(SERVICE_XML),
            ["https://github.com/pytorch/pytorch.git"])
        self.assertEqual(pa._forge_urls_from_service_xml(
            '<param name="url">https://example.com/x</param>'), [])
        self.assertEqual(pa._forge_urls_from_service_xml(""), [])

    def test_service_found_on_direct_origin(self):
        router = _obs_router({
            "public/source/home:x:16/python-torch": HOME_LISTING,
            "public/source/science:machinelearning/python-torch/_service": SERVICE_XML,
        })
        with patch.object(pa.urllib.request, "urlopen", side_effect=router):
            urls = pa._forge_urls_from_obs_origin(
                "https://api.opensuse.org", "home:x:16", "python-torch")
        self.assertEqual(urls, ["https://github.com/pytorch/pytorch.git"])

    def test_multi_hop_chain(self):
        router = _obs_router({
            "public/source/home:x:16/python-torch": HOME_LISTING,
            "public/source/science:machinelearning/python-torch": SCIENCE_LISTING,
            "public/source/openSUSE:Factory/python-torch": FACTORY_LISTING,
            "public/source/openSUSE:Factory/python-torch/_service": SERVICE_XML,
        })
        with patch.object(pa.urllib.request, "urlopen", side_effect=router):
            urls = pa._forge_urls_from_obs_origin(
                "https://api.opensuse.org", "home:x:16", "python-torch")
        self.assertEqual(urls, ["https://github.com/pytorch/pytorch.git"])

    def test_cycle_guard(self):
        a = '<directory><linkinfo project="P" package="A"/></directory>'
        b = '<directory><linkinfo project="P" package="B"/></directory>'
        router = _obs_router({"source/P/A": a, "source/P/B": b})
        with patch.object(pa.urllib.request, "urlopen", side_effect=router):
            urls = pa._forge_urls_from_obs_origin("https://api.opensuse.org", "P", "A")
        self.assertEqual(urls, [])

    def test_no_linkinfo(self):
        router = _obs_router({"source/P/A": FACTORY_LISTING})
        with patch.object(pa.urllib.request, "urlopen", side_effect=router):
            urls = pa._forge_urls_from_obs_origin("https://api.opensuse.org", "P", "A")
        self.assertEqual(urls, [])

    def test_obs_origin_urls_needs_osc_mode(self):
        mgr = types.SimpleNamespace(osc_mode=False, osc_project="P",
                                    osc_apiurl="https://api.opensuse.org",
                                    osc_package="A", base_dir="/tmp/A")
        with patch.object(pa.urllib.request, "urlopen") as m:
            self.assertEqual(pa._obs_origin_urls(mgr), [])
            m.assert_not_called()
        self.assertEqual(pa._obs_origin_urls(None), [])

    def test_obs_origin_urls_checkout(self):
        mgr = types.SimpleNamespace(osc_mode=True, osc_project="home:x:16",
                                    osc_apiurl="https://api.opensuse.org",
                                    osc_package="python-torch",
                                    base_dir="/tmp/python-torch")
        router = _obs_router({
            "public/source/home:x:16/python-torch": HOME_LISTING,
            "public/source/science:machinelearning/python-torch/_service": SERVICE_XML,
        })
        with patch.object(pa.urllib.request, "urlopen", side_effect=router):
            urls = pa._obs_origin_urls(mgr)
        self.assertEqual(urls, ["https://github.com/pytorch/pytorch.git"])


class TestForcedVersionAlignment(unittest.TestCase):
    """The mechanical Version bump must keep the 'Version:<padding>' column."""

    def _bump(self, text, version):
        return re.sub(r'^(Version:\s*)\S+',
                      lambda m: f'{m.group(1)}{version}',
                      text, count=1, flags=re.M)

    def test_padding_preserved(self):
        spec = "Name:           python-torch\nVersion:        2.14.1\nRelease:        0\n"
        out = self._bump(spec, "2.15.0")
        self.assertIn("Version:        2.15.0", out)
        self.assertIn("Release:        0", out)


if __name__ == "__main__":
    unittest.main()
