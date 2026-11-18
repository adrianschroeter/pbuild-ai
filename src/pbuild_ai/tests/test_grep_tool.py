"""Tests for the grep tool: literal and regular-expression search inside the
workspace sandbox and inside tar/zip archive members, with include filter,
binary/hidden skipping and caps."""

import io
import os
import sys
import tarfile
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Mock yaml before any pbuild_ai import to prevent ModuleNotFoundError
if 'yaml' not in sys.modules:
    _yaml = types.ModuleType('yaml')
    _yaml.YAMLError = Exception
    sys.modules['yaml'] = _yaml

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.tools import build_tools_list, execute_tool_calls, format_tool_display

SPEC = "Name: foo\nBuildRequires: protobuf21-devel\n%description\nTest.\n"
MAIN_C = "int main(void)\n{\n    return run_protoc();\n}\n"
HIDDEN_SECRET = "BuildRequires: from-hidden-dir\n"


class TestGrepTool(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="pai_grep_")
        (Path(self.ws) / "foo.spec").write_text(SPEC)
        (Path(self.ws) / "src").mkdir()
        (Path(self.ws) / "src" / "main.c").write_text(MAIN_C)
        (Path(self.ws) / "src" / "util.h").write_text("#define PROTOC 1\n")
        (Path(self.ws) / ".hidden").mkdir()
        (Path(self.ws) / ".hidden" / "secret.txt").write_text(HIDDEN_SECRET)
        (Path(self.ws) / "blob.bin").write_bytes(b"needle\x00\x00binary haystack")
        # a corrupt archive: parsing it must fail silently, never abort the scan
        (Path(self.ws) / "broken.tar.gz").write_bytes(b"\x1f\x8b\x08needle")

    def _grep(self, tool_input, manager=None):
        manager = manager or MagicMock()
        out = execute_tool_calls([("grep", tool_input)], manager, self.ws)
        return out[0]

    def test_tool_is_advertised(self):
        fn = next(t for t in build_tools_list()
                  if t["function"]["name"] == "grep")["function"]
        self.assertEqual(fn["parameters"]["required"], ["pattern"])
        self.assertIn("literal", fn["description"])
        self.assertIn("regular expression", fn["description"])

    def test_literal_search_over_workspace(self):
        out = self._grep({"pattern": "protobuf21-devel"})
        self.assertIn("foo.spec:2:", out)
        self.assertIn("BuildRequires: protobuf21-devel", out)

    def test_regex_search(self):
        out = self._grep({"pattern": r"BuildRequires:\s+\S*devel", "regex": True})
        self.assertIn("foo.spec:2:", out)

    def test_regex_false_does_not_interpret_meta_chars(self):
        out = self._grep({"pattern": "main(void)"})
        self.assertIn("src/main.c:1:", out)
        out = self._grep({"pattern": "n.w", "regex": False})
        self.assertTrue(out.startswith("No matches"))

    def test_invalid_regex_reports_error(self):
        out = self._grep({"pattern": "(unclosed", "regex": True})
        self.assertTrue(out.startswith("Error"))
        self.assertIn("invalid regular expression", out)

    def test_ignore_case_defaults_to_true(self):
        out = self._grep({"pattern": "buildrequires"})
        self.assertIn("foo.spec:2:", out)

    def test_case_sensitive_when_disabled(self):
        out = self._grep({"pattern": "buildrequires", "ignore_case": False})
        self.assertTrue(out.startswith("No matches"))
        out = self._grep({"pattern": "BuildRequires", "ignore_case": False})
        self.assertIn("foo.spec:2:", out)

    def test_include_filter(self):
        out = self._grep({"pattern": "protoc", "include": "*.h"})
        self.assertIn("src/util.h:1:", out)
        self.assertNotIn("main.c", out)
        out = self._grep({"pattern": "protoc", "include": "*.spec"})
        self.assertTrue(out.startswith("No matches"))

    def test_single_file_search(self):
        out = self._grep({"pattern": "protoc", "path": "src/main.c"})
        self.assertIn("src/main.c:3:", out)

    def test_hidden_directories_are_skipped(self):
        out = self._grep({"pattern": "from-hidden-dir"})
        self.assertTrue(out.startswith("No matches"))

    def test_build_result_dirs_are_skipped_but_reachable(self):
        build = Path(self.ws) / "_build.tumbleweed.x86_64"
        (build / "root" / "usr").mkdir(parents=True)
        (build / "root" / "usr" / "noisefile").write_text("rootstree-value\n")
        # not searched when walking the workspace
        out = self._grep({"pattern": "rootstree-value"})
        self.assertTrue(out.startswith("No matches"))
        # explicitly pointing at it still works
        out = self._grep({"pattern": "rootstree-value",
                          "path": "_build.tumbleweed.x86_64"})
        self.assertIn("_build.tumbleweed.x86_64/root/usr/noisefile:1:", out)

    def test_binary_files_are_skipped(self):
        out = self._grep({"pattern": "needle"})
        self.assertNotIn("blob.bin", out)
        self.assertNotIn("broken.tar.gz", out)
        self.assertTrue(out.startswith("No matches"))

    def test_no_matches_message(self):
        out = self._grep({"pattern": "absolutely-not-here-xyz"})
        self.assertTrue(out.startswith("No matches"))

    def test_missing_pattern(self):
        out = self._grep({})
        self.assertTrue(out.startswith("Error"))

    def test_unknown_path(self):
        out = self._grep({"pattern": "x", "path": "nope"})
        self.assertTrue(out.startswith("Error"))
        self.assertIn("path not found", out)

    def test_path_traversal_rejected(self):
        out = self._grep({"pattern": "root", "path": "../../etc"})
        self.assertTrue(out.startswith("Error"))
        self.assertIn("outside the workspace", out)

    def test_symlink_to_outside_file_is_not_read(self):
        outside = tempfile.mkdtemp(prefix="pai_grep_outside_")
        secret = Path(outside) / "credentials"
        secret.write_text("password: topsecret-value\n")
        (Path(self.ws) / "leak.txt").symlink_to(secret)
        # a walk of the workspace must not follow the file symlink out
        out = self._grep({"pattern": "topsecret-value"})
        self.assertTrue(out.startswith("No matches"))
        # and pointing path= directly at the link must be rejected
        out = self._grep({"pattern": "topsecret-value", "path": "leak.txt"})
        self.assertTrue(out.startswith("Error"))
        self.assertIn("outside the workspace", out)

    def test_symlinked_directory_is_not_traversed(self):
        outside = tempfile.mkdtemp(prefix="pai_grep_outdir_")
        (Path(outside) / "secret.txt").write_text("dirsecret-value\n")
        (Path(self.ws) / "extdir").symlink_to(outside, target_is_directory=True)
        out = self._grep({"pattern": "dirsecret-value"})
        self.assertTrue(out.startswith("No matches"))

    def test_absolute_path_outside_rejected(self):
        outside = tempfile.mkdtemp(prefix="pai_grep_abs_")
        (Path(outside) / "s.txt").write_text("abssecret-value\n")
        out = self._grep({"pattern": "abssecret-value", "path": str(outside)})
        self.assertTrue(out.startswith("Error"))
        self.assertIn("outside the workspace", out)

    def test_unsafe_path_rejected_by_manager(self):
        manager = MagicMock()
        manager._is_safe_path.return_value = False
        out = self._grep({"pattern": "x"}, manager=manager)
        self.assertTrue(out.startswith("Error"))
        self.assertIn("outside the workspace", out)

    def test_match_cap_truncates(self):
        (Path(self.ws) / "many.txt").write_text("hit\n" * 300)
        with patch('pbuild_ai.tools._GREP_MAX_MATCHES', 5):
            out = self._grep({"pattern": "hit"})
        self.assertIn("truncated", out)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("many.txt:")]), 5)

    def test_display_hides_match_output_keeps_errors(self):
        out = self._grep({"pattern": "protobuf21-devel"})
        self.assertIsNone(format_tool_display("grep", {"pattern": "x"}, out, False))
        err = "Error: grep: missing 'pattern'."
        self.assertEqual(format_tool_display("grep", {}, err, False), err)
        nomatch = "No matches for 'x' in ."
        self.assertEqual(format_tool_display("grep", {}, nomatch, False), nomatch)

    def test_long_lines_are_cut(self):
        (Path(self.ws) / "long.txt").write_text("needle" + "x" * 1000 + "\n")
        out = self._grep({"pattern": "needle"})
        matched_line = next(l for l in out.splitlines() if l.startswith("long.txt:"))
        self.assertLess(len(matched_line), 320)


