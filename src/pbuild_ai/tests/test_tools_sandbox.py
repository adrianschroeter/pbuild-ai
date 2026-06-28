"""Unit tests for tool sandbox: files outside workspace are blocked, git push is blocked."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.utils import resolve_path
from pbuild_ai.workspace import RpmSourceManager
from pbuild_ai.tools import execute_tool_calls


class TestResolvePath(unittest.TestCase):
    """Test the path resolution sandbox (resolve_path returns None for escapes)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="pbuild_test_")
        self.ws = self.tmpdir

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_relative_path_within_workspace(self):
        Path(self.ws, "foo.txt").write_text("hello")
        result = resolve_path("foo.txt", self.ws)
        self.assertIsNotNone(result)
        self.assertEqual(result, Path(self.ws, "foo.txt").resolve())

    def test_absolute_path_within_workspace(self):
        Path(self.ws, "bar.txt").write_text("hello")
        result = resolve_path(str(Path(self.ws, "bar.txt")), self.ws)
        self.assertIsNotNone(result)

    def test_dotdot_escape_blocked(self):
        result = resolve_path("../etc/passwd", self.ws)
        self.assertIsNone(result)

    def test_absolute_path_outside_blocked(self):
        result = resolve_path("/etc/passwd", self.ws)
        self.assertIsNone(result)

    def test_multiple_dotdot_escape_blocked(self):
        result = resolve_path("../../tmp/foo", self.ws)
        self.assertIsNone(result)

    def test_symlink_escape_blocked(self):
        outside = Path(self.ws, "..", "escape_target.txt")
        outside.write_text("should not be accessible")
        link = Path(self.ws, "evil_link")
        try:
            link.symlink_to(outside.resolve())
            result = resolve_path("evil_link", self.ws)
            self.assertIsNone(result)
        except OSError:
            self.skipTest("symlink creation not supported on this platform")

    def test_path_with_null_byte_blocked(self):
        result = resolve_path("safe\x00/etc/passwd", self.ws)
        self.assertIsNone(result)


