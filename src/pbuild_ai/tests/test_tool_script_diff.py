"""Changes made by tool scripts bypass write_file/edit_file; they must still
show up as a diff (a removed #!BcntSyncTag line went unnoticed before)."""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.diff_utils import write_with_diff
from pbuild_ai.workspace import RpmSourceManager
from pbuild_ai.tools import execute_tool_calls

SPEC = "#!BcntSyncTag: ollama\nName: demo\nVersion: 1.0\n"


class TestToolScriptDiff(unittest.TestCase):

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="pbuild_test_script_diff_"))
        self.addCleanup(shutil.rmtree, self.ws, True)
        (self.ws / "demo.spec").write_text(SPEC)
        (self.ws / "tool-scripts").mkdir()
        self.manager = RpmSourceManager(str(self.ws))

    def _run(self, body):
        script = self.ws / "tool-scripts" / "s.sh"
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(0o755)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            results = execute_tool_calls(
                [("run_tool_script", {"script_name": "s.sh", "args": []})],
                self.manager, str(self.ws), allow_tool_scripts=True)
        return results, out.getvalue()

    def test_spec_change_by_script_is_diffed(self):
        results, out = self._run("sed -i '/BcntSyncTag/d' demo.spec\n")
        self.assertIn("-#!BcntSyncTag: ollama", out)
        self.assertIn("Files changed by the script: demo.spec", results[0])

    def test_failed_script_changes_are_diffed(self):
        results, out = self._run("sed -i 's/1.0/2.0/' demo.spec\nexit 3\n")
        self.assertIn("+Version: 2.0", out)
        self.assertIn("Files changed by the script: demo.spec", results[0])

    def test_new_and_removed_files_are_reported(self):
        (self.ws / "old.txt").write_text("x\n")
        results, out = self._run("rm old.txt\necho y > new.txt\n")
        self.assertIn("old.txt was removed", out)
        self.assertIn("+y", out)
        self.assertIn("new.txt", results[0])
        self.assertIn("old.txt", results[0])

    def test_unchanged_files_are_quiet(self):
        results, out = self._run("echo hi\n")
        self.assertNotIn("--- Diff", out)
        self.assertNotIn("Files changed", results[0])


class TestWriteWithDiff(unittest.TestCase):

    def test_write_shows_diff(self):
        tmp = Path(tempfile.mkdtemp(prefix="pbuild_test_wwd_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        spec = tmp / "demo.spec"
        spec.write_text(SPEC)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            write_with_diff(spec, SPEC.replace("1.0", "1.1"), prefix="[UPDATE]")
        self.assertIn("+Version: 1.1", out.getvalue())
        self.assertEqual(spec.read_text(), SPEC.replace("1.0", "1.1"))


if __name__ == "__main__":
    unittest.main()