def _add_tar(path, members, gzipped=True):
    with tarfile.open(path, "w:gz" if gzipped else "w") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


class TestGrepArchives(unittest.TestCase):
    """Tar/zip members are searchable; hits are labelled so they can be
    re-opened with read_file_from_archive."""

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="pai_grep_arc_")
        (Path(self.ws) / "foo.spec").write_text("Name: foo\nSource0: foo-1.0.tar.gz\n")
        self.tar = os.path.join(self.ws, "foo-1.0.tar.gz")
        _add_tar(self.tar, [
            ("foo-1.0/configure", b"#!/bin/sh\n# find protoc here\nexit 0\n"),
            ("foo-1.0/src/main.c", b"int main(void)\n{\n    return call_protoc();\n}\n"),
            ("foo-1.0/blob", b"\x00\x01binary member needle"),
        ])
        self.plain_tar = os.path.join(self.ws, "sub")
        os.makedirs(self.plain_tar)
        self.tar2 = os.path.join(self.plain_tar, "plain.tar")
        _add_tar(self.tar2, [("pkg/Makefile", b"PROG = protoc\n")], gzipped=False)
        self.zip = os.path.join(self.ws, "extra.zip")
        with zipfile.ZipFile(self.zip, "w") as zf:
            zf.writestr("README.md", "# Title\nprotoc is required\n")
            zf.writestr("docs/notes.txt", "unrelated line\n")
            zf.writestr("bin/tool", b"\x00binary zip member")

    def _grep(self, tool_input):
        return execute_tool_calls([("grep", tool_input)], MagicMock(), self.ws)[0]

    def test_tar_gz_members_are_searched(self):
        out = self._grep({"pattern": "find protoc here"})
        self.assertIn("foo-1.0.tar.gz!foo-1.0/configure:2:", out)

    def test_nested_archive_and_plain_tar(self):
        out = self._grep({"pattern": "call_protoc"})
        self.assertIn("foo-1.0.tar.gz!foo-1.0/src/main.c:3:", out)
        out = self._grep({"pattern": "PROG"})
        self.assertIn("sub/plain.tar!pkg/Makefile:1:", out)

    def test_zip_members_are_searched(self):
        out = self._grep({"pattern": "protoc is required"})
        self.assertIn("extra.zip!README.md:2:", out)

    def test_archive_matches_seen_alongside_plain_files(self):
        out = self._grep({"pattern": "protoc"})
        self.assertIn("foo-1.0.tar.gz!foo-1.0/configure:2:", out)
        self.assertIn("extra.zip!README.md:2:", out)
        self.assertIn("sub/plain.tar!pkg/Makefile:1:", out)
        # the spec line mentions the tarball name, not "protoc"
        self.assertNotIn("foo.spec:2:", out)

    def test_path_can_point_directly_at_an_archive(self):
        out = self._grep({"pattern": "find protoc here", "path": "foo-1.0.tar.gz"})
        self.assertIn("foo-1.0.tar.gz!foo-1.0/configure:2:", out)
        out = self._grep({"pattern": "PROG", "path": "sub/plain.tar"})
        self.assertIn("sub/plain.tar!pkg/Makefile:1:", out)

    def test_include_filters_members_by_name(self):
        out = self._grep({"pattern": "protoc", "include": "*.md"})
        self.assertIn("extra.zip!README.md:2:", out)
        self.assertNotIn("configure", out)
        out = self._grep({"pattern": "protoc", "include": "foo-1.0/src/*"})
        self.assertIn("foo-1.0.tar.gz!foo-1.0/src/main.c:3:", out)
        self.assertNotIn("README.md", out)

    def test_binary_members_are_skipped(self):
        out = self._grep({"pattern": "needle"})
        self.assertNotIn("foo-1.0/blob", out)
        self.assertNotIn("bin/tool", out)

    def test_oversized_members_are_skipped(self):
        big = os.path.join(self.ws, "big.tar.gz")
        _add_tar(big, [("pkg/small.txt", b"a protoc line\n"),
                       ("pkg/huge.txt", b"protoc " + b"x" * 4096)])
        with patch('pbuild_ai.tools._GREP_MEMBER_MAX_SIZE', 128):
            out = self._grep({"pattern": "protoc", "path": "big.tar.gz"})
        self.assertIn("big.tar.gz!pkg/small.txt:1:", out)
        self.assertNotIn("huge.txt", out)

    def test_oversized_archive_is_skipped(self):
        with patch('pbuild_ai.tools._GREP_ARCHIVE_MAX_SIZE', 16):
            out = self._grep({"pattern": "find protoc here"})
        self.assertTrue(out.startswith("No matches"))

    def test_corrupt_archive_does_not_break_search(self):
        (Path(self.ws) / "junk.tar.gz").write_bytes(b"\x1f\x8b\x08not-an-archive")
        out = self._grep({"pattern": "find protoc here"})
        self.assertIn("foo-1.0.tar.gz!foo-1.0/configure:2:", out)

    def test_hit_label_can_be_read_back(self):
        """The 'archive!member' label must be usable with read_file_from_archive."""
        out = self._grep({"pattern": "find protoc here"})
        label = next(l for l in out.splitlines() if "configure" in l).split(":")[0]
        archive_path, member = label.split("!", 1)
        content = execute_tool_calls(
            [("read_file_from_archive", {"archive_path": archive_path, "file_path": member})],
            MagicMock(), self.ws)[0]
        self.assertIn("find protoc here", content)

    def test_symlinked_archive_outside_workspace_is_not_read(self):
        outside = tempfile.mkdtemp(prefix="pai_grep_arc_out_")
        secret = os.path.join(outside, "secret.tar.gz")
        _add_tar(secret, [("s/keys", b"password: topsecret-value\n")])
        (Path(self.ws) / "link.tar.gz").symlink_to(secret)
        out = self._grep({"pattern": "topsecret-value"})
        self.assertTrue(out.startswith("No matches"))


if __name__ == "__main__":
    unittest.main()
