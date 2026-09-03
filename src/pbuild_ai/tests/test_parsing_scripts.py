"""Tests for AGENTS.md startup-script parsing.

Guards against a script mentioned anywhere in AGENTS.md (for example in an
"After changed the package version in the spec file" section) being picked up
as a startup script and therefore run at the wrong point of the workflow.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.parsing import parse_agents_md_scripts


class TestParseAgentsMdScripts(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="pbuild_agents_"))
        self.scripts = self.tmpdir / "tool-scripts"
        self.scripts.mkdir()
        (self.scripts / "post-update.sh").write_text("#!/bin/sh\n")
        (self.scripts / "prepare.sh").write_text("#!/bin/sh\n")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _startup(self, text):
        startup, _order, _skip = parse_agents_md_scripts(text, self.scripts)
        return startup

    def test_mention_outside_startup_section_is_not_a_startup_script(self):
        text = (
            "## hints for pbuild-ai\n\n"
            "## After changed the package version in the spec file:\n\n"
            "* make sure to call .agents/skills/post-update.sh to update vendored deps\n"
            "* abort when this is not possible\n"
        )
        self.assertNotIn("post-update.sh", self._startup(text))

    def test_startup_section_still_registers_scripts(self):
        text = "## Startup\n\n* prepare.sh\n"
        self.assertIn("prepare.sh", self._startup(text))

    def test_explicit_startup_script_directive_still_works(self):
        self.assertIn("prepare.sh", self._startup("startup-script: prepare.sh\n"))

    def test_empty_text_returns_empty_lists(self):
        self.assertEqual(parse_agents_md_scripts("", self.scripts), ([], [], []))


if __name__ == "__main__":
    unittest.main()
