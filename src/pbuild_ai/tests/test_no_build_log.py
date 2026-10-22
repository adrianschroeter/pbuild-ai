"""Regression tests for --fix with no build log present.

Before the fix, a build that failed before pbuild wrote any log (e.g. a
stale build root or an early VM failure) made the fix loop abort with
"[BUG] No build log and no unresolvable deps detected" and exit 1.
build_phase_reached() also wrongly returned True when a _build.* dir
existed but contained no log, skipping the --clean retry safety net.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


class TestBuildPhaseReachedNoLog(unittest.TestCase):
    """build_phase_reached must be False when _build.* exists but holds no log."""

    def setUp(self):
        _yaml = types.ModuleType('yaml')
        _yaml.YAMLError = Exception
        sys.modules.setdefault('yaml', _yaml)
        if SRC_DIR not in sys.path:
            sys.path.insert(0, SRC_DIR)
        self.tmpdir = Path(tempfile.mkdtemp(prefix="pbuild_nolog_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _manager(self):
        from pbuild_ai.workspace import RpmSourceManager
        return RpmSourceManager(str(self.tmpdir))

    def test_no_build_dir_at_all_returns_true(self):
        self.assertTrue(self._manager().build_phase_reached(package_name="testpkg"))

    def test_build_dir_without_log_returns_false(self):
        (self.tmpdir / "_build.openSUSE_Tumbleweed.x86_64").mkdir()
        self.assertFalse(self._manager().build_phase_reached(package_name="testpkg"))

    def test_build_dir_with_building_log_returns_true(self):
        log = self.tmpdir / "_build.openSUSE_Tumbleweed.x86_64" / "build.log"
        log.parent.mkdir()
        log.write_text("... some header\n----- building testpkg.spec ...\n")
        self.assertTrue(self._manager().build_phase_reached(package_name="testpkg"))

    def test_build_dir_with_pre_build_log_returns_false(self):
        log = self.tmpdir / "_build.openSUSE_Tumbleweed.x86_64" / "build.log"
        log.parent.mkdir()
        log.write_text("setting up build root\n")
        self.assertFalse(self._manager().build_phase_reached(package_name="testpkg"))


class TestFixLoopNoBuildLog(unittest.TestCase):
    """--fix must not abort with [BUG] when no build log exists; it retries
    with --clean once and then analyzes the pbuild output itself."""

    def test_no_log_falls_back_to_clean_retry_and_ai_analysis(self):
        script = textwrap.dedent(f"""\
        import sys, os, types, tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch

        _yaml = types.ModuleType('yaml')
        _yaml.YAMLError = Exception
        sys.modules['yaml'] = _yaml

        tmpdir = tempfile.mkdtemp(prefix="pbuild_nolog_e2e_")
        (Path(tmpdir) / "testpkg.spec").write_text(
            "Name: testpkg\\nVersion: 1.0\\n\\n%description\\nTest.\\n"
        )

        sys.path.insert(0, {SRC_DIR!r})
        sys.argv = ["pbuild-ai", "--fix", "--max-fix-attempts", "3", tmpdir]

        from pbuild_ai.workspace import RpmSourceManager as _RSM
        from pbuild_ai.llm_client import LlmAnalyzer as _OA

        # Early failure: nothing unresolvable, nothing env-pattern matches.
        build_out = ("STDOUT (last 100 lines):\\n"
                     "Virtual machine exited unexpectedly with code 3\\n"
                     "bootstrap phase aborted")

        calls = []
        def fake_build(*a, **k):
            calls.append(bool(k.get('force_clean')))
            return False, build_out

        analyses = []
        def fake_analyze(self, prompt, context, *a, **k):
            analyses.append(context or "")
            return "analysis: nothing to fix"

        exit_code = 0
        try:
            with _patch.object(_RSM, 'run_orphan_build', side_effect=fake_build):
                with _patch.object(_RSM, 'get_build_log',
                                   return_value=(None, "No build log found in _build.x86_64")):
                    with _patch.object(_OA, 'analyze', fake_analyze):
                        with _patch.object(_OA, 'call_with_tools', return_value=[]):
                            with _patch.object(_OA, 'print_stats'):
                                with _patch.object(_OA, '_write_analysis_file'):
                                    import pbuild_ai.pbuild_ai
                                    try:
                                        pbuild_ai.pbuild_ai.main()
                                    except SystemExit as e:
                                        exit_code = e.code
        finally:
            print(f"RESULT exit={{exit_code}} builds={{len(calls)}} clean_retries={{sum(calls)}}")
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
            sys.exit(exit_code)
        """)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(script)
            tmpfile = f.name
        try:
            result = subprocess.run(
                [sys.executable, tmpfile],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        finally:
            os.unlink(tmpfile)
        out = result.stdout + result.stderr
        self.assertNotIn("This is likely a bug", out)
        self.assertIn("No build log — build phase likely never started. Retrying with --clean...", out)
        # The loop retried the build with force_clean=True at least once.
        self.assertIn("RESULT", out)
        clean_line = [l for l in out.splitlines() if l.startswith("RESULT")]
        self.assertTrue(clean_line, out)
        self.assertGreaterEqual(int(clean_line[0].split("builds=")[1].split()[0]), 2,
                               f"expected initial build plus clean retry: {clean_line[0]}")
        self.assertGreaterEqual(int(clean_line[0].split("clean_retries=")[1].split()[0]), 1,
                               f"expected a force_clean retry among builds: {clean_line[0]}")


if __name__ == "__main__":
    unittest.main()