class TestIsSafePath(unittest.TestCase):
    """Test RpmSourceManager._is_safe_path sandbox enforcement."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="pbuild_test_")
        self.manager = RpmSourceManager(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_file_inside_workspace_is_safe(self):
        f = Path(self.tmpdir, "inside.txt")
        f.write_text("safe")
        self.assertTrue(self.manager._is_safe_path(f))

    def test_file_outside_workspace_is_unsafe(self):
        f = Path("/etc/passwd")
        self.assertFalse(self.manager._is_safe_path(f))

    def test_parent_dir_is_unsafe(self):
        f = Path(self.tmpdir, "..")
        self.assertFalse(self.manager._is_safe_path(f))

    def test_unrelated_path_is_unsafe(self):
        f = Path("/tmp/unrelated")
        self.assertFalse(self.manager._is_safe_path(f))


class TestExecuteToolCallsSandbox(unittest.TestCase):
    """Test that execute_tool_calls blocks file access outside workspace."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="pbuild_test_")
        self.ws = Path(self.tmpdir).resolve()
        self.manager = RpmSourceManager(str(self.ws))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _safe_file(self, name="safe.txt", content="safe content"):
        p = self.ws / name
        p.write_text(content)
        return p

    def _unsafe_path(self, name="../outside.txt"):
        return name

    # -- read_file --

    def test_read_file_blocked_outside_workspace(self):
        results = execute_tool_calls(
            [("read_file", {"path": "/etc/passwd"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("outside the workspace", results[0])

    def test_read_file_dotdot_blocked(self):
        results = execute_tool_calls(
            [("read_file", {"path": "../outside.txt"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("outside the workspace", results[0])

    def test_read_file_allowed_inside(self):
        self._safe_file("valid.txt")
        results = execute_tool_calls(
            [("read_file", {"path": "valid.txt"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("Error", results[0])
        self.assertIn("safe content", results[0])

    # -- write_file --

    def test_write_file_absolute_outside_maps_to_basename(self):
        results = execute_tool_calls(
            [("write_file", {"path": "/tmp/evil.txt", "content": "pwned"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("Error", results[0])
        self.assertTrue((self.ws / "evil.txt").exists())

    def test_write_file_dotdot_maps_to_basename(self):
        results = execute_tool_calls(
            [("write_file", {"path": "../evil.txt", "content": "pwned"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("Error", results[0])
        self.assertTrue((self.ws / "evil.txt").exists())

    def test_write_file_allowed_inside(self):
        results = execute_tool_calls(
            [("write_file", {"path": "newfile.txt", "content": "new content"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("Error", results[0])
        self.assertIn("Wrote", results[0])
        self.assertTrue((self.ws / "newfile.txt").exists())

    def test_write_file_escapes_symlink_in_path(self):
        link_dir = self.ws / "linkdir"
        try:
            link_dir.symlink_to(self.tmpdir, target_is_directory=True)
        except OSError:
            self.skipTest("symlink not supported")
        results = execute_tool_calls(
            [("write_file", {"path": "linkdir/../outside.txt", "content": "pwned"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("Error", results[0])
        # Writes to workspace/outside.txt via basename fallback, not via the symlink

    # -- edit_file --

    def test_edit_file_blocked_outside_workspace(self):
        results = execute_tool_calls(
            [("edit_file", {"path": "/etc/passwd", "old_string": "root", "new_string": "pwned"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("outside the workspace", results[0])

    def test_edit_file_dotdot_blocked(self):
        results = execute_tool_calls(
            [("edit_file", {"path": "../outside.txt", "old_string": "x", "new_string": "y"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("outside the workspace", results[0])

    def test_edit_file_allowed_inside(self):
        self._safe_file("editable.txt", "replace me")
        results = execute_tool_calls(
            [("edit_file", {"path": "editable.txt", "old_string": "replace me", "new_string": "done"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("Error", results[0])
        self.assertIn("Edited", results[0])

    # -- remove_file --

    def test_remove_file_blocked_outside_workspace(self):
        results = execute_tool_calls(
            [("remove_file", {"path": "/etc/passwd"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("outside the workspace", results[0])

    def test_remove_file_dotdot_blocked(self):
        results = execute_tool_calls(
            [("remove_file", {"path": "../outside.txt"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("outside the workspace", results[0])

    def test_remove_file_allowed_inside(self):
        self._safe_file("todelete.txt")
        results = execute_tool_calls(
            [("remove_file", {"path": "todelete.txt"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("Error", results[0])
        self.assertIn("Removed", results[0])

    # -- rename_file --

    def test_rename_file_source_outside_blocked(self):
        results = execute_tool_calls(
            [("rename_file", {"source": "/etc/passwd", "destination": "passwd.txt"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("outside the workspace", results[0])

    def test_rename_file_dest_outside_maps_to_basename(self):
        self._safe_file("source.txt")
        results = execute_tool_calls(
            [("rename_file", {"source": "source.txt", "destination": "/tmp/leak.txt"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("Error", results[0])

    def test_rename_file_allowed_inside(self):
        self._safe_file("old.txt", "rename me")
        results = execute_tool_calls(
            [("rename_file", {"source": "old.txt", "destination": "new.txt"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("Error", results[0])
        self.assertIn("Renamed", results[0])
        self.assertFalse((self.ws / "old.txt").exists())
        self.assertTrue((self.ws / "new.txt").exists())

    # -- list_files --

    def test_list_files_outside_blocked(self):
        results = execute_tool_calls(
            [("list_files", {"path": "/etc"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("outside the workspace", results[0])

    def test_list_files_dotdot_blocked(self):
        results = execute_tool_calls(
            [("list_files", {"path": ".."})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("outside the workspace", results[0])

    def test_list_files_allowed_inside(self):
        (self.ws / "subdir").mkdir()
        (self.ws / "subdir" / "a.txt").write_text("a")
        results = execute_tool_calls(
            [("list_files", {"path": "subdir"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("Error", results[0])
        self.assertIn("a.txt", results[0])

    # -- download_file (filename escaping) --

    def test_download_file_dest_outside_maps_to_basename(self):
        results = execute_tool_calls(
            [("download_file", {"url": "https://example.com/file", "filename": "../escape.tar.gz"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        # Should fail with HTTP error (download attempt), not sandbox error
        self.assertNotIn("outside the workspace", results[0])

    # -- tool-scripts protection (no tool may create/modify scripts) --

    def test_write_file_to_tool_scripts_blocked(self):
        results = execute_tool_calls(
            [("write_file", {"path": "tool-scripts/deploy.sh", "content": "echo pwned"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("Cannot write to tool-scripts", results[0])

    def test_edit_file_in_tool_scripts_blocked(self):
        (self.ws / "tool-scripts").mkdir()
        (self.ws / "tool-scripts" / "existing.sh").write_text("ok")
        results = execute_tool_calls(
            [("edit_file", {"path": "tool-scripts/existing.sh", "old_string": "ok", "new_string": "pwned"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("Cannot edit files in tool-scripts", results[0])

    def test_remove_file_from_tool_scripts_blocked(self):
        (self.ws / "tool-scripts").mkdir()
        (self.ws / "tool-scripts" / "script.sh").write_text("data")
        results = execute_tool_calls(
            [("remove_file", {"path": "tool-scripts/script.sh"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("Cannot remove files from tool-scripts", results[0])

    def test_rename_file_from_tool_scripts_blocked(self):
        (self.ws / "tool-scripts").mkdir()
        (self.ws / "tool-scripts" / "script.sh").write_text("data")
        results = execute_tool_calls(
            [("rename_file", {"source": "tool-scripts/script.sh", "destination": "escaped.sh"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("Cannot rename files in tool-scripts", results[0])

    def test_rename_file_into_tool_scripts_blocked(self):
        self._safe_file("normal.sh", "data")
        results = execute_tool_calls(
            [("rename_file", {"source": "normal.sh", "destination": "tool-scripts/normal.sh"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("Cannot rename files into tool-scripts", results[0])

    def test_download_file_to_tool_scripts_blocked(self):
        results = execute_tool_calls(
            [("download_file", {"url": "https://example.com/script.sh", "filename": "tool-scripts/script.sh"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("Cannot download to tool-scripts", results[0])

    def test_read_file_in_tool_scripts_allowed(self):
        (self.ws / "tool-scripts").mkdir()
        (self.ws / "tool-scripts" / "known.sh").write_text("echo hello")
        results = execute_tool_calls(
            [("read_file", {"path": "tool-scripts/known.sh"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("Error", results[0])
        self.assertIn("echo hello", results[0])

    # -- run_tool_script execution guard --

    def test_run_tool_script_blocked_without_flag(self):
        (self.ws / "tool-scripts").mkdir()
        (self.ws / "tool-scripts" / "list.sh").write_text("#!/bin/sh\necho listed")
        (self.ws / "tool-scripts" / "list.sh").chmod(0o755)
        results = execute_tool_calls(
            [("run_tool_script", {"script_name": "list.sh", "args": []})],
            self.manager, str(self.ws),
            allow_tool_scripts=False,
        )
        self.assertEqual(len(results), 1)
        self.assertIn("--allow-tool-scripts", results[0])

    def test_run_tool_script_works_with_flag(self):
        (self.ws / "tool-scripts").mkdir()
        (self.ws / "tool-scripts" / "hello.sh").write_text("#!/bin/sh\necho hello-world")
        (self.ws / "tool-scripts" / "hello.sh").chmod(0o755)
        results = execute_tool_calls(
            [("run_tool_script", {"script_name": "hello.sh", "args": []})],
            self.manager, str(self.ws),
            allow_tool_scripts=True,
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("Error", results[0])
        self.assertIn("hello-world", results[0])

    def test_run_tool_script_not_found(self):
        (self.ws / "tool-scripts").mkdir()
        results = execute_tool_calls(
            [("run_tool_script", {"script_name": "nonexistent.sh", "args": []})],
            self.manager, str(self.ws),
            allow_tool_scripts=True,
        )
        self.assertEqual(len(results), 1)
        self.assertIn("not found", results[0])

    def test_run_tool_script_outside_blocked(self):
        (self.ws / "tool-scripts").mkdir(exist_ok=True)
        results = execute_tool_calls(
            [("run_tool_script", {"script_name": "/etc/passwd", "args": []})],
            self.manager, str(self.ws),
            allow_tool_scripts=True,
        )
        self.assertEqual(len(results), 1)
        self.assertIn("outside the workspace", results[0])


class TestGitPushBlocked(unittest.TestCase):
    """Test that git push is blocked by execute_tool_calls."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="pbuild_test_")
        self.ws = Path(self.tmpdir).resolve()
        self.manager = RpmSourceManager(str(self.ws))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_git_push_blocked(self):
        results = execute_tool_calls(
            [("git_command", {"command": "git push origin main"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("not allowed", results[0])
        self.assertIn("push", results[0])

    def test_git_push_with_flags_blocked(self):
        results = execute_tool_calls(
            [("git_command", {"command": "git push --force origin main"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("not allowed", results[0])
        self.assertIn("push", results[0])

    def test_git_push_to_upstream_blocked(self):
        results = execute_tool_calls(
            [("git_command", {"command": "git push upstream v1.0"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("not allowed", results[0])
        self.assertIn("push", results[0])

    def test_git_clone_allowed(self):
        results = execute_tool_calls(
            [("git_command", {"command": "git clone https://example.com/repo.git"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("not allowed", results[0])

    def test_git_submodule_allowed(self):
        results = execute_tool_calls(
            [("git_command", {"command": "git submodule add https://example.com/repo.git sub/repo"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("not allowed", results[0])


if __name__ == "__main__":
    unittest.main()
