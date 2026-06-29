"""Unit tests for the build guard: pbuild is only called when --fix or --update is active."""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Mock yaml before any pbuild_ai import to prevent ModuleNotFoundError
_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules['yaml'] = _yaml

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.context import PbuildContext
from pbuild_ai.pbuild_ai import _run_build_guard


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
        """When --fix is active and build fails, run_fix_loop must be called."""
        _run_build_guard(
            self.spec_path, self.manager, self.ollama, "full_context",
            "error prompt", self.ctx, 100.0, self.run_fix_loop,
        )
        self.manager.run_orphan_build.assert_called_once()
        self.ollama.analyze.assert_called_once()
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
