"""Tests for RpmSourceManager OSC detection and _target_args: builds against an
osc checkout's project + first build repository instead of the default dist/preset."""

import os
import sys
import tempfile
import types
import unittest

# Mock yaml before any pbuild_ai import
_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules['yaml'] = _yaml

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.workspace import RpmSourceManager


def _make_osc(base, project, repos_lines, apiurl=None):
    osc = os.path.join(base, ".osc")
    os.makedirs(osc)
    with open(os.path.join(osc, "_project"), "w") as f:
        f.write(project)
    with open(os.path.join(osc, "_build_repositories"), "w") as f:
        f.write(repos_lines)
    if apiurl is not None:
        with open(os.path.join(osc, "_apiurl"), "w") as f:
            f.write(apiurl)


class TestOscDetection(unittest.TestCase):

    def test_no_osc_dir(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = RpmSourceManager(td)
            self.assertFalse(mgr.osc_mode)
            self.assertIsNone(mgr.osc_project)
            self.assertIsNone(mgr.osc_repository)

    def test_full_osc_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            _make_osc(td, "system:ha:unstable\n",
                      "standard x86_64\nother aarch64\n",
                      "https://api.opensuse.org\n")
            mgr = RpmSourceManager(td)
            self.assertTrue(mgr.osc_mode)
            self.assertEqual(mgr.osc_project, "system:ha:unstable")
            self.assertEqual(mgr.osc_repository, "standard")
            self.assertEqual(mgr.osc_apiurl, "https://api.opensuse.org")

    def test_default_apiurl_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            _make_osc(td, "p:q\n", "  obs_standard x86_64\n")
            mgr = RpmSourceManager(td)
            self.assertEqual(mgr.osc_apiurl, "https://api.opensuse.org")
            self.assertEqual(mgr.osc_repository, "obs_standard")

    def test_empty_repos_no_osc(self):
        with tempfile.TemporaryDirectory() as td:
            _make_osc(td, "p:q\n", "\n   \n")
            mgr = RpmSourceManager(td)
            self.assertFalse(mgr.osc_mode)

    def test_missing_project_no_osc(self):
        with tempfile.TemporaryDirectory() as td:
            osc = os.path.join(td, ".osc")
            os.makedirs(osc)
            with open(os.path.join(osc, "_build_repositories"), "w") as f:
                f.write("standard x86_64\n")
            mgr = RpmSourceManager(td)
            self.assertFalse(mgr.osc_mode)


class TestTargetArgs(unittest.TestCase):

    def test_fallback_dist(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = RpmSourceManager(td)
            self.assertEqual(mgr._target_args("tumbleweed", None, "tumbleweed", True),
                             ["--dist", "tumbleweed"])
            self.assertEqual(mgr._target_args(None, "mypreset", "tumbleweed", False),
                             ["--preset", "mypreset"])
            self.assertEqual(mgr._target_args(None, None, "tumbleweed", False), [])
            self.assertEqual(mgr._target_args(None, None, "tumbleweed", True),
                             ["--dist", "tumbleweed"])

    def test_osc_target_overrides_all(self):
        with tempfile.TemporaryDirectory() as td:
            _make_osc(td, "system:ha:unstable\n", "standard x86_64\n",
                      "https://custom.api\n")
            mgr = RpmSourceManager(td)
            expected = ["--obs", "https://custom.api", "--dist",
                        "obs://system:ha:unstable/standard"]
            # osc wins even when preset/dist are supplied
            self.assertEqual(mgr._target_args("leap", "somepreset", "tumbleweed", True), expected)
            self.assertEqual(mgr._target_args(None, None, "tumbleweed", False), expected)


if __name__ == "__main__":
    unittest.main()
