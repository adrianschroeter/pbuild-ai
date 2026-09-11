"""Tests for the per-round AI spinner/log label helpers in llm_client.

Guards that multi-round AI calls render a distinct label each round (round
counter + last round's activity) instead of repeating a static suffix.
"""

import os
import sys
import unittest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.llm_client import _format_round_task, _summarize_round_calls


class TestFormatRoundTask(unittest.TestCase):
    def test_base_round_counter_only(self):
        self.assertEqual(_format_round_task("Running post-update steps", 1, 30, ""),
                         "Running post-update steps (1/30)")

    def test_with_activity(self):
        self.assertEqual(
            _format_round_task("Running post-update steps", 3, 30,
                               "edit_file ollama.changes -> skipped"),
            "Running post-update steps (3/30) — edit_file ollama.changes -> skipped")

    def test_no_base_shows_activity_only(self):
        self.assertEqual(_format_round_task("", 2, 30, "read_file x -> ok"),
                         "read_file x -> ok")

    def test_infinite_rounds_display(self):
        self.assertEqual(_format_round_task("Analyzing build failure", 15, 999999, ""),
                         "Analyzing build failure (15/999999)")

    def test_empty_total_shows_base_only(self):
        self.assertEqual(_format_round_task("Backup", 1, 0, ""), "Backup")

    def test_fully_empty(self):
        self.assertEqual(_format_round_task("", 1, 30, ""), "")


class TestSummarizeRoundCalls(unittest.TestCase):
    def test_ok_status(self):
        self.assertEqual(
            _summarize_round_calls([("edit_file", {"path": "x.spec"}, "OK: Edited x.spec")]),
            "edit_file x.spec -> ok")

    def test_skipped_status(self):
        self.assertEqual(
            _summarize_round_calls([("write_file", {"path": "x.changes"},
                                     "SKIP: duplicate changelog entries")]),
            "write_file x.changes -> skipped")

    def test_error_status(self):
        self.assertEqual(
            _summarize_round_calls([("run_tool_script", {"script_name": "a.sh"},
                                     "Error: script missing")]),
            "run_tool_script a.sh -> error")

    def test_other_status_maps_to_done(self):
        self.assertEqual(
            _summarize_round_calls([("web_fetch", {"url": "https://x.example"},
                                     "[Fetched 42 bytes]")]),
            "web_fetch https://x.example -> done")

    def test_script_name_takes_precedence(self):
        self.assertEqual(
            _summarize_round_calls([("run_tool_script",
                                     {"script_name": "post-update.sh", "args": []},
                                     "OK: ran")]),
            "run_tool_script post-update.sh -> ok")

    def test_multiple_calls_joined(self):
        self.assertEqual(
            _summarize_round_calls([
                ("read_file", {"path": "a.spec"}, "[FIX] read_file: a.spec"),
                ("edit_file", {"path": "b.spec"}, "OK: Edited b.spec"),
            ]),
            "read_file a.spec -> done; edit_file b.spec -> ok")

    def test_content_arg_falls_back_and_truncates(self):
        result = _summarize_round_calls([("write_file", {"path": "p", "content": "x" * 200},
                                          "OK: Wrote p.spec")])
        self.assertIn("write_file p -> ok", result)
        self.assertTrue(len("p") < 60)

    def test_empty_list(self):
        self.assertEqual(_summarize_round_calls([]), "")


if __name__ == "__main__":
    unittest.main()