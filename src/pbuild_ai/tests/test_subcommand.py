"""Tests for the command form: pbuild-ai generate --prompt "..." [DIR]."""

import sys
import types
import unittest

_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules.setdefault('yaml', _yaml)

from pbuild_ai.pbuild_ai import _apply_subcommand


def _nodir(path):
    return False


class TestApplySubcommand(unittest.TestCase):
    def test_generate_with_prompt_option(self):
        argv = ["generate", "--prompt", "Create gufo"]
        self.assertEqual(_apply_subcommand(argv, _nodir),
                         (["--generate", "Create gufo"], "generate"))

    def test_prompt_equals_form_and_dir(self):
        self.assertEqual(_apply_subcommand(["create", "--prompt=x", "gufo"], _nodir),
                         (["--create=x", "gufo"], "create"))

    def test_positional_prompt(self):
        self.assertEqual(_apply_subcommand(["modify", "add a patch", "."], _nodir),
                         (["--modify", "add a patch", "."], "modify"))

    def test_simple_commands(self):
        self.assertEqual(_apply_subcommand(["fix", "."], _nodir), (["--fix", "."], "fix"))
        self.assertEqual(_apply_subcommand(["update"], _nodir), (["--update"], "update"))

    def test_explicit_flag_kept(self):
        self.assertEqual(_apply_subcommand(["generate", "--generate", "x", "d"], _nodir),
                         (["--generate", "x", "d"], "generate"))

    def test_existing_directory_wins(self):
        argv = ["fix", "--update"]
        self.assertEqual(_apply_subcommand(argv, lambda p: True), (argv, None))

    def test_option_form_untouched(self):
        argv = ["--generate", "x", "dir"]
        self.assertEqual(_apply_subcommand(argv, _nodir), (argv, None))


if __name__ == "__main__":
    unittest.main()
