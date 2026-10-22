"""Regression tests for fatal OBS API access failures (401/403).

When pbuild itself dies on the OBS API before the build phase starts — e.g.
"download of https://api.opensuse.org/source/home:foo:16/_meta failed:
401 Unauthorized" — pbuild-ai must report the offending line and abort
immediately instead of burning AI tokens on analysis or doomed retries.
pbuild fetches OBS metadata anonymously (oscrc credentials are not used
for --obs), so 401/403 means a private or non-existent project and is
deterministic. Other download failures (5xx, timeouts) stay on the
retryable environment-error path.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

_401_OUTPUT = (
    "STDOUT (last 100 lines):\n"
    "download of https://api.opensuse.org/source/home:adrianSuSE:16/_meta "
    "failed: 401 Unauthorized\n"
)


class TestFatalPbuildReason(unittest.TestCase):
    """_fatal_pbuild_reason must match 401/403 auth failures only."""

    def setUp(self):
        _yaml = types.ModuleType('yaml')
        _yaml.YAMLError = Exception
        sys.modules.setdefault('yaml', _yaml)
        if SRC_DIR not in sys.path:
            sys.path.insert(0, SRC_DIR)
        from pbuild_ai.pbuild_ai import _fatal_pbuild_reason
        self.fatal = _fatal_pbuild_reason

    def test_401_meta_download_matches(self):
        line = self.fatal(_401_OUTPUT)
        self.assertIn("failed: 401 Unauthorized", line)

    def test_403_forbidden_matches(self):
        out = ("download of https://api.opensuse.org/source/home:foo/_meta "
               "failed: 403 Forbidden\n")
        self.assertIn("403 Forbidden", self.fatal(out))

    def test_bare_code_matches(self):
        self.assertTrue(self.fatal("some request failed: 401\n"))

    def test_compile_error_does_not_match(self):
        out = ("gcc -O2 foo.c\nfoo.c:10:5: error: 'bar' undeclared\n"
               "make: *** [Makefile:401: all] Error 2\n")
        self.assertEqual(self.fatal(out), "")

    def test_transient_download_failure_does_not_match(self):
        out = ("download of https://mirror.example.com/dummy.rpm "
               "failed: 500 Internal Server Error\n")
        self.assertEqual(self.fatal(out), "")

    def test_empty_output(self):
        self.assertEqual(self.fatal(""), "")


class TestFixLoopAbortsOnFatal(unittest.TestCase):
    """--fix must abort with exit 1 without build-failure AI analysis and
    without a doomed --clean retry when the output shows a 401 and no
    build log exists."""

    def test_401_aborts_without_ai_analysis(self):
        script = textwrap.dedent(f"""\
        import sys, os, types, tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch

        _yaml = types.ModuleType('yaml')
        _yaml.YAMLError = Exception
        sys.modules['yaml'] = _yaml

        tmpdir = tempfile.mkdtemp(prefix="pbuild_401_e2e_")
        (Path(tmpdir) / "testpkg.spec").write_text(
            "Name: testpkg\\nVersion: 1.0\\n\\n%description\\nTest.\\n"
        )

        sys.path.insert(0, {SRC_DIR!r})
        sys.argv = ["pbuild-ai", "--fix", "--max-fix-attempts", "3", tmpdir]

        from pbuild_ai.workspace import RpmSourceManager as _RSM
        from pbuild_ai.llm_client import LlmAnalyzer as _OA

        build_out = {(_401_OUTPUT)!r}

        calls = []
        def fake_build(*a, **k):
            calls.append(bool(k.get('force_clean')))
            return False, build_out

        analyses = []
        def fake_analyze(self, prompt, context, *a, **k):
            if k.get('task') == 'Analyzing build failure':
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
            print(f"RESULT exit={{exit_code}} builds={{len(calls)}} analyses={{len(analyses)}}")
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
        result_line = [l for l in out.splitlines() if l.startswith("RESULT")]
        self.assertTrue(result_line, out)
        self.assertIn("[FATAL] pbuild failed before the build phase started", out)
        self.assertIn("failed: 401 Unauthorized", out)
        self.assertIn("[HINT] pbuild requests OBS project metadata anonymously", out)
        fields = dict(p.split("=") for p in result_line[0].split()[1:])
        self.assertEqual(fields["exit"], "1", out)
        # No doomed --clean retry and no AI analysis: abort right after build 1.
        self.assertEqual(fields["builds"], "1", out)
        self.assertEqual(fields["analyses"], "0", out)


if __name__ == "__main__":
    unittest.main()
