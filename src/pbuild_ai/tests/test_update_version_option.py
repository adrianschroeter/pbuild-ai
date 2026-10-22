import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


class TestUpdateVersionOption(unittest.TestCase):
    """--update-version skips the version research; the update prompt still
    needs the current version (NameError _current_version before)."""

    def test_update_prompt_gets_current_version(self):
        tmpdir = tempfile.mkdtemp(prefix="pbuild_updver_")
        self.addCleanup(__import__("shutil").rmtree, tmpdir, True)
        with open(os.path.join(tmpdir, "demo.spec"), "w") as f:
            f.write("Name: demo\nVersion: 0.30.11\nRelease: 0\nSummary: x\nLicense: MIT\n"
                    "Source: demo-%{version}.tar.gz\n\n%description\nTest.\n")
        script = textwrap.dedent(f"""\
        import sys, types
        from unittest.mock import patch
        _yaml = types.ModuleType('yaml'); _yaml.YAMLError = Exception
        sys.modules['yaml'] = _yaml
        sys.path.insert(0, {SRC_DIR!r})
        sys.argv = ["pbuild-ai", "--update-only", "--update-version=0.30.12-rc0", {tmpdir!r}]
        from pbuild_ai.llm_client import LlmAnalyzer
        import pbuild_ai.pbuild_ai as pa
        seen = []
        def fake(self, messages, *a, **k):
            seen.append(messages[1]["content"] if len(messages) > 1 else "")
            return []
        with patch.object(LlmAnalyzer, 'call_with_tools', fake), \\
                patch.object(LlmAnalyzer, 'analyze', return_value=""), \\
                patch.object(LlmAnalyzer, 'print_stats'):
            try:
                pa.main()
            except SystemExit:
                pass
        print("PROMPT:", seen[0] if seen else "<none>")
        """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                timeout=60, cwd=tmpdir, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        out = result.stdout + result.stderr
        self.assertNotIn("NameError", out)
        self.assertIn("PROMPT:", out)
        self.assertIn("0.30.11", out.split("PROMPT:", 1)[1])
        self.assertIn("0.30.12-rc0", out.split("PROMPT:", 1)[1])


if __name__ == "__main__":
    unittest.main()
