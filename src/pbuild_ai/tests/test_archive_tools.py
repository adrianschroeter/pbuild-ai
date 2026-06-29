"""Tests for list_archive and read_file_from_archive tools."""

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

# Mock yaml before any pbuild_ai import
_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules['yaml'] = _yaml

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.tools import execute_tool_calls


def _make_manager(tmpdir):
    from pbuild_ai.workspace import RpmSourceManager
    return RpmSourceManager(tmpdir, do_clean=False)


def _call(tool_calls, tmpdir):
    manager = _make_manager(tmpdir)
    return execute_tool_calls(tool_calls, manager, tmpdir)


def _create_tar(archive_path, files):
    """Create a tar.gz archive with given files dict (path -> content)."""
    with tarfile.open(archive_path, 'w:gz') as tar:
        for fpath, content in files.items():
            info = tarfile.TarInfo(name=fpath)
            data = content.encode('utf-8') if isinstance(content, str) else content
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def _create_tar_with_symlink(archive_path, target, link_name):
    """Create a tar.gz with a symlink."""
    with tarfile.open(archive_path, 'w:gz') as tar:
        info = tarfile.TarInfo(name=link_name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        tar.addfile(info)


def _create_zip(archive_path, files):
    """Create a zip archive with given files dict (path -> content)."""
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fpath, content in files.items():
            data = content.encode('utf-8') if isinstance(content, str) else content
            zf.writestr(fpath, data)


class TestListArchive(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="archive_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _path(self, name):
        return os.path.join(self.tmpdir, name)

    # --- list_archive: tar.gz ---

    def test_list_tar_gz(self):
        ap = self._path("test.tar.gz")
        _create_tar(ap, {
            "pkg-1.0/": "",
            "pkg-1.0/Makefile": "all:\n",
            "pkg-1.0/src/main.c": "int main(void) { return 0; }\n",
        })
        calls = [("list_archive", {"archive_path": "test.tar.gz"})]
        results = _call(calls, self.tmpdir)
        self.assertIn("pkg-1.0/", results[0])
        self.assertIn("pkg-1.0/Makefile", results[0])
        self.assertIn("pkg-1.0/src/main.c", results[0])

    # --- list_archive: zip ---

    def test_list_zip(self):
        ap = self._path("test.zip")
        _create_zip(ap, {
            "pkg-1.0/": "",
            "pkg-1.0/setup.py": "from setuptools import setup\n",
        })
        calls = [("list_archive", {"archive_path": "test.zip"})]
        results = _call(calls, self.tmpdir)
        self.assertIn("pkg-1.0/", results[0])
        self.assertIn("pkg-1.0/setup.py", results[0])

    # --- list_archive: empty tar ---

    def test_list_empty_tar(self):
        ap = self._path("empty.tar.gz")
        _create_tar(ap, {})
        calls = [("list_archive", {"archive_path": "empty.tar.gz"})]
        results = _call(calls, self.tmpdir)
        self.assertEqual(results[0], "(empty archive)")

    # --- list_archive: unsupported format ---

    def test_list_unsupported_format(self):
        Path(self._path("data.bin")).write_bytes(b"not an archive")
        calls = [("list_archive", {"archive_path": "data.bin"})]
        results = _call(calls, self.tmpdir)
        self.assertIn("Unsupported archive format", results[0])

    # --- list_archive: file not found ---

    def test_list_not_found(self):
        calls = [("list_archive", {"archive_path": "nonexistent.tar.gz"})]
        results = _call(calls, self.tmpdir)
        self.assertIn("Archive not found", results[0])

    # --- list_archive: outside workspace ---

    def test_list_outside_workspace(self):
        calls = [("list_archive", {"archive_path": "/etc/passwd"})]
        results = _call(calls, self.tmpdir)
        self.assertIn("outside the workspace", results[0])

    # --- list_archive: missing path argument ---

    def test_list_missing_arg(self):
        calls = [("list_archive", {})]
        results = _call(calls, self.tmpdir)
        self.assertIn("requires an 'archive_path'", results[0])


class TestReadFileFromArchive(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="archive_read_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _path(self, name):
        return os.path.join(self.tmpdir, name)

    # --- read_file_from_archive: tar.gz ---

    def test_read_tar_gz(self):
        ap = self._path("test.tar.gz")
        _create_tar(ap, {"pkg/Makefile": "all:\n\techo done\n"})
        calls = [("read_file_from_archive", {"archive_path": "test.tar.gz", "file_path": "pkg/Makefile"})]
        results = _call(calls, self.tmpdir)
        self.assertEqual(results[0], "all:\n\techo done\n")

    # --- read_file_from_archive: zip ---

    def test_read_zip(self):
        ap = self._path("test.zip")
        _create_zip(ap, {"pkg/setup.py": "VERSION = '1.0'\n"})
        calls = [("read_file_from_archive", {"archive_path": "test.zip", "file_path": "pkg/setup.py"})]
        results = _call(calls, self.tmpdir)
        self.assertEqual(results[0], "VERSION = '1.0'\n")

    # --- read_file_from_archive: tar.bz2 ---

    def test_read_tar_bz2(self):
        ap = self._path("test.tar.bz2")
        with tarfile.open(ap, 'w:bz2') as tar:
            info = tarfile.TarInfo(name="pkg/VERSION")
            data = b"2.0\n"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        calls = [("read_file_from_archive", {"archive_path": "test.tar.bz2", "file_path": "pkg/VERSION"})]
        results = _call(calls, self.tmpdir)
        self.assertEqual(results[0], "2.0\n")

    # --- read_file_from_archive: tar.xz ---

    def test_read_tar_xz(self):
        ap = self._path("test.tar.xz")
        with tarfile.open(ap, 'w:xz') as tar:
            info = tarfile.TarInfo(name="pkg/VERSION")
            data = b"3.0\n"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        calls = [("read_file_from_archive", {"archive_path": "test.tar.xz", "file_path": "pkg/VERSION"})]
        results = _call(calls, self.tmpdir)
        self.assertEqual(results[0], "3.0\n")

    # --- read_file_from_archive: file not found inside archive ---

    def test_file_not_found_inside(self):
        ap = self._path("test.tar.gz")
        _create_tar(ap, {"a.txt": "hello\n"})
        calls = [("read_file_from_archive", {"archive_path": "test.tar.gz", "file_path": "b.txt"})]
        results = _call(calls, self.tmpdir)
        self.assertIn("File not found in archive", results[0])

    # --- read_file_from_archive: archive not found ---

    def test_archive_not_found(self):
        calls = [("read_file_from_archive", {"archive_path": "nonexistent.tar.gz", "file_path": "a.txt"})]
        results = _call(calls, self.tmpdir)
        self.assertIn("Archive not found", results[0])

    # --- read_file_from_archive: missing args ---

    def test_missing_archive_path(self):
        calls = [("read_file_from_archive", {"file_path": "a.txt"})]
        results = _call(calls, self.tmpdir)
        self.assertIn("requires 'archive_path'", results[0])

    def test_missing_file_path(self):
        calls = [("read_file_from_archive", {"archive_path": "x.tar.gz"})]
        results = _call(calls, self.tmpdir)
        self.assertIn("requires a 'file_path'", results[0])

    # --- path traversal inside archive: ../ ---

    def test_path_traversal_rejected(self):
        ap = self._path("test.tar.gz")
        _create_tar(ap, {"safe.txt": "ok\n"})
        calls = [("read_file_from_archive", {"archive_path": "test.tar.gz", "file_path": "../etc/passwd"})]
        results = _call(calls, self.tmpdir)
        self.assertIn("Invalid path", results[0])

    # --- path traversal inside archive: absolute ---

    def test_absolute_path_rejected(self):
        ap = self._path("test.tar.gz")
        _create_tar(ap, {"safe.txt": "ok\n"})
        calls = [("read_file_from_archive", {"archive_path": "test.tar.gz", "file_path": "/etc/passwd"})]
        results = _call(calls, self.tmpdir)
        self.assertIn("Invalid path", results[0])

    # --- path traversal inside archive: nested ../ ---

    def test_nested_traversal_rejected(self):
        ap = self._path("test.tar.gz")
        _create_tar(ap, {"safe.txt": "ok\n"})
        calls = [("read_file_from_archive", {"archive_path": "test.tar.gz", "file_path": "pkg/../../etc/passwd"})]
        results = _call(calls, self.tmpdir)
        self.assertIn("Invalid path", results[0])

    # --- symlink rejected in tar ---

    def test_symlink_rejected_tar(self):
        ap = self._path("test.tar.gz")
        _create_tar_with_symlink(ap, "/etc/passwd", "pkg/link")
        calls = [("read_file_from_archive", {"archive_path": "test.tar.gz", "file_path": "pkg/link"})]
        results = _call(calls, self.tmpdir)
        self.assertIn("symlink/hardlink", results[0])

    # --- unsupported format ---

    def test_unsupported_format(self):
        Path(self._path("data.bin")).write_bytes(b"not an archive")
        calls = [("read_file_from_archive", {"archive_path": "data.bin", "file_path": "a.txt"})]
        results = _call(calls, self.tmpdir)
        self.assertIn("Unsupported archive format", results[0])

    # --- directory read fails ---

    def test_read_directory_fails(self):
        ap = self._path("test.tar.gz")
        _create_tar(ap, {"pkg/": "", "pkg/a.txt": "content\n"})
        calls = [("read_file_from_archive", {"archive_path": "test.tar.gz", "file_path": "pkg/"})]
        results = _call(calls, self.tmpdir)
        self.assertIn("File not found in archive", results[0])

    # --- outside workspace ---

    def test_outside_workspace(self):
        calls = [("read_file_from_archive", {"archive_path": "/etc/passwd", "file_path": "x"})]
        results = _call(calls, self.tmpdir)
        self.assertIn("outside the workspace", results[0])


if __name__ == "__main__":
    unittest.main()
