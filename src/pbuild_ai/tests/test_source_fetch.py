"""Sources named without a URL (Source: %{name}-%{version}.tar.gz) are
fetched from the tag archive of the project URL after a version update."""

import io
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

from pbuild_ai.source_fetch import archive_url_candidates, expand_spec_macros, fetch_missing_source

SPEC = """Name:           gufo
Version:        0.4.0
URL:            https://github.com/gufo-org/gufo
Source:         %name-%{version}.tar.gz
"""


def _make_tgz(path, top="gufo-0.4.0"):
    with tarfile.open(path, "w:gz") as tf:
        data = b"hello\n"
        info = tarfile.TarInfo(f"{top}/README")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))


class TestCandidates(unittest.TestCase):
    def test_github(self):
        self.assertEqual(archive_url_candidates(SPEC, "0.4.0")[:2], [
            "https://github.com/gufo-org/gufo/archive/refs/tags/v0.4.0.tar.gz",
            "https://github.com/gufo-org/gufo/archive/refs/tags/0.4.0.tar.gz"])

    def test_github_url_with_path_and_git_suffix(self):
        spec = "URL: https://github.com/o/r.git\n"
        self.assertIn("https://github.com/o/r/archive/refs/tags/v1.tar.gz",
                      archive_url_candidates(spec, "1"))
        spec = "URL: https://github.com/o/r/releases\n"
        self.assertIn("https://github.com/o/r/archive/refs/tags/v1.tar.gz",
                      archive_url_candidates(spec, "1"))

    def test_gitlab_and_codeberg(self):
        self.assertIn("https://gitlab.com/g/sub/p/-/archive/v2/p-v2.tar.gz",
                      archive_url_candidates("URL: https://gitlab.com/g/sub/p\n", "2"))
        self.assertIn("https://codeberg.org/o/r/archive/v2.tar.gz",
                      archive_url_candidates("URL: https://codeberg.org/o/r\n", "2"))

    def test_unknown_host_gives_nothing(self):
        self.assertEqual(archive_url_candidates("URL: https://example.org/\n", "1"), [])


class TestFetchMissingSource(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.spec = self.dir / "gufo.spec"
        self.spec.write_text(SPEC)
        self.tried = []
        self.log = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _download(self, works_for):
        def download(url, dest):
            self.tried.append(url)
            if url in works_for:
                _make_tgz(dest)
                return True
            return False
        return download

    def test_tries_tags_and_stores_under_spec_name(self):
        ok = fetch_missing_source(self.spec, "gufo-0.4.0.tar.gz", "0.4.0", self._download(
            {"https://github.com/gufo-org/gufo/archive/refs/tags/0.4.0.tar.gz"}), log=self.log.append)
        self.assertTrue(ok)
        self.assertEqual(len(self.tried), 2)
        self.assertTrue(tarfile.is_tarfile(self.dir / "gufo-0.4.0.tar.gz"))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["gufo-0.4.0.tar.gz", "gufo.spec"])

    def test_recompresses_to_xz(self):
        ok = fetch_missing_source(self.spec, "gufo-0.4.0.tar.xz", "0.4.0", self._download(
            {"https://github.com/gufo-org/gufo/archive/refs/tags/v0.4.0.tar.gz"}), log=self.log.append)
        self.assertTrue(ok)
        with tarfile.open(self.dir / "gufo-0.4.0.tar.xz", "r:xz") as tf:
            self.assertEqual(tf.getnames(), ["gufo-0.4.0/README"])

    def test_existing_file_is_kept(self):
        (self.dir / "gufo-0.4.0.tar.gz").write_bytes(b"x")
        self.assertTrue(fetch_missing_source(self.spec, "gufo-0.4.0.tar.gz", "0.4.0",
                                             self._download(set()), log=self.log.append))
        self.assertEqual(self.tried, [])

    def test_failure_is_reported(self):
        self.assertFalse(fetch_missing_source(self.spec, "gufo-0.4.0.tar.gz", "0.4.0",
                                              self._download(set()), log=self.log.append))
        self.assertIn("Could not download", self.log[-1])
        self.assertFalse(any(p.name.startswith(".") for p in self.dir.iterdir()))

    def test_no_project_url(self):
        self.spec.write_text("Name: x\nVersion: 1\nSource: x-1.tar.gz\n")
        self.assertFalse(fetch_missing_source(self.spec, "x-1.tar.gz", "1",
                                              self._download(set()), log=self.log.append))
        self.assertIn("no known project URL", self.log[-1])


if __name__ == "__main__":
    unittest.main()


class TestExpandSpecMacros(unittest.TestCase):
    SPEC = "Name:           gufo\nVersion:        0.4.0\n%global tag v%{version}\n"

    def test_brace_and_bare_forms(self):
        for src in ("%{name}-%{version}.tar.gz", "%{name}-%version.tar.gz",
                    "%name-%version.tar.gz", "%{name}-%{?version}.tar.gz"):
            self.assertEqual(expand_spec_macros(src, self.SPEC), "gufo-0.4.0.tar.gz", src)

    def test_override_and_nested(self):
        self.assertEqual(expand_spec_macros("%{name}-%{tag}.tar.gz", self.SPEC, version="0.7.0"),
                         "gufo-v0.7.0.tar.gz")

    def test_unknown_macro_kept(self):
        self.assertEqual(expand_spec_macros("%{foo}-%version", self.SPEC), "%{foo}-0.4.0")
