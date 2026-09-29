"""Tests for continuing an update that was started but not committed."""

import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

# Mock yaml before any pbuild_ai import
_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules.setdefault('yaml', _yaml)

from pbuild_ai.pbuild_ai import _pending_update
from pbuild_ai.workspace import RpmSourceManager

_OLD = "Name: ollama\nVersion: 0.34.4\n"
_NEW = "Name: ollama\nVersion: 0.35.0\n"


class TestPendingUpdate(unittest.TestCase):

    def test_uncommitted_bump_detected(self):
        self.assertEqual(_pending_update(_NEW, _OLD, ""), ("0.34.4", "0.35.0"))
        self.assertEqual(_pending_update(_NEW, _OLD, None), ("0.34.4", "0.35.0"))

    def test_requested_same_version_continues(self):
        self.assertEqual(_pending_update(_NEW, _OLD, "0.35.0"), ("0.34.4", "0.35.0"))

    def test_requested_other_version_is_a_new_update(self):
        self.assertIsNone(_pending_update(_NEW, _OLD, "0.36.0"))

    def test_no_bump(self):
        self.assertIsNone(_pending_update(_OLD, _OLD, ""))
        self.assertIsNone(_pending_update(_NEW.replace("Name", "Summary: x\nName"), _NEW, ""))

    def test_nothing_committed(self):
        self.assertIsNone(_pending_update(_NEW, None, ""))
        self.assertIsNone(_pending_update(_NEW, "no version here\n", ""))


class TestReadCommittedFile(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="pbuild_committed_")
        self.spec = Path(self.tmpdir) / "ollama.spec"
        self.spec.write_text(_OLD)
        self.manager = RpmSourceManager(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_head_content(self):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        for cmd in (["git", "init", "-q"], ["git", "add", "ollama.spec"],
                    ["git", "commit", "-q", "-m", "init"]):
            subprocess.run(cmd, cwd=self.tmpdir, check=True, env=env, capture_output=True)
        self.spec.write_text(_NEW)
        self.assertEqual(self.manager.read_committed_file(self.spec), _OLD)
        self.assertIsNone(self.manager.read_committed_file(Path(self.tmpdir) / "ollama.changes"))

    def test_osc_pristine_copy(self):
        for sub in (Path(".osc") / "sources", Path(".osc")):
            with self.subTest(layout=str(sub)):
                d = Path(self.tmpdir) / sub
                d.mkdir(parents=True, exist_ok=True)
                (d / "ollama.spec").write_text(_OLD)
                self.spec.write_text(_NEW)
                self.assertEqual(self.manager.read_committed_file(self.spec), _OLD)
                shutil.rmtree(Path(self.tmpdir) / ".osc")

    def test_untracked_directory(self):
        self.assertIsNone(self.manager.read_committed_file(self.spec))

    def test_outside_sandbox_rejected(self):
        with self.assertRaises(PermissionError):
            self.manager.read_committed_file("/etc/passwd")


if __name__ == '__main__':
    unittest.main()
