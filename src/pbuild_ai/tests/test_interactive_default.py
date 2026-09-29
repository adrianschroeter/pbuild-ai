"""Tests for the interactive default (terminal) and --non-interactive."""

import argparse
import sys
import types
import unittest
from unittest.mock import patch

# Mock yaml before any pbuild_ai import
_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules.setdefault('yaml', _yaml)

from pbuild_ai.pbuild_ai import _resolve_interactive


def _args(interactive=None, non_interactive=False):
    return argparse.Namespace(interactive=interactive, non_interactive=non_interactive)


class TestResolveInteractive(unittest.TestCase):

    def test_terminal_defaults_to_interactive(self):
        self.assertTrue(_resolve_interactive(_args(), isatty=lambda: True))

    def test_no_terminal_defaults_to_non_interactive(self):
        self.assertFalse(_resolve_interactive(_args(), isatty=lambda: False))

    def test_explicit_interactive_without_terminal(self):
        self.assertTrue(_resolve_interactive(_args(interactive=True), isatty=lambda: False))

    def test_non_interactive_wins_in_terminal(self):
        self.assertFalse(_resolve_interactive(_args(non_interactive=True), isatty=lambda: True))

    def test_closed_stdin_is_non_interactive(self):
        def boom():
            raise ValueError("I/O operation on closed file")
        self.assertFalse(_resolve_interactive(_args(), isatty=boom))

    def test_default_checks_stdin_and_stdout(self):
        with patch.object(sys, "stdin") as stdin, patch.object(sys, "stdout") as stdout:
            stdin.isatty.return_value = True
            stdout.isatty.return_value = False
            self.assertFalse(_resolve_interactive(_args()))
            stdout.isatty.return_value = True
            self.assertTrue(_resolve_interactive(_args()))


if __name__ == '__main__':
    unittest.main()
