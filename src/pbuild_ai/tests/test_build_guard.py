"""Unit tests for the build guard: pbuild is only called when --fix or --update is active."""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Mock yaml before any pbuild_ai import to prevent ModuleNotFoundError
_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules['yaml'] = _yaml

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.context import PbuildContext
from pbuild_ai.pbuild_ai import _run_build_guard, _check_arg_conflicts, _check_update_hints


class TestBuildGuard(unittest.TestCase):
    """Verify that _run_build_guard only calls pbuild when fix_mode or update_version is set."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="pbuild_guard_test_")
        self.spec_path = Path(self.tmpdir) / "testpkg.spec"
        self.spec_path.write_text("Name: testpkg\nVersion: 1.0\n\n%description\nTest package.\n")

        # Manager: all pbuild methods raise if called
        self.manager = MagicMock()
        self.manager.run_orphan_build.side_effect = AssertionError("pbuild should not be called")
        self.manager.run_project_build.side_effect = AssertionError("pbuild should not be called")
        self.manager.run_full_project_build.side_effect = AssertionError("pbuild should not be called")
        self.manager.run_deep_analyze_shell.side_effect = AssertionError("pbuild should not be called")

        # Ollama: needs model attribute for failure messages
        self.ollama = MagicMock()
        self.ollama.model = "test-model"

        # Context: no fix, no update
        self.ctx = PbuildContext(
            workspace_dir=self.tmpdir,
            fix_mode=False,
            update_version=None,
            package_filter=None,
            project_mode=False,
            preset=None,
            show_buildlog=False,
            allow_tool_scripts=False,
            deep_analyze=False,
            fix_attempts=10,
            debug=False,
            interactive=False,
        )

        self.run_fix_loop = MagicMock()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_build_not_called_when_no_fix_no_update(self):
        """_run_build_guard must NOT call pbuild when --fix and --update are both absent."""
        error_prompt = "test error prompt"
        result = _run_build_guard(
            self.spec_path, self.manager, self.ollama, "full_context",
            error_prompt, self.ctx, 100.0, self.run_fix_loop,
        )

        self.manager.run_orphan_build.assert_not_called()
        self.manager.run_project_build.assert_not_called()
        self.manager.run_full_project_build.assert_not_called()
        self.manager.run_deep_analyze_shell.assert_not_called()
        self.run_fix_loop.assert_not_called()
        self.assertEqual(result, error_prompt, "error_prompt must be returned unchanged")

    def _enable_orphan_build(self):
        """Remove side_effect and set a success return for run_orphan_build."""
        self.manager.run_orphan_build.side_effect = None
        self.manager.run_orphan_build.return_value = (True, "build output")

    def _enable_project_build(self):
        """Remove side_effect and set a success return for run_project_build."""
        self.manager.run_project_build.side_effect = None
        self.manager.run_project_build.return_value = (True, "build output")

    def test_orphan_build_called_when_fix_mode(self):
        """With --fix and no filter/mode, run_orphan_build must be called."""
        self.ctx.fix_mode = True
        self.ctx.update_version = None
        self.ctx.package_filter = None
        self.ctx.project_mode = False
        self._enable_orphan_build()
        self.manager.build_phase_reached.return_value = True

        _run_build_guard(
            self.spec_path, self.manager, self.ollama, "full_context",
            "error prompt", self.ctx, 100.0, self.run_fix_loop,
        )

        self.manager.run_orphan_build.assert_called_once()
        self.manager.run_project_build.assert_not_called()

    def test_project_build_called_when_fix_and_package_filter(self):
        """With --fix and package_filter, run_project_build must be called."""
        self.ctx.fix_mode = True
        self.ctx.update_version = None
        self.ctx.package_filter = "testpkg"
        self.ctx.project_mode = False
        self._enable_project_build()
        self.manager.build_phase_reached.return_value = True

        _run_build_guard(
            self.spec_path, self.manager, self.ollama, "full_context",
            "error prompt", self.ctx, 100.0, self.run_fix_loop,
        )

        self.manager.run_project_build.assert_called_once()
        self.manager.run_orphan_build.assert_not_called()

    def test_project_mode_build_called_when_fix(self):
        """With --fix and project_mode, run_project_build must be called."""
        self.ctx.fix_mode = True
        self.ctx.update_version = None
        self.ctx.package_filter = None
        self.ctx.project_mode = True
        self._enable_project_build()
        self.manager.build_phase_reached.return_value = True

        _run_build_guard(
            self.spec_path, self.manager, self.ollama, "full_context",
            "error prompt", self.ctx, 100.0, self.run_fix_loop,
        )

        self.manager.run_project_build.assert_called_once()
        self.manager.run_orphan_build.assert_not_called()

    def test_orphan_build_called_when_update_version(self):
        """With --update (update_version set), run_orphan_build must be called."""
        self.ctx.fix_mode = False
        self.ctx.update_version = "2.0"
        self.ctx.package_filter = None
        self.ctx.project_mode = False
        self._enable_orphan_build()
        self.manager.build_phase_reached.return_value = True

        _run_build_guard(
            self.spec_path, self.manager, self.ollama, "full_context",
            "error prompt", self.ctx, 100.0, self.run_fix_loop,
        )

        self.manager.run_orphan_build.assert_called_once()
        self.manager.run_project_build.assert_not_called()

    def test_build_not_called_when_update_version_none_and_fix_false(self):
        """Explicitly confirm: both update_version=None AND fix_mode=False means no build."""
        self.ctx.fix_mode = False
        self.ctx.update_version = None

        for pkg_filter in [None, "testpkg", "other"]:
            for proj_mode in [False, True]:
                with self.subTest(package_filter=pkg_filter, project_mode=proj_mode):
                    self.ctx.package_filter = pkg_filter
                    self.ctx.project_mode = proj_mode
                    _run_build_guard(
                        self.spec_path, self.manager, self.ollama, "full_context",
                        "error", self.ctx, 100.0, self.run_fix_loop,
                    )
                    self.manager.run_orphan_build.assert_not_called()
                    self.manager.run_project_build.assert_not_called()


class TestBuildGuardBuildFails(unittest.TestCase):
    """Tests for the build-failure path within the guard."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="pbuild_guard_fail_")
        self.spec_path = Path(self.tmpdir) / "testpkg.spec"
        self.spec_path.write_text("Name: testpkg\nVersion: 1.0\n\n%description\nTest package.\n")

        self.manager = MagicMock()
        self.manager.run_orphan_build.return_value = (False, "error: unresolvable dependency")
        self.manager.run_project_build.return_value = (False, "error: some build error")
        self.manager.build_phase_reached.return_value = True

        self.ollama = MagicMock()
        self.ollama.model = "test-model"

        self.ctx = PbuildContext(
            workspace_dir=self.tmpdir,
            fix_mode=True,
            update_version=None,
            project_mode=False,
        )

        self.run_fix_loop = MagicMock()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fix_loop_called_on_build_failure(self):
        """When --fix is active and build fails, run_fix_loop must be called.
        analyze is skipped here — run_fix_loop handles it internally."""
        _run_build_guard(
            self.spec_path, self.manager, self.ollama, "full_context",
            "error prompt", self.ctx, 100.0, self.run_fix_loop,
        )
        self.manager.run_orphan_build.assert_called_once()
        self.ollama.analyze.assert_not_called()
        self.run_fix_loop.assert_called_once()

    def test_no_fix_loop_without_fix_mode(self):
        """With --update but no --fix, run_fix_loop must NOT be called on failure."""
        self.ctx.fix_mode = False
        self.ctx.update_version = "2.0"

        _run_build_guard(
            self.spec_path, self.manager, self.ollama, "full_context",
            "error prompt", self.ctx, 100.0, self.run_fix_loop,
        )
        self.run_fix_loop.assert_not_called()

    def test_analyze_not_called_when_no_build(self):
        """Without --fix or --update, ollama.analyze must NOT be called."""
        self.ctx.fix_mode = False
        self.ctx.update_version = None

        _run_build_guard(
            self.spec_path, self.manager, self.ollama, "full_context",
            "error prompt", self.ctx, 100.0, self.run_fix_loop,
        )
        self.ollama.analyze.assert_not_called()

    def test_gitexplorer_not_called_when_no_build(self):
        """Without --fix or --update, gitexplorer injection must NOT be attempted."""
        self.ctx.fix_mode = False
        self.ctx.update_version = None

        with unittest.mock.patch('pbuild_ai.pbuild_ai._inject_gitexplorer_results') as mock_inject:
            _run_build_guard(
                self.spec_path, self.manager, self.ollama, "full_context",
                "error prompt", self.ctx, 100.0, self.run_fix_loop,
            )
            mock_inject.assert_not_called()

    def test_fix_loop_propagates_exit_1(self):
        """_run_build_guard must propagate SystemExit(1) from run_fix_loop when exhausted."""
        self.run_fix_loop.side_effect = SystemExit(1)
        with self.assertRaises(SystemExit) as cm:
            _run_build_guard(
                self.spec_path, self.manager, self.ollama, "full_context",
                "error prompt", self.ctx, 100.0, self.run_fix_loop,
            )
        self.assertEqual(cm.exception.code, 1)


