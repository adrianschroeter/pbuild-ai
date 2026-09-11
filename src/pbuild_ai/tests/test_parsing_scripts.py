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

from pbuild_ai.parsing import parse_agents_md_scripts, parse_post_update_scripts


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


class TestParsePostUpdateScripts(unittest.TestCase):
    def test_single_line_rule(self):
        text = (
            "## After changed the package version in the spec file:\n"
            "* make sure to call .agents/skills/post-update.sh to update vendored deps\n"
        )
        self.assertEqual(parse_post_update_scripts(text),
                         [".agents/skills/post-update.sh"])

    def test_plain_name_on_keyword_neighbor_line(self):
        text = ("After changed the package version in the spec file, make sure to "
                "call update_references.sh to update vendored deps.\n")
        self.assertEqual(parse_post_update_scripts(text), ["update_references.sh"])

    def test_section_based_rules(self):
        text = ("## Post-Update\n"
                "Run these after a version bump:\n"
                "- update_references.sh\n"
                "- skills/gen-abi.sh\n")
        self.assertEqual(parse_post_update_scripts(text),
                         ["update_references.sh", "skills/gen-abi.sh"])

    def test_script_named_on_line_after_version_hint(self):
        # Mirrors .agents/skills/OLLAMA_VERSION_UPDATE.md phrasing: the change
        # hint is on the previous line, the script reference on the next one.
        text = ("After the `Version:` tag in `ollama.spec` has been changed, run the vendored\n"
                "source refresh script `update_references.sh`.\n")
        self.assertEqual(parse_post_update_scripts(text), ["update_references.sh"])

    def test_explicit_marker(self):
        text = "post-update-script: .agents/skills/rebuild-assets.sh\n"
        self.assertEqual(parse_post_update_scripts(text),
                         [".agents/skills/rebuild-assets.sh"])

    def test_no_keywords_means_no_scripts(self):
        text = ("Some unrelated housekeeping mentioning make.sh and build.sh "
                "without any relevant context.\n")
        self.assertEqual(parse_post_update_scripts(text), [])

    def test_keyword_without_script(self):
        self.assertEqual(
            parse_post_update_scripts("Versioning is handled manually.\n"), [])

    def test_deduplicates(self):
        text = ("After changing the version, call .agents/skills/update.sh.\n"
                "## Post-Update\n"
                ".agents/skills/update.sh must also run.\n")
        self.assertEqual(parse_post_update_scripts(text),
                         [".agents/skills/update.sh"])

    def test_empty_text_returns_empty(self):
        self.assertEqual(parse_post_update_scripts(""), [])
        self.assertEqual(parse_post_update_scripts(None), [])


if __name__ == "__main__":
    unittest.main()
