"""Tests for _extract_source_tarball_hint: source tarball path extraction for %files failures."""

import os
import sys
import types
import unittest
from pathlib import Path

# Mock yaml before any pbuild_ai import
_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules['yaml'] = _yaml

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.pbuild_ai import _extract_source_tarball_hint


class TestExtractSourceTarballHint(unittest.TestCase):

    def test_url_source_with_macros(self):
        spec = "Name: gawk\nVersion: 5.4.0\nSource: https://ftp.gnu.org/gnu/%{name}/%{name}-%{version}.tar.xz"
        result = _extract_source_tarball_hint(spec, Path("/ws/gawk.spec"), "/ws")
        self.assertIn("gawk-5.4.0.tar.xz", result)
        self.assertIn('archive_path="gawk-5.4.0.tar.xz"', result)

    def test_source0_with_macros(self):
        spec = "Name: foo\nVersion: 1.2.3\nSource0: http://example.com/pkg-%{version}.tar.gz"
        result = _extract_source_tarball_hint(spec, Path("/ws/foo.spec"), "/ws")
        self.assertIn("pkg-1.2.3.tar.gz", result)
        self.assertIn('archive_path="pkg-1.2.3.tar.gz"', result)

    def test_subdirectory_spec(self):
        spec = "Name: foo\nVersion: 1.0\nSource: https://example.com/foo-1.0.tar.bz2"
        result = _extract_source_tarball_hint(spec, Path("/ws/subdir/foo.spec"), "/ws")
        self.assertIn("subdir/foo-1.0.tar.bz2", result)
        self.assertIn('archive_path="subdir/foo-1.0.tar.bz2"', result)

    def test_no_macros_in_url(self):
        spec = "Name: foo\nVersion: 1.0\nSource: https://example.com/myproject-3.2.tar.gz"
        result = _extract_source_tarball_hint(spec, Path("/ws/foo.spec"), "/ws")
        self.assertIn("myproject-3.2.tar.gz", result)

    def test_tar_xz_extension(self):
        spec = "Name: bar\nVersion: 2.0\nSource: bar-2.0.tar.xz"
        result = _extract_source_tarball_hint(spec, Path("/ws/bar.spec"), "/ws")
        self.assertIn("bar-2.0.tar.xz", result)

    def test_zip_extension(self):
        spec = "Name: baz\nVersion: 0.1\nSource: https://example.com/baz-0.1.zip"
        result = _extract_source_tarball_hint(spec, Path("/ws/baz.spec"), "/ws")
        self.assertIn("baz-0.1.zip", result)

    def test_no_source_line(self):
        spec = "Name: foo\nVersion: 1.0\n%description\nTest"
        result = _extract_source_tarball_hint(spec, Path("/ws/foo.spec"), "/ws")
        self.assertEqual(result, "")

    def test_non_tarball_source(self):
        spec = "Name: foo\nVersion: 1.0\nSource: README.txt"
        result = _extract_source_tarball_hint(spec, Path("/ws/foo.spec"), "/ws")
        self.assertEqual(result, "")

    def test_empty_spec_content(self):
        result = _extract_source_tarball_hint("", Path("/ws/foo.spec"), "/ws")
        self.assertEqual(result, "")

    def test_none_spec_content(self):
        result = _extract_source_tarball_hint(None, Path("/ws/foo.spec"), "/ws")
        self.assertEqual(result, "")

    def test_no_workspace_dir(self):
        spec = "Name: gawk\nVersion: 5.4.0\nSource: https://ftp.gnu.org/gnu/%{name}/%{name}-%{version}.tar.xz"
        result = _extract_source_tarball_hint(spec, Path("/anywhere/gawk.spec"), "")
        self.assertIn("gawk-5.4.0.tar.xz", result)

    def test_spec_not_relative_to_workspace(self):
        spec = "Name: foo\nVersion: 1.0\nSource: foo-1.0.tar.gz"
        result = _extract_source_tarball_hint(spec, Path("/other/dir/foo.spec"), "/ws")
        self.assertIn("foo-1.0.tar.gz", result)

    def test_multiple_source_lines_picks_first(self):
        spec = ("Name: foo\nVersion: 1.0\n"
                "Source0: foo-1.0.tar.gz\n"
                "Source1: extra-1.0.tar.gz")
        result = _extract_source_tarball_hint(spec, Path("/ws/foo.spec"), "/ws")
        self.assertIn("foo-1.0.tar.gz", result)
        self.assertNotIn("extra-1.0.tar.gz", result)

    def test_case_insensitive_source_tag(self):
        spec = "Name: foo\nVersion: 1.0\nsource: foo-1.0.tar.gz"
        result = _extract_source_tarball_hint(spec, Path("/ws/foo.spec"), "/ws")
        self.assertIn("foo-1.0.tar.gz", result)

    def test_hint_contains_read_file_from_archive_instruction(self):
        spec = "Name: foo\nVersion: 1.0\nSource: foo-1.0.tar.gz"
        result = _extract_source_tarball_hint(spec, Path("/ws/foo.spec"), "/ws")
        self.assertIn("read_file_from_archive", result)

    def test_hint_contains_local_tarball_label(self):
        spec = "Name: foo\nVersion: 1.0\nSource: foo-1.0.tar.gz"
        result = _extract_source_tarball_hint(spec, Path("/ws/foo.spec"), "/ws")
        self.assertIn("Local source tarball:", result)


if __name__ == '__main__':
    unittest.main()