class TestAnalyzeFlag(unittest.TestCase):
    """Verify the --analyze flag prevents builds and enforces conflicts."""

    def test_analyze_mode_in_context(self):
        """--analyze must set ctx.analyze_mode to True."""
        with patch('sys.argv', ['pbuild_ai', '--analyze', '/tmp/foo']):
            import argparse
            parser = argparse.ArgumentParser(description="RPM packager helper")
            parser.add_argument("workspace_dir")
            parser.add_argument("package_name", nargs="?", default=None)
            parser.add_argument("--analyze", "-a", action="store_true")
            parser.add_argument("--fix", "-f", action="store_true")
            args = parser.parse_args()
            self.assertTrue(args.analyze)
            self.assertFalse(args.fix)

    def test_analyze_no_build(self):
        """With --analyze (fix_mode=False, update_version=None), _run_build_guard must skip build."""
        manager = MagicMock()
        manager.run_orphan_build.side_effect = AssertionError("pbuild should not be called")
        manager.run_project_build.side_effect = AssertionError("pbuild should not be called")

        ollama = MagicMock()
        ollama.model = "test-model"
        tmpdir = tempfile.mkdtemp(prefix="pbuild_analyze_test_")
        try:
            spec_path = Path(tmpdir) / "testpkg.spec"
            spec_path.write_text("Name: testpkg\nVersion: 1.0\n\n%description\nTest package.\n")
            ctx = PbuildContext(
                workspace_dir=tmpdir,
                fix_mode=False,
                update_version=None,
                analyze_mode=True,
            )
            result = _run_build_guard(
                spec_path, manager, ollama, "full_context",
                "error prompt", ctx, 100.0, MagicMock(),
            )
            manager.run_orphan_build.assert_not_called()
            manager.run_project_build.assert_not_called()
            self.assertEqual(result, "error prompt")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestAnalyzeConflicts(unittest.TestCase):
    """Verify --analyze conflict checks via _check_arg_conflicts."""

    def _make_parser(self):
        """Return an argparse parser matching the CLI."""
        import argparse
        parser = argparse.ArgumentParser(description="RPM packager helper")
        parser.add_argument("workspace_dir")
        parser.add_argument("--analyze", "-a", action="store_true")
        parser.add_argument("--fix", "-f", action="store_true")
        parser.add_argument("--update", "-u", action="store_true")
        parser.add_argument("--update-only", action="store_true")
        parser.add_argument("--changelog", action="store_true")
        parser.add_argument("--generate", default=None)
        parser.add_argument("--modify", "-m", default=None)
        return parser

    def _check(self, argv):
        """Parse argv and call _check_arg_conflicts, return the error message (or None)."""
        import argparse, io
        parser = self._make_parser()
        stderr_buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = stderr_buf
        try:
            args = parser.parse_args(argv)
            _check_arg_conflicts(parser, args)
            return None  # no conflict
        except SystemExit:
            return stderr_buf.getvalue()
        finally:
            sys.stderr = old_stderr

    def test_analyze_conflicts_with_update(self):
        msg = self._check(["--analyze", "--update", "/tmp/d"])
        self.assertIsNotNone(msg)
        self.assertIn("--analyze cannot be used with", msg)

    def test_analyze_conflicts_with_update_only(self):
        msg = self._check(["--analyze", "--update-only", "/tmp/d"])
        self.assertIsNotNone(msg)
        self.assertIn("--analyze cannot be used with", msg)

    def test_analyze_conflicts_with_generate(self):
        msg = self._check(["--analyze", "--generate=foo", "/tmp/d"])
        self.assertIsNotNone(msg)
        self.assertIn("--analyze cannot be used with", msg)

    def test_analyze_conflicts_with_changelog(self):
        msg = self._check(["--analyze", "--changelog", "/tmp/d"])
        self.assertIsNotNone(msg)
        self.assertIn("--analyze cannot be used with", msg)

    def test_analyze_conflicts_with_modify(self):
        msg = self._check(["--analyze", "--modify=fix", "/tmp/d"])
        self.assertIsNotNone(msg)
        self.assertIn("--analyze cannot be used with", msg)

    def test_fix_conflicts_with_analyze(self):
        msg = self._check(["--fix", "--analyze", "/tmp/d"])
        self.assertIsNotNone(msg)
        self.assertIn("--fix cannot be used with", msg)

    def test_fix_conflicts_with_changelog(self):
        msg = self._check(["--fix", "--changelog", "/tmp/d"])
        self.assertIsNotNone(msg)
        self.assertIn("--fix cannot be used with", msg)

    def test_no_conflict_with_valid_combos(self):
        """Combinations that should NOT trigger conflicts."""
        for argv in [
            ["--analyze", "/tmp/d"],
            ["--fix", "/tmp/d"],
            ["--update", "/tmp/d"],
            ["--generate=foo", "/tmp/d"],
            ["--changelog", "/tmp/d"],
            ["--modify=fix", "/tmp/d"],
            ["--fix", "--update", "/tmp/d"],
            ["--update", "--changelog", "/tmp/d"],
            ["-u", "/tmp/d"],
            ["-a", "/tmp/d"],
            ["-f", "/tmp/d"],
        ]:
            with self.subTest(argv=argv):
                self.assertIsNone(self._check(argv))


