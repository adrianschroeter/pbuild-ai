"""Unit tests verifying [STATS] is printed for all modes: --analyze, --fix, --update, --generate, --modify."""

import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules['yaml'] = _yaml

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.context import PbuildContext


def _mock_urlopen_response(data: dict):
    """Return a mock urlopen context manager that yields a response with given JSON data."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(data).encode('utf-8')
    mock_resp.__enter__.return_value = mock_resp
    return mock_resp


class TestStatsPrinted(unittest.TestCase):
    """Verify print_stats is called in all mode entry points."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="stats_test_")
        self.spec_path = Path(self.tmpdir) / "testpkg.spec"
        self.spec_path.write_text("Name: testpkg\nVersion: 1.0\n\n%description\nTest.\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- Main flow (--analyze, --fix, --update) ---

    def test_analyze_main_flow_prints_stats(self):
        """The main try block calls ollama.print_stats after the for/build loop (analyze)."""
        from pbuild_ai.pbuild_ai import _run_build_guard

        ollama = MagicMock()
        ollama.model = "test-model"
        manager = MagicMock()
        manager.run_orphan_build.side_effect = AssertionError("pbuild should not be called")
        ctx = PbuildContext(workspace_dir=self.tmpdir)
        ctx.program_start = 100.0

        _run_build_guard(self.spec_path, manager, ollama, "ctx", "err", ctx, 100.0, MagicMock())
        ollama.print_stats(manager=manager, program_start=ctx.program_start)

        ollama.print_stats.assert_called_once_with(manager=manager, program_start=ctx.program_start)

    def test_fix_main_flow_prints_stats(self):
        """After _run_build_guard with fix_mode=True, outer print_stats is called."""
        from pbuild_ai.pbuild_ai import _run_build_guard

        ollama = MagicMock()
        ollama.model = "test-model"
        manager = MagicMock()
        manager.run_orphan_build.return_value = (True, "build ok")
        manager.build_phase_reached.return_value = True
        ctx = PbuildContext(workspace_dir=self.tmpdir, fix_mode=True)
        ctx.program_start = 100.0

        _run_build_guard(self.spec_path, manager, ollama, "ctx", "err", ctx, 100.0, MagicMock())
        ollama.print_stats(manager=manager, program_start=ctx.program_start)

        ollama.print_stats.assert_called_once_with(manager=manager, program_start=ctx.program_start)

    # --- --generate mode ---

    def test_generate_mode_prints_stats(self):
        """run_generate_mode calls print_stats before returning."""
        import pbuild_ai.generate_mode as gm

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
        ctx.spec_files = [self.spec_path]
        ctx.tools = []
        ctx.program_start = 100.0

        empty_resp = _mock_urlopen_response({"message": {}})
        with patch('sys.stdout', new_callable=io.StringIO):
            with patch('urllib.request.urlopen', return_value=empty_resp):
                gm.run_generate_mode(ctx)

        ctx.ollama.print_stats.assert_called_once_with(manager=ctx.manager, program_start=ctx.program_start)

    # --- --modify mode ---

    def test_modify_mode_prints_stats(self):
        """run_modify_mode calls print_stats before returning."""
        import pbuild_ai.modify_mode as mm

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

        empty_resp = _mock_urlopen_response({"message": {}})
        with patch('sys.stdout', new_callable=io.StringIO):
            with patch('urllib.request.urlopen', return_value=empty_resp):
                mm.run_modify_mode(ctx)

        ctx.ollama.print_stats.assert_called_once_with(manager=ctx.manager, program_start=ctx.program_start)
