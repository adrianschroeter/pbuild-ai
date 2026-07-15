import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import sys
SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.utils import ReadCoverageTracker, ranges_covered, ranges_merge


class TestRangesCovered(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(ranges_covered([(0, 100)], 0, 100))

    def test_subrange(self):
        self.assertTrue(ranges_covered([(0, 100)], 10, 50))

    def test_not_covered(self):
        self.assertFalse(ranges_covered([(0, 50)], 60, 100))

    def test_infinite_end(self):
        self.assertTrue(ranges_covered([(10, None)], 20, None))

    def test_infinite_end_subrange(self):
        self.assertTrue(ranges_covered([(10, None)], 10, 200))

    def test_infinite_not_covered(self):
        self.assertFalse(ranges_covered([(10, 50)], 5, 20))

    def test_empty_ranges(self):
        self.assertFalse(ranges_covered([], 0, 100))

    def test_whole_file_covered_by_none_end(self):
        self.assertTrue(ranges_covered([(0, None)], 0, None))

    def test_whole_file_covered_by_none_end_partial(self):
        self.assertTrue(ranges_covered([(0, None)], 50, 100))


class TestRangesMerge(unittest.TestCase):
    def test_merge_adjacent(self):
        self.assertEqual(ranges_merge([(0, 10)], 10, 20), [(0, 20)])

    def test_merge_overlapping(self):
        self.assertEqual(ranges_merge([(0, 15)], 10, 20), [(0, 20)])

    def test_merge_separate(self):
        self.assertEqual(ranges_merge([(0, 10)], 20, 30), [(0, 10), (20, 30)])

    def test_merge_subsumed(self):
        result = ranges_merge([(0, 100)], 10, 50)
        self.assertEqual(result, [(0, 100)])

    def test_merge_infinite_end(self):
        result = ranges_merge([(0, 50)], 50, None)
        self.assertEqual(result, [(0, None)])

    def test_merge_empty(self):
        self.assertEqual(ranges_merge([], 0, 10), [(0, 10)])


class TestReadCoverageTracker(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="read_coverage_test_")
        self.ws = self.tmpdir

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, path, content):
        full = os.path.join(self.tmpdir, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        Path(full).write_text(content)
        return full

    def _hash(self, content):
        return hashlib.md5(content.encode()).hexdigest()

    def _manager(self):
        m = MagicMock()
        def read_file_safe(path):
            return Path(path).read_text()
        m.read_file_safe = read_file_safe
        return m

    # --- filter_reads: no coverage ---

    def test_filter_no_coverage_returns_all(self):
        tracker = ReadCoverageTracker()
        self._write("foo.c", "int x;\n")
        calls = [("read_file", {"path": "foo.c"})]
        filtered, skipped = tracker.filter_reads(calls, self.ws, self._manager())
        self.assertEqual(filtered, calls)
        self.assertEqual(skipped, {})

    # --- filter_reads: skips when fully covered ---

    def test_filter_skips_fully_covered_file(self):
        tracker = ReadCoverageTracker()
        content = "int x;\nfloat y;\n"
        self._write("foo.c", content)
        calls = [("read_file", {"path": "foo.c"})]
        # Seed coverage so the full file (no offset/limit => 0, None) is covered
        tracker._file_coverage[os.path.join(self.ws, "foo.c")] = {
            "hash": self._hash(content),
            "ranges": [(0, None)]
        }
        filtered, skipped = tracker.filter_reads(calls, self.ws, self._manager())
        self.assertEqual(filtered, [])
        self.assertIn(0, skipped)
        self.assertIn("READ SKIP", skipped[0])

    # --- filter_reads: skips partial range when covered ---

    def test_filter_skips_partial_range(self):
        tracker = ReadCoverageTracker()
        content = "0123456789"
        self._write("foo.c", content)
        calls = [("read_file", {"path": "foo.c", "offset": 2, "limit": 5})]
        tracker._file_coverage[os.path.join(self.ws, "foo.c")] = {
            "hash": self._hash(content),
            "ranges": [(0, 10)]
        }
        filtered, skipped = tracker.filter_reads(calls, self.ws, self._manager())
        self.assertEqual(filtered, [])
        self.assertIn(0, skipped)

    # --- filter_reads: does not skip when range not covered ---

    def test_filter_does_not_skip_uncovered_range(self):
        tracker = ReadCoverageTracker()
        content = "0123456789"
        self._write("foo.c", content)
        calls = [("read_file", {"path": "foo.c", "offset": 5, "limit": 10})]
        tracker._file_coverage[os.path.join(self.ws, "foo.c")] = {
            "hash": self._hash(content),
            "ranges": [(0, 5)]
        }
        filtered, skipped = tracker.filter_reads(calls, self.ws, self._manager())
        self.assertEqual(filtered, calls)
        self.assertEqual(skipped, {})

    # --- filter_reads: hash mismatch invalidates coverage ---

    def test_filter_hash_mismatch_does_not_skip(self):
        tracker = ReadCoverageTracker()
        self._write("foo.c", "original content")
        calls = [("read_file", {"path": "foo.c"})]
        tracker._file_coverage[os.path.join(self.ws, "foo.c")] = {
            "hash": self._hash("stale content"),
            "ranges": [(0, None)]
        }
        filtered, skipped = tracker.filter_reads(calls, self.ws, self._manager())
        self.assertEqual(filtered, calls)
        self.assertEqual(skipped, {})

    # --- filter_reads: no manager means no hash check (trusts coverage) ---

    def test_filter_no_manager_checks_only_ranges(self):
        tracker = ReadCoverageTracker()
        content = "0123456789"
        self._write("foo.c", content)
        calls = [("read_file", {"path": "foo.c"})]
        tracker._file_coverage[os.path.join(self.ws, "foo.c")] = {
            "hash": self._hash(content),
            "ranges": [(0, None)]
        }
        filtered, skipped = tracker.filter_reads(calls, self.ws, self._manager())
        self.assertEqual(filtered, [])
        self.assertIn(0, skipped)

    # --- filter_reads: archive coverage ---

    def test_filter_skips_archive_read(self):
        tracker = ReadCoverageTracker()
        self._write("src.tar.gz", "dummy archive content")
        calls = [("read_file_from_archive", {"archive_path": "src.tar.gz", "file_path": "pkg/a.c"})]
        tracker._archive_coverage[(os.path.join(self.ws, "src.tar.gz"), "pkg/a.c")] = [(0, None)]
        filtered, skipped = tracker.filter_reads(calls, self.ws)
        self.assertEqual(filtered, [])
        self.assertIn(0, skipped)

    # --- filter_reads: archive not covered ---

    def test_filter_does_not_skip_uncovered_archive(self):
        tracker = ReadCoverageTracker()
        self._write("src.tar.gz", "dummy")
        calls = [("read_file_from_archive", {"archive_path": "src.tar.gz", "file_path": "pkg/b.c"})]
        tracker._archive_coverage[(os.path.join(self.ws, "src.tar.gz"), "pkg/a.c")] = [(0, None)]
        filtered, skipped = tracker.filter_reads(calls, self.ws)
        self.assertEqual(filtered, calls)
        self.assertEqual(skipped, {})

    # --- filter_reads: no workspace_dir ---

    def test_filter_no_workspace_returns_all(self):
        tracker = ReadCoverageTracker()
        calls = [("read_file", {"path": "foo.c"})]
        filtered, skipped = tracker.filter_reads(calls, workspace_dir=None)
        self.assertEqual(filtered, calls)
        self.assertEqual(skipped, {})

    # --- filter_reads: file does not exist ---

    def test_filter_nonexistent_file(self):
        tracker = ReadCoverageTracker()
        calls = [("read_file", {"path": "nonexistent.c"})]
        filtered, skipped = tracker.filter_reads(calls, self.ws)
        self.assertEqual(filtered, calls)
        self.assertEqual(skipped, {})

    # --- filter_reads: mixed covered and uncovered ---

    def test_filter_mixed(self):
        tracker = ReadCoverageTracker()
        content = "a" * 200
        self._write("covered.c", content)
        self._write("new.c", content)
        tracker._file_coverage[os.path.join(self.ws, "covered.c")] = {
            "hash": self._hash(content),
            "ranges": [(0, None)]
        }
        calls = [
            ("read_file", {"path": "covered.c"}),
            ("read_file", {"path": "new.c"})
        ]
        filtered, skipped = tracker.filter_reads(calls, self.ws, self._manager())
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0], ("read_file", {"path": "new.c"}))
        self.assertIn(0, skipped)
        self.assertNotIn(1, skipped)

    # --- update_from_results: file read ---

    def test_update_file_read(self):
        tracker = ReadCoverageTracker()
        content = "0123456789"
        fpath = self._write("foo.c", content)
        calls = [("read_file", {"path": "foo.c"})]
        results = [content]
        tracker.update_from_results(calls, results, self.ws, self._manager())
        fn = str(fpath)
        self.assertIn(fn, tracker._file_coverage)
        self.assertEqual(tracker._file_coverage[fn]["hash"], self._hash(content))
        self.assertIn((0, None), tracker._file_coverage[fn]["ranges"])

    # --- update_from_results: file read with offset/limit ---

    def test_update_file_read_partial(self):
        tracker = ReadCoverageTracker()
        content = "0123456789"
        fpath = self._write("foo.c", content)
        calls = [("read_file", {"path": "foo.c", "offset": 2, "limit": 5})]
        results = ["23456"]
        tracker.update_from_results(calls, results, self.ws, self._manager())
        fn = str(fpath)
        self.assertEqual(tracker._file_coverage[fn]["ranges"], [(2, 7)])

    # --- update_from_results: multiple reads merge ---

    def test_update_merges_ranges(self):
        tracker = ReadCoverageTracker()
        content = "0123456789" * 10
        fpath = self._write("foo.c", content)
        calls = [("read_file", {"path": "foo.c", "offset": 0, "limit": 10})]
        results = ["0123456789"]
        tracker.update_from_results(calls, results, self.ws, self._manager())
        calls2 = [("read_file", {"path": "foo.c", "offset": 5, "limit": 10})]
        results2 = ["56789" + content[10:15]]
        tracker.update_from_results(calls2, results2, self.ws, self._manager())
        fn = str(fpath)
        self.assertEqual(tracker._file_coverage[fn]["ranges"], [(0, 15)])

    # --- update_from_results: error results don't update coverage ---

    def test_update_ignores_errors(self):
        tracker = ReadCoverageTracker()
        self._write("foo.c", "content")
        calls = [("read_file", {"path": "foo.c"})]
        results = ["Error: something went wrong"]
        tracker.update_from_results(calls, results, self.ws, self._manager())
        fn = os.path.join(self.ws, "foo.c")
        self.assertNotIn(fn, tracker._file_coverage)

    # --- update_from_results: archive read ---

    def test_update_archive_read(self):
        tracker = ReadCoverageTracker()
        self._write("archive.tar.gz", "dummy")
        calls = [("read_file_from_archive", {"archive_path": "archive.tar.gz", "file_path": "pkg/a.c"})]
        results = ["int x;\n"]
        tracker.update_from_results(calls, results, self.ws)
        key = (os.path.join(self.ws, "archive.tar.gz"), "pkg/a.c")
        self.assertIn(key, tracker._archive_coverage)
        self.assertEqual(tracker._archive_coverage[key], [(0, None)])

    # --- update_from_results: archive read with offset/limit ---

    def test_update_archive_read_partial(self):
        tracker = ReadCoverageTracker()
        self._write("archive.tar.gz", "dummy")
        calls = [("read_file_from_archive", {"archive_path": "archive.tar.gz", "file_path": "pkg/a.c", "offset": 2, "limit": 5})]
        results = ["23456"]
        tracker.update_from_results(calls, results, self.ws)
        key = (os.path.join(self.ws, "archive.tar.gz"), "pkg/a.c")
        self.assertEqual(tracker._archive_coverage[key], [(2, 7)])

    # --- update_from_results: "READ SKIP" results don't update coverage ---

    def test_update_ignores_skip_results(self):
        tracker = ReadCoverageTracker()
        self._write("foo.c", "content")
        calls = [("read_file", {"path": "foo.c"})]
        results = ["READ SKIP: foo.c already read"]
        tracker.update_from_results(calls, results, self.ws, self._manager())
        fn = os.path.join(self.ws, "foo.c")
        self.assertNotIn(fn, tracker._file_coverage)

    # --- update_from_results: "OK:" results don't update coverage ---

    def test_update_ignores_ok_results(self):
        tracker = ReadCoverageTracker()
        self._write("foo.c", "content")
        calls = [("read_file", {"path": "foo.c"})]
        results = ["OK: done"]
        tracker.update_from_results(calls, results, self.ws, self._manager())
        fn = os.path.join(self.ws, "foo.c")
        self.assertNotIn(fn, tracker._file_coverage)

    # --- merge_results ---

    def test_merge_no_skips(self):
        calls = [("read_file", {"path": "a"}), ("read_file", {"path": "b"})]
        results = ReadCoverageTracker.merge_results(calls, ["content a", "content b"], {})
        self.assertEqual(results, ["content a", "content b"])

    def test_merge_with_skips(self):
        calls = [("read_file", {"path": "a"}), ("read_file", {"path": "b"}), ("read_file", {"path": "c"})]
        skipped = {0: "SKIP a", 2: "SKIP c"}
        filtered_results = ["content b"]
        results = ReadCoverageTracker.merge_results(calls, filtered_results, skipped)
        self.assertEqual(results, ["SKIP a", "content b", "SKIP c"])

    def test_merge_all_skipped(self):
        calls = [("read_file", {"path": "a"})]
        results = ReadCoverageTracker.merge_results(calls, [], {0: "SKIP a"})
        self.assertEqual(results, ["SKIP a"])

    def test_merge_empty(self):
        results = ReadCoverageTracker.merge_results([], [], {})
        self.assertEqual(results, [])

    # --- integration: realistic round trip ---

    def test_round_trip_skips_second_read(self):
        tracker = ReadCoverageTracker()
        content = "0123456789"
        self._write("foo.c", content)

        # Round 1: read the full file
        calls1 = [("read_file", {"path": "foo.c"})]
        filtered1, skipped1 = tracker.filter_reads(calls1, self.ws, self._manager())
        self.assertEqual(len(filtered1), 1)
        exec1 = [content]
        results1 = ReadCoverageTracker.merge_results(calls1, exec1, skipped1)
        tracker.update_from_results(calls1, results1, self.ws, self._manager())

        # Round 2: same read should be skipped
        calls2 = [("read_file", {"path": "foo.c"})]
        filtered2, skipped2 = tracker.filter_reads(calls2, self.ws, self._manager())
        self.assertEqual(filtered2, [])
        self.assertIn(0, skipped2)
        results2 = ReadCoverageTracker.merge_results(calls2, [], skipped2)
        self.assertIn("READ SKIP", results2[0])

    def test_round_trip_re_read_after_edit(self):
        tracker = ReadCoverageTracker()
        fpath = self._write("foo.c", "old content")

        # Read original
        calls1 = [("read_file", {"path": "foo.c"})]
        filtered1, skipped1 = tracker.filter_reads(calls1, self.ws, self._manager())
        results1 = ReadCoverageTracker.merge_results(calls1, ["old content"], skipped1)
        tracker.update_from_results(calls1, results1, self.ws, self._manager())

        # Modify the file (simulate AI edit)
        Path(fpath).write_text("new content")

        # Read again — should NOT be skipped because hash changed
        calls2 = [("read_file", {"path": "foo.c"})]
        filtered2, _skipped2 = tracker.filter_reads(calls2, self.ws, self._manager())
        self.assertEqual(len(filtered2), 1, "Should re-read after edit (hash mismatch)")


if __name__ == "__main__":
    unittest.main()