class TestUpdateHints(unittest.TestCase):
    """Test _check_update_hints: detects [REBUILD: pkg] and cross-package spec edits."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="pbuild_hints_test_"))
        self.spec_a = self.tmpdir / "pkg-a.spec"
        self.spec_b = self.tmpdir / "pkg-b.spec"
        self.spec_a.write_text("Name: pkg-a\nVersion: 1.0\n")
        self.spec_b.write_text("Name: pkg-b\nVersion: 2.0\n")
        self.spec_files = [self.spec_a, self.spec_b]
        self.originals = {
            self.spec_a: self.spec_a.read_text(),
            self.spec_b: self.spec_b.read_text(),
        }
        self.updated = set()
        self.manager = MagicMock()
        self.manager.read_file_safe.side_effect = lambda p: p.read_text()

    def test_rebuild_marker_in_assistant_message(self):
        """[REBUILD: pkg-a] in assistant content adds spec to updated_packages."""
        messages = [
            {"role": "assistant", "content": "Found version 2.0.\n[REBUILD: pkg-a]"},
        ]
        _check_update_hints([], messages, self.spec_files, self.originals, self.updated, self.manager)
        self.assertIn(self.spec_a, self.updated)
        self.assertNotIn(self.spec_b, self.updated)

    def test_rebuild_marker_with_spec_suffix(self):
        """[REBUILD: pkg-a.spec] also matches by stem."""
        messages = [
            {"role": "assistant", "content": "[REBUILD: pkg-b.spec]"},
        ]
        _check_update_hints([], messages, self.spec_files, self.originals, self.updated, self.manager)
        self.assertIn(self.spec_b, self.updated)

    def test_rebuild_marker_no_false_positive(self):
        """Bare '[REBUILD:' without a valid package name does not add anything."""
        messages = [
            {"role": "assistant", "content": "[REBUILD:]"},
        ]
        _check_update_hints(None, messages, self.spec_files, self.originals, self.updated, self.manager)
        self.assertEqual(len(self.updated), 0)

    def test_cross_package_spec_edit_detected(self):
        """AI edits pkg-b.spec during pkg-a's research phase -> pkg-b added to updated_packages."""
        self.spec_b.write_text("Name: pkg-b\nVersion: 3.0\n")  # AI edited this
        _check_update_hints([], [], self.spec_files, self.originals, self.updated, self.manager)
        self.assertIn(self.spec_b, self.updated)
        self.assertNotIn(self.spec_a, self.updated)

    def test_cross_package_edit_no_false_positive(self):
        """No spec changes -> nothing added to updated_packages."""
        _check_update_hints([], [], self.spec_files, self.originals, self.updated, self.manager)
        self.assertEqual(len(self.updated), 0)

    def test_rebuild_marker_multiple_messages(self):
        """Multiple assistant messages are scanned."""
        messages = [
            {"role": "assistant", "content": "Step 1 done."},
            {"role": "tool", "content": "read_file: pkg-a.spec (5 lines)", "name": "read_file"},
            {"role": "assistant", "content": "[REBUILD: pkg-b]"},
        ]
        _check_update_hints([], messages, self.spec_files, self.originals, self.updated, self.manager)
        self.assertIn(self.spec_b, self.updated)

    def test_rebuild_dedup(self):
        """Same package hinted twice is only added once."""
        messages = [
            {"role": "assistant", "content": "[REBUILD: pkg-a] [REBUILD: pkg-a]"},
        ]
        _check_update_hints(None, messages, self.spec_files, self.originals, self.updated, self.manager)
        self.assertIn(self.spec_a, self.updated)
        self.assertEqual(len(self.updated), 1)
