"""Unit tests for tool sandbox: files outside workspace are blocked, git push is blocked."""

import os
import sys
import tempfile
import unittest
from unittest import mock
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

    def test_read_file_with_offset(self):
        self._safe_file("offset.txt", "0123456789abcdef")
        results = execute_tool_calls(
            [("read_file", {"path": "offset.txt", "offset": 5})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], "56789abcdef")

    def test_read_file_with_limit(self):
        self._safe_file("limit.txt", "0123456789abcdef")
        results = execute_tool_calls(
            [("read_file", {"path": "limit.txt", "limit": 7})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], "0123456")

    def test_read_file_with_offset_and_limit(self):
        self._safe_file("both.txt", "0123456789abcdef")
        results = execute_tool_calls(
            [("read_file", {"path": "both.txt", "offset": 3, "limit": 5})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], "34567")

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

    # -- run_tool_script result-count and flag bypass regression tests --

    def test_run_tool_script_blocked_without_flag_and_no_tool_scripts_dir(self):
        results = execute_tool_calls(
            [("run_tool_script", {"script_name": "list.sh", "args": []})],
            self.manager, str(self.ws),
            allow_tool_scripts=False,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], "")

    def test_format_spec_file_without_allow_tool_scripts(self):
        results = execute_tool_calls(
            [("run_tool_script", {"script_name": "format_spec_file", "args": [str(self.ws)]})],
            self.manager, str(self.ws),
            allow_tool_scripts=False,
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("--allow-tool-scripts", results[0])

    def test_multiple_tool_calls_include_run_tool_script_without_flag(self):
        self._safe_file("existing.txt", "data")
        results = execute_tool_calls(
            [
                ("write_file", {"path": "new.txt", "content": "hello"}),
                ("run_tool_script", {"script_name": "deploy.sh", "args": []}),
            ],
            self.manager, str(self.ws),
            allow_tool_scripts=False,
        )
        self.assertEqual(len(results), 2)
        self.assertIn("Wrote", results[0])
        self.assertEqual(results[1], "")


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

    def test_git_without_prefix_allowed(self):
        """git_command accepts commands without the 'git ' prefix."""
        results = execute_tool_calls(
            [("git_command", {"command": "clone https://example.com/repo.git"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("must start with", results[0])

    def test_git_dash_C_subdir_allowed(self):
        """git -C <subdir> is allowed when subdir is within workspace."""
        subdir = Path(self.ws) / "myrepo"
        subdir.mkdir()
        results = execute_tool_calls(
            [("git_command", {"command": "git -C myrepo status"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("outside the workspace", results[0])

    def test_git_dash_C_dotdot_blocked(self):
        """git -C .. is blocked (directory escape)."""
        results = execute_tool_calls(
            [("git_command", {"command": "git -C .. status"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("outside the workspace", results[0])

    def test_git_dash_C_absolute_blocked(self):
        """git -C /etc is blocked (absolute path outside workspace)."""
        results = execute_tool_calls(
            [("git_command", {"command": "git -C /etc status"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("outside the workspace", results[0])

    def test_git_dash_C_dotdot_deep_blocked(self):
        """git -C ../../tmp is blocked (escape via ..)."""
        results = execute_tool_calls(
            [("git_command", {"command": "git -C ../../tmp status"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("outside the workspace", results[0])

    def test_git_shell_metachar_semicolon_blocked(self):
        """Shell metacharacters are blocked to prevent injection."""
        results = execute_tool_calls(
            [("git_command", {"command": "git clone foo; rm -rf /"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("metacharacters", results[0])

    def test_git_shell_metachar_pipe_blocked(self):
        results = execute_tool_calls(
            [("git_command", {"command": "git status | cat"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("metacharacters", results[0])

    def test_git_shell_metachar_backtick_blocked(self):
        results = execute_tool_calls(
            [("git_command", {"command": "git status `whoami`"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("metacharacters", results[0])

    def test_git_dash_C_deep_dotdot_blocked(self):
        """git -C somedir/../../../ is blocked (escape via nested ..)."""
        results = execute_tool_calls(
            [("git_command", {"command": "git -C somedirectory/../../../ status"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("outside the workspace", results[0])

    def test_git_dash_C_subdir_dotdot_to_etc_blocked(self):
        """git -C subdir/../../../../etc is blocked (escape to /etc)."""
        results = execute_tool_calls(
            [("git_command", {"command": "git -C subdir/../../../../etc status"})],
            self.manager, str(self.ws),
        )
        self.assertEqual(len(results), 1)
        self.assertIn("outside the workspace", results[0])


class TestParseFailedPackage(unittest.TestCase):
    """parse_failed_package must extract the failing package stem from build output."""

    def setUp(self):
        from pbuild_ai.parsing import parse_failed_package
        self.parse = parse_failed_package

    def test_failed_keyword(self):
        out = "something failed for python3-foo\n"
        self.assertEqual(self.parse(out), "python3-foo")

    def test_failure_keyword(self):
        out = "Build failure in bar-1.0\n"
        self.assertEqual(self.parse(out), "bar")

    def test_building_line(self):
        out = "building foo.spec\nsucceeded: 1\nunresolvable: 1\n"
        self.assertEqual(self.parse(out), "foo")

    def test_building_before_unresolvable(self):
        out = ("[PID] building foo.spec\n"
               "[PID] something normal\n"
               "[PID] unresolvable: nothing provides bar\n")
        self.assertEqual(self.parse(out), "foo")

    def test_unresolvable_with_needed_by(self):
        out = ("[PID] unresolvable: nothing provides libxyz\n"
               "needed by python3-foo-1.0\n")
        self.assertEqual(self.parse(out), "python3-foo")

    def test_unresolvable_with_required_by(self):
        out = "unresolvable: nothing provides libxyz required by bar-2.0\n"
        self.assertEqual(self.parse(out), "bar")

    def test_unresolvable_no_package_found(self):
        out = "succeeded: 2\nunresolvable: 1\n"
        self.assertIsNone(self.parse(out))

    def test_unknown_output_returns_none(self):
        out = "some random build output\nno matches here\n"
        self.assertIsNone(self.parse(out))

    def test_empty_string(self):
        self.assertIsNone(self.parse(""))


class TestWriteToolChanges(unittest.TestCase):
    """_write_tool_changes writes a .tool_changes beside the build log when files were changed."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="pbuild_test_tool_changes_")
        (Path(self.tmpdir) / ".git").mkdir()  # fake git repo marker
        self.fake_log = Path(self.tmpdir) / "build-1.log"
        self.fake_log.write_text("build log content")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_analyzer(self):
        from pbuild_ai.ollama_client import OllamaAnalyzer
        analyzer = OllamaAnalyzer()
        analyzer.manager = mock.MagicMock()
        analyzer.manager._last_log_path = str(self.fake_log)
        analyzer.manager.base_dir = self.tmpdir
        return analyzer

    def test_changes_written_when_git_diff_has_content(self):
        diff_out = "--- a/foo.spec\n+++ b/foo.spec\n@@ -1 +1 @@\n-old\n+new\n"
        def mock_run(cmd, **kwargs):
            m = mock.MagicMock()
            m.returncode = 0
            m.stdout = diff_out
            return m
        with mock.patch('subprocess.run', side_effect=mock_run):
            analyzer = self._make_analyzer()
            analyzer._changed_files = {'foo.spec'}
            analyzer._write_tool_changes()
        changes_path = Path(str(self.fake_log) + ".tool_changes")
        self.assertTrue(changes_path.exists())
        content = changes_path.read_text()
        self.assertIn("foo.spec", content)

    def test_changes_written_with_untracked(self):
        """Untracked source files should appear as new file additions."""
        noindex_out = "--- /dev/null\n+++ b/new/foo.spec\n@@ -0,0 +1 @@\n+new\n"
        call_count = 0
        def mock_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            m = mock.MagicMock()
            m.returncode = 0
            m.stdout = noindex_out if '--no-index' in cmd else ""
            return m
        with mock.patch('subprocess.run', side_effect=mock_run):
            analyzer = self._make_analyzer()
            analyzer._changed_files = {'new/foo.spec'}
            analyzer._write_tool_changes()
        changes_path = Path(str(self.fake_log) + ".tool_changes")
        self.assertTrue(changes_path.exists())
        content = changes_path.read_text()
        self.assertIn("new/foo.spec", content)
        self.assertEqual(call_count, 3)  # plain, staged, no-index

    def test_changes_written_with_untracked_and_modified(self):
        """Mixed untracked + modified source files produce a combined diff."""
        diff_out = "--- a/foo.spec\n+++ b/foo.spec\n@@ -1 +1 @@\n-old\n+new\n"
        noindex_out = "--- /dev/null\n+++ b/new/bar.spec\n@@ -0,0 +1 @@\n+bar\n"
        def mock_run(cmd, **kwargs):
            m = mock.MagicMock()
            m.returncode = 0
            if '--no-index' in cmd:
                m.stdout = noindex_out
            elif 'new/bar.spec' in cmd:
                m.stdout = ""
            else:
                m.stdout = diff_out
            return m
        with mock.patch('subprocess.run', side_effect=mock_run):
            analyzer = self._make_analyzer()
            analyzer._changed_files = {'foo.spec', 'new/bar.spec'}
            analyzer._write_tool_changes()
        changes_path = Path(str(self.fake_log) + ".tool_changes")
        self.assertTrue(changes_path.exists())
        content = changes_path.read_text()
        self.assertIn("foo.spec", content)
        self.assertIn("bar.spec", content)

    def test_filters_build_artifacts(self):
        """Files under docs/results/ or ending in .log must be excluded."""
        diff_out = "--- a/foo.spec\n+++ b/foo.spec\n@@ -1 +1 @@\n-old\n+new\n"
        def mock_run(cmd, **kwargs):
            m = mock.MagicMock()
            m.returncode = 0
            m.stdout = diff_out
            return m
        with mock.patch('subprocess.run', side_effect=mock_run):
            analyzer = self._make_analyzer()
            analyzer._changed_files = {'docs/results/foo/build-1.log', 'foo.spec'}
            analyzer._write_tool_changes()
        changes_path = Path(str(self.fake_log) + ".tool_changes")
        self.assertTrue(changes_path.exists())
        content = changes_path.read_text()
        self.assertIn("foo.spec", content)
        self.assertNotIn("build-1.log", content)

    def test_no_changes_when_no_changed_files(self):
        analyzer = self._make_analyzer()
        analyzer._changed_files = set()
        analyzer._write_tool_changes()
        changes_path = Path(str(self.fake_log) + ".tool_changes")
        self.assertFalse(changes_path.exists())

    def test_no_changes_all_filtered_out(self):
        """Only build artifact changes → no .tool_changes file written."""
        analyzer = self._make_analyzer()
        analyzer._changed_files = {'docs/results/foo/build-1.log', 'docs/results.json'}
        analyzer._write_tool_changes()
        changes_path = Path(str(self.fake_log) + ".tool_changes")
        self.assertFalse(changes_path.exists())

    def test_silent_on_git_error(self):
        analyzer = self._make_analyzer()
        analyzer._changed_files = {'foo.spec'}
        with mock.patch('subprocess.run', side_effect=FileNotFoundError):
            analyzer._write_tool_changes()  # must not raise
        changes_path = Path(str(self.fake_log) + ".tool_changes")
        self.assertFalse(changes_path.exists())

    def test_clears_changed_files_after_write(self):
        """_changed_files must be cleared after a successful .tool_changes write."""
        diff_out = "--- a/foo.spec\n+++ b/foo.spec\n@@ -1 +1 @@\n-old\n+new\n"
        def mock_run(cmd, **kwargs):
            m = mock.MagicMock()
            m.returncode = 0
            m.stdout = diff_out
            return m
        with mock.patch('subprocess.run', side_effect=mock_run):
            analyzer = self._make_analyzer()
            analyzer._changed_files = {'foo.spec'}
            analyzer._write_tool_changes()
        self.assertEqual(analyzer._changed_files, set())

    def test_before_contents_shows_only_changes_for_untracked(self):
        """Untracked file with before_contents should produce a modified-file diff, not a new-file diff."""
        before = "Name: oterm\nVersion: 1.0\n%description\nTest.\n"
        after  = "Name: oterm\nVersion: 2.0\n%description\nTest.\n"
        (Path(self.tmpdir) / "oterm.spec").write_text(after)
        def mock_run(cmd, **kwargs):
            m = mock.MagicMock()
            m.returncode = 0
            m.stdout = ""  # all git diff attempts return empty (untracked file)
            return m
        with mock.patch('subprocess.run', side_effect=mock_run):
            analyzer = self._make_analyzer()
            analyzer._changed_files = {'oterm.spec'}
            analyzer._write_tool_changes(before_contents={'oterm.spec': before})
        changes_path = Path(str(self.fake_log) + ".tool_changes")
        self.assertTrue(changes_path.exists())
        content = changes_path.read_text()
        # Must show the changed line (not entire file)
        self.assertIn("-Version: 1.0", content)
        self.assertIn("+Version: 2.0", content)
        # Must be a modified diff, NOT a new-file diff
        self.assertNotIn("/dev/null", content)
        self.assertNotIn("@@ -0,0", content)

    def test_before_contents_skips_when_content_identical(self):
        """When before equals after, content-based diff returns nothing."""
        content = "Name: oterm\nVersion: 1.0\n"
        (Path(self.tmpdir) / "oterm.spec").write_text(content)
        def mock_run(cmd, **kwargs):
            m = mock.MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m
        with mock.patch('subprocess.run', side_effect=mock_run):
            analyzer = self._make_analyzer()
            analyzer._changed_files = {'oterm.spec'}
            analyzer._write_tool_changes(before_contents={'oterm.spec': content})
        changes_path = Path(str(self.fake_log) + ".tool_changes")
        self.assertFalse(changes_path.exists())

    def test_multiple_diff_parts_have_single_newline_separator(self):
        """Multiple diffs in .tool_changes must be joined without extra blank lines."""
        diff1 = "--- a/foo.spec\n+++ b/foo.spec\n@@ -1 +1 @@\n-old\n+new\n"
        diff2 = "--- a/bar.spec\n+++ b/bar.spec\n@@ -1 +1 @@\n-old\n+new\n"
        call_idx = 0
        def mock_run(cmd, **kwargs):
            nonlocal call_idx
            m = mock.MagicMock()
            m.returncode = 0
            if '--no-index' in cmd:
                m.stdout = ""
            elif call_idx < 2 and '--staged' not in cmd:
                m.stdout = diff1 if call_idx == 0 else diff2
                call_idx += 1
            else:
                m.stdout = ""
            return m
        with mock.patch('subprocess.run', side_effect=mock_run):
            analyzer = self._make_analyzer()
            analyzer._changed_files = {'foo.spec', 'bar.spec'}
            analyzer._write_tool_changes()
        changes_path = Path(str(self.fake_log) + ".tool_changes")
        self.assertTrue(changes_path.exists())
        content = changes_path.read_text()
        self.assertIn("foo.spec", content)
        self.assertIn("bar.spec", content)
        self.assertNotIn("\n\n-", content, msg="Should not have blank lines between diff hunks")

    def test_add_changed_file_tracks_relative_path(self):
        """_add_changed_file converts an absolute path to the relative form used by git diff."""
        analyzer = self._make_analyzer()
        abs_path = str(Path(self.tmpdir) / "subdir" / "other.spec")
        Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
        analyzer._add_changed_file(abs_path)
        self.assertIn("subdir/other.spec", analyzer._changed_files)

    def test_add_changed_file_tracks_relative_path_as_is(self):
        """_add_changed_file keeps an already-relative path unchanged."""
        analyzer = self._make_analyzer()
        analyzer._add_changed_file("my.spec")
        self.assertIn("my.spec", analyzer._changed_files)


if __name__ == "__main__":
    unittest.main()
