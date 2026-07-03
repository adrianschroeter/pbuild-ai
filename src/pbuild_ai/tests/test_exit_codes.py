"""Unit tests verifying correct exit codes for error conditions."""

import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules['yaml'] = _yaml

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.context import PbuildContext


class TestGenerateModeExitCode(unittest.TestCase):
    """run_generate_mode must exit(2) on HTTP errors from Ollama."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="exitcode_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_generate_mode_exits_2_on_http_error(self):
        import pbuild_ai.generate_mode as gm
        from urllib.error import HTTPError

        ctx = PbuildContext(
            workspace_dir=self.tmpdir,
            generate_prompt="create a package",
        )
        ctx.ollama = MagicMock()
        ctx.ollama.model = "test-model"
        ctx.ollama.chat_api_url = "http://localhost:99999"
        ctx.manager = MagicMock()
        ctx.manager.read_file_safe.return_value = "dummy"
        ctx.skill_manager = MagicMock()
        ctx.skill_manager.get_skill_by_name.return_value = None
        ctx.spec_files = []
        ctx.tools = []
        ctx.program_start = 100.0

        fp = BytesIO(b'{"error":"test message"}')
        http_error = HTTPError("http://localhost:99999", 500, "Internal Server Error", {}, fp)

        with patch('sys.stdout', new_callable=io.StringIO):
            with patch('urllib.request.urlopen', side_effect=http_error):
                with self.assertRaises(SystemExit) as cm:
                    gm.run_generate_mode(ctx)

        self.assertEqual(cm.exception.code, 2)


class TestModifyModeExitCode(unittest.TestCase):
    """run_modify_mode must exit(2) on HTTP errors from Ollama."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="exitcode_test_")
        self.spec_path = Path(self.tmpdir) / "testpkg.spec"
        self.spec_path.write_text("Name: testpkg\nVersion: 1.0\n\n%description\nTest.\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_modify_mode_exits_2_on_http_error(self):
        import pbuild_ai.modify_mode as mm
        from urllib.error import HTTPError

        ctx = PbuildContext(
            workspace_dir=self.tmpdir,
            modify_prompt="add a patch",
        )
        ctx.ollama = MagicMock()
        ctx.ollama.model = "test-model"
        ctx.ollama.chat_api_url = "http://localhost:99999"
        ctx.manager = MagicMock()
        ctx.manager.read_file_safe.return_value = "Name: testpkg\nVersion: 1.0\n\n%description\nTest.\n"
        ctx.skill_manager = MagicMock()
        ctx.skill_manager.get_skills_for.return_value = []
        ctx.tools = []
        ctx.spec_files = [self.spec_path]
        ctx.program_start = 100.0
        ctx.debug = False
        ctx.interactive = False
        ctx.allow_tool_scripts = False

        fp = BytesIO(b'{"error":"test message"}')
        http_error = HTTPError("http://localhost:99999", 500, "Internal Server Error", {}, fp)

        with patch('sys.stdout', new_callable=io.StringIO):
            with patch('urllib.request.urlopen', side_effect=http_error):
                with self.assertRaises(SystemExit) as cm:
                    mm.run_modify_mode(ctx)

        self.assertEqual(cm.exception.code, 2)


class TestOuterHandlerExitCode(unittest.TestCase):
    """The outer try/except in the __main__ block exits 2 on unhandled exception."""

    def test_outer_handler_exits_2(self):
        script = textwrap.dedent(f"""\
        import sys, os, types

        # Mock yaml before importing pbuild_ai (it's imported by pbuild_ai.manifest)
        _yaml = types.ModuleType('yaml')
        _yaml.YAMLError = Exception
        sys.modules['yaml'] = _yaml

        sys.path.insert(0, {SRC_DIR!r})
        sys.argv = ["pbuild-ai", "--analyze", "/tmp"]

        from pathlib import Path
        _original_rglob = Path.rglob
        def _broken_rglob(self, pattern):
            if pattern == "*.spec":
                raise RuntimeError("Simulated spec discovery failure")
            return _original_rglob(self, pattern)
        Path.rglob = _broken_rglob

        import pbuild_ai.pbuild_ai
        try:
            pbuild_ai.pbuild_ai.main()
        except SystemExit as e:
            sys.exit(e.code)
        """)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(script)
            tmpfile = f.name
        try:
            result = subprocess.run(
                [sys.executable, tmpfile],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        finally:
            os.unlink(tmpfile)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Script aborted", result.stdout + result.stderr)


class TestFixLoopExitCode(unittest.TestCase):
    """run_fix_loop exits 1 when all fix attempts are exhausted."""

    def test_fix_loop_exits_1_on_exhausted(self):
        script = textwrap.dedent(f"""\
        import sys, os, types, tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch

        _yaml = types.ModuleType('yaml')
        _yaml.YAMLError = Exception
        sys.modules['yaml'] = _yaml

        tmpdir = tempfile.mkdtemp(prefix="pbuild_fix_exit_")
        (Path(tmpdir) / "testpkg.spec").write_text(
            "Name: testpkg\\nVersion: 1.0\\n\\n%description\\nTest.\\n"
        )

        sys.path.insert(0, {SRC_DIR!r})
        sys.argv = ["pbuild-ai", "--fix", "--fix-attempts", "1", tmpdir]

        from pbuild_ai.workspace import RpmSourceManager as _RSM
        from pbuild_ai.ollama_client import OllamaAnalyzer as _OA

        exit_code = 0
        try:
            with _patch.object(_RSM, 'run_orphan_build',
                               return_value=(False, "error: unresolvable dependency")):
                with _patch.object(_RSM, 'build_phase_reached', return_value=True):
                    with _patch.object(_OA, 'analyze',
                                       return_value="analysis found a missing dependency"):
                        with _patch.object(_OA, 'call_with_tools',
                                           return_value=["edit_file: OK: Edited spec file"]):
                            with _patch.object(_OA, 'print_stats'):
                                with _patch.object(_OA, '_write_analysis_file'):
                                    import pbuild_ai.pbuild_ai
                                    try:
                                        pbuild_ai.pbuild_ai.main()
                                    except SystemExit as e:
                                        exit_code = e.code
        finally:
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
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        finally:
            os.unlink(tmpfile)
        self.assertEqual(result.returncode, 1)
        self.assertIn("All 1 fix attempts exhausted.", result.stdout)


if __name__ == "__main__":
    unittest.main()
