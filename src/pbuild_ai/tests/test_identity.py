"""Tests for the stored/proposed changelog author (identity.py)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai import identity
from pbuild_ai.identity import (
    load_identity, propose_identity, resolve_changelog_author, save_identity,
    split_address,
)
from pbuild_ai.skills.changelog_skill import write_changelog_entry


def _no_input(prompt):
    raise AssertionError(f"must not ask: {prompt}")


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = self.tmp / "pbuild-ai" / "config"

    def tearDown(self):
        self._tmp.cleanup()


class TestSplitAddress(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(split_address("Jane Doe <jane@example.org>"), ("Jane Doe", "jane@example.org"))
        self.assertEqual(split_address("jane@example.org"), ("", "jane@example.org"))
        self.assertEqual(split_address("not an address"), ("", ""))
        self.assertEqual(split_address(None), ("", ""))


class TestConfigFile(_Base):
    def test_save_and_load_keeps_other_sections(self):
        self.cfg.parent.mkdir(parents=True)
        self.cfg.write_text("[other]\nkey = value\n", encoding="utf-8")
        save_identity("Jane Doe", "jane@example.org", self.cfg)
        self.assertEqual(load_identity(self.cfg), ("Jane Doe", "jane@example.org"))
        self.assertIn("[other]", self.cfg.read_text(encoding="utf-8"))

    def test_missing_file(self):
        self.assertEqual(load_identity(self.tmp / "nope"), ("", ""))

    def test_default_path_honors_xdg(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp)}):
            self.assertEqual(identity.config_path(), self.tmp / "pbuild-ai" / "config")


class TestProposal(_Base):
    def test_first_source_wins_per_field(self):
        sources = (lambda: ("", "osc@example.org"),
                   lambda: ("Git Name", "git@example.org"))
        self.assertEqual(propose_identity(sources), ("Git Name", "osc@example.org"))

    def test_failing_source_is_ignored(self):
        def boom():
            raise OSError("no git")
        self.assertEqual(propose_identity((boom, lambda: ("N", "n@x.org"))), ("N", "n@x.org"))

    def test_oscrc_default_api_section(self):
        home = self.tmp
        (home / ".oscrc").write_text(
            "[general]\napiurl = https://api.suse.de\n\n"
            "[https://api.opensuse.org]\nuser = a\nemail = public@example.org\n\n"
            "[https://api.suse.de]\nuser = a\nemail = internal@example.org\nrealname = Jane Doe\n",
            encoding="utf-8")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(home / "nocfg")}):
            self.assertEqual(identity._osc_identity(home), ("Jane Doe", "internal@example.org"))

    def test_env(self):
        self.assertEqual(identity._env_identity({"EMAIL": "Jane <jane@example.org>"}),
                         ("Jane", "jane@example.org"))
        self.assertEqual(identity._env_identity({"DEBEMAIL": "j@x.org", "DEBFULLNAME": "J X"}),
                         ("J X", "j@x.org"))


class TestResolve(_Base):
    SOURCES = (lambda: ("Proposed Name", "proposed@example.org"),)

    def test_configured_value_is_used_without_asking(self):
        save_identity("Jane Doe", "jane@example.org", self.cfg)
        self.assertEqual(
            resolve_changelog_author("", True, self.cfg, _no_input, self.SOURCES),
            "Jane Doe <jane@example.org>")

    def test_cli_bare_email_gets_configured_name(self):
        save_identity("Jane Doe", "jane@example.org", self.cfg)
        self.assertEqual(
            resolve_changelog_author("other@example.org", True, self.cfg, _no_input, self.SOURCES),
            "Jane Doe <other@example.org>")

    def test_cli_full_address_wins(self):
        self.assertEqual(
            resolve_changelog_author("X Y <x@y.org>", True, self.cfg, _no_input, self.SOURCES),
            "X Y <x@y.org>")

    def test_interactive_accepts_proposal_and_stores_it(self):
        answers = iter(["", ""])
        author = resolve_changelog_author("", True, self.cfg, lambda p: next(answers), self.SOURCES)
        self.assertEqual(author, "Proposed Name <proposed@example.org>")
        self.assertEqual(load_identity(self.cfg), ("Proposed Name", "proposed@example.org"))

    def test_interactive_override_and_email_validation(self):
        answers = iter(["Jane Doe", "not-an-email", "jane@example.org"])
        author = resolve_changelog_author("", True, self.cfg, lambda p: next(answers), self.SOURCES)
        self.assertEqual(author, "Jane Doe <jane@example.org>")
        self.assertEqual(load_identity(self.cfg), ("Jane Doe", "jane@example.org"))

    def test_non_interactive_uses_proposal_without_storing(self):
        author = resolve_changelog_author("", False, self.cfg, _no_input, self.SOURCES)
        self.assertEqual(author, "Proposed Name <proposed@example.org>")
        self.assertFalse(self.cfg.exists())

    def test_non_interactive_without_anything(self):
        with mock.patch.object(identity, "_passwd_name", return_value=""):
            self.assertEqual(
                resolve_changelog_author("", False, self.cfg, _no_input, (lambda: ("", ""),)), "")


class TestChangelogHeaderAuthor(_Base):
    def _header(self, author):
        path = self.tmp / "foo.changes"
        write_changelog_entry(path, "1.0", "1.1", author)
        return path.read_text(encoding="utf-8").splitlines()[1]

    def test_full_address_used_as_given(self):
        self.assertTrue(self._header("Jane Doe <jane@example.org>").endswith(
            " - Jane Doe <jane@example.org>"))

    def test_bare_email_keeps_pbuild_ai_name(self):
        self.assertTrue(self._header("jane@example.org").endswith(" - pbuild-ai <jane@example.org>"))


if __name__ == "__main__":
    unittest.main()
