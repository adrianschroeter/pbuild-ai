"""Interactive prompts accept a free-text instruction for the AI in place
of a plain yes/no or selection answer."""

import io
import unittest
from contextlib import redirect_stdout

from pbuild_ai.tools import (
    ask_tool_selection,
    ask_yes_no_or_instruction,
    instruction_messages,
    _ask_tool_script_consent,
)


def _answer(text):
    return lambda _prompt: text


def _eof(_prompt):
    raise EOFError


CALLS = [("write_file", {"path": "a.spec"}), ("edit_file", {"path": "b"}), ("remove_file", {"path": "c"})]


class TestYesNoOrInstruction(unittest.TestCase):
    def test_yes_and_no(self):
        for text, expected in (("y", True), ("YES", True), ("n", False), ("no", False), ("", False)):
            self.assertEqual(ask_yes_no_or_instruction("? ", _answer(text)), (expected, ""), text)

    def test_free_text_is_instruction(self):
        self.assertEqual(ask_yes_no_or_instruction("? ", _answer("  use the git tag instead ")),
                         (None, "use the git tag instead"))

    def test_eof_means_no(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ask_yes_no_or_instruction("? ", _eof), (False, ""))


class TestToolSelection(unittest.TestCase):
    def _select(self, text):
        with redirect_stdout(io.StringIO()) as out:
            result = ask_tool_selection(CALLS, _answer(text))
        return result, out.getvalue()

    def test_menu_mentions_instruction(self):
        _, out = self._select("a")
        self.assertIn("instruction", out)

    def test_all(self):
        for text in ("a", "all", "y"):
            self.assertEqual(self._select(text)[0], (CALLS, ""))

    def test_none(self):
        for text in ("n", "none", ""):
            self.assertEqual(self._select(text)[0], ([], ""))

    def test_numbers(self):
        self.assertEqual(self._select("1,3")[0], ([CALLS[0], CALLS[2]], ""))
        self.assertEqual(self._select("2 3")[0], ([CALLS[1], CALLS[2]], ""))
        self.assertEqual(self._select("9")[0], ([], ""))

    def test_text_is_instruction(self):
        self.assertEqual(self._select("Don't remove c, patch it")[0], ([], "Don't remove c, patch it"))

    def test_eof(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ask_tool_selection(CALLS, _eof), ([], ""))


class TestInstructionMessages(unittest.TestCase):
    def test_every_call_gets_a_result_and_user_message_follows(self):
        tool_calls = [{"function": {"name": "write_file", "arguments": {}}},
                      {"function": {"name": "edit_file", "arguments": {}}}]
        message = {"content": "plan", "tool_calls": tool_calls, "thinking": "hmm"}
        msgs = instruction_messages(message, ["write_file", "edit_file"], "keep the old license")
        self.assertEqual([m["role"] for m in msgs], ["assistant", "tool", "tool", "user"])
        self.assertEqual(msgs[0]["tool_calls"], tool_calls)
        self.assertEqual(msgs[0]["thinking"], "hmm")
        self.assertEqual([m["name"] for m in msgs[1:3]], ["write_file", "edit_file"])
        self.assertIn("Not executed", msgs[1]["content"])
        self.assertIn("keep the old license", msgs[3]["content"])


class TestScriptConsent(unittest.TestCase):
    def _ask(self, text):
        with redirect_stdout(io.StringIO()):
            return _ask_tool_script_consent("/x/post.sh", ["--a"], input_fn=_answer(text))

    def test_answers(self):
        self.assertEqual(self._ask("y"), (True, ""))
        self.assertEqual(self._ask(""), (False, ""))
        self.assertEqual(self._ask("run make instead"), (False, "run make instead"))


if __name__ == "__main__":
    unittest.main()
