"""Regression tests for #!RemoteAsset / #!CreateArchive repair.

The historical bug: a #!RemoteAsset: line followed by a correct standalone
#!CreateArchive line was treated as a 'merged line' and the #!CreateArchive
marker was deleted, breaking OBS asset generation.
"""

import os
import sys
import types
import unittest

_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules['yaml'] = _yaml

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.parsing import fix_remote_asset_formatting


CORRECT = ("Name: ollama\n"
           "Version: 0.33.3\n"
           "#!RemoteAsset: git+https://github.com/ggml-org/llama.cpp#%{llama_cpp_version}\n"
           "#!CreateArchive\n"
           "Source10:       llama.cpp-main.tar.xz\n"
           "#!RemoteAsset: git+https://github.com/ml-explore/mlx#%{mlx_version}\n"
           "#!CreateArchive\n"
           "Source11:       mlx-main.tar.xz\n")


class TestCorrectFormIsUntouched(unittest.TestCase):

    def test_standalone_markers_are_kept(self):
        out, changed, notes = fix_remote_asset_formatting(CORRECT)
        self.assertEqual(out, CORRECT)
        self.assertFalse(changed)
        self.assertEqual(notes, [])
        self.assertEqual(out.count('#!CreateArchive'), 2)

    def test_no_marker_at_all_is_untouched(self):
        text = "Name: plain\nSource0: https://example.com/%{name}.tar.gz\n"
        out, changed, _ = fix_remote_asset_formatting(text)
        self.assertEqual(out, text)
        self.assertFalse(changed)


class TestRepairs(unittest.TestCase):

    def test_marker_merged_on_asset_line(self):
        text = ("#!RemoteAsset: git+https://github.com/o/r#abc #!CreateArchive\n"
                "Source1: a.tar.xz\n")
        out, changed, _ = fix_remote_asset_formatting(text)
        self.assertTrue(changed)
        self.assertEqual(out, "#!RemoteAsset: git+https://github.com/o/r#abc\n"
                              "#!CreateArchive\nSource1: a.tar.xz\n")

    def test_missing_marker_is_added(self):
        text = "#!RemoteAsset: git+https://github.com/o/r#abc\nSource1: a.tar.xz\n"
        out, changed, _ = fix_remote_asset_formatting(text)
        self.assertTrue(changed)
        self.assertIn("#!RemoteAsset: git+https://github.com/o/r#abc\n#!CreateArchive\n", out)

    def test_every_asset_gets_its_own_marker(self):
        text = ("#!RemoteAsset: git+https://o/a#a\n#!CreateArchive\nSource10: a.tar.xz\n"
                "#!RemoteAsset: git+https://o/b#b\nSource11: b.tar.xz\n")
        out, changed, _ = fix_remote_asset_formatting(text)
        self.assertTrue(changed)
        self.assertEqual(out.count('#!RemoteAsset:'), 2)
        self.assertEqual(out.count('#!CreateArchive'), 2)

    def test_marker_glued_to_source_line_is_not_duplicated(self):
        text = "#!RemoteAsset: git+https://o/r#a\nSource1: a.tar.xz #!CreateArchive\n"
        out, changed, _ = fix_remote_asset_formatting(text)
        self.assertTrue(changed)
        self.assertEqual(out.count('#!CreateArchive'), 1)
        self.assertEqual(out, "#!RemoteAsset: git+https://o/r#a\n"
                              "#!CreateArchive\nSource1: a.tar.xz\n")

    def test_inline_asset_on_source_line(self):
        text = "Source1: #!RemoteAsset: git+https://o/r#a a.tar.xz\n"
        out, changed, _ = fix_remote_asset_formatting(text)
        self.assertTrue(changed)
        self.assertIn('#!RemoteAsset:', out.split('\n')[0])

    def test_source0_renamed_back_to_source(self):
        text = "#!RemoteAsset: git+https://o/r#a\n#!CreateArchive\nSource0: a.tar.xz\n"
        out, changed, _ = fix_remote_asset_formatting(text)
        self.assertTrue(changed)
        self.assertIn("Source: a.tar.xz", out)
        self.assertNotIn("Source0:", out)

    def test_filename_double_colon_url_is_stripped(self):
        text = ("Source0: file::https://example.com/f.tar.gz\n"
                "#!RemoteAsset: git+https://o/r#a\n#!CreateArchive\n")
        out, changed, _ = fix_remote_asset_formatting(text)
        self.assertTrue(changed)
        self.assertIn("https://example.com/f.tar.gz", out)
        self.assertNotIn('::https', out)


class TestIdempotency(unittest.TestCase):

    def test_repair_is_stable(self):
        inputs = [CORRECT,
                  "#!RemoteAsset: git+https://o/r#a #!CreateArchive\nSource1: a.tar.xz\n",
                  "#!RemoteAsset: git+https://o/r#a\nSource1: a.tar.xz #!CreateArchive\n",
                  "#!RemoteAsset: git+https://o/a#a\nSource10: a.tar.xz\n"
                  "#!RemoteAsset: git+https://o/b#b\nSource11: b.tar.xz\n"]
        for text in inputs:
            once, _ = fix_remote_asset_formatting(text)[:2]
            twice, changed_again, _ = fix_remote_asset_formatting(once)
            self.assertEqual(once, twice, f"not idempotent for {text!r}")
            self.assertFalse(changed_again, f"second pass still changed {text!r}")


if __name__ == "__main__":
    unittest.main()
