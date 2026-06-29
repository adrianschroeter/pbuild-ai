"""Tests for _resolve_url_references: auto-download of URL-based Source:/Patch: lines."""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Mock yaml before any pbuild_ai import
_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules['yaml'] = _yaml

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.context import PbuildContext


def make_ctx(tmpdir, spec_content, spec_name="testpkg.spec"):
    spec_path = Path(tmpdir) / spec_name
    spec_path.write_text(spec_content)
    manager = MagicMock()
    manager.base_dir = Path(tmpdir)
    manager.read_file_safe.return_value = spec_content
    ctx = PbuildContext(
        workspace_dir=tmpdir,
        fix_mode=False,
        update_version=None,
        package_filter=[spec_name],
        project_mode=False,
        preset=None,
        show_buildlog=False,
        allow_tool_scripts=False,
        deep_analyze=False,
        fix_attempts=10,
        debug=False,
        interactive=False,
    )
    ctx.spec_files = [spec_path]
    ctx.manager = manager
    return ctx, spec_path


class TestResolveUrlReferences(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="modify_urls_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- No URL references in spec ---

    def test_no_urls(self):
        content = "Name: testpkg\nVersion: 1.0\nPatch0: fix.patch\nSource0: test.tar.gz\n"
        ctx, spec_path = make_ctx(self.tmpdir, content)
        from pbuild_ai.modify_mode import _resolve_url_references
        _resolve_url_references(ctx)
        self.assertEqual(spec_path.read_text(), content,
                         "Spec with only local refs must remain unchanged")

    # --- Patch URL ---

    def test_patch_url_downloaded(self):
        content = "Name: testpkg\nVersion: 1.0\nPatch0: https://example.com/fix.patch\n"
        ctx, spec_path = make_ctx(self.tmpdir, content)

        patch_data = b"--- a/foo\n+++ b/foo\n@@ -1 +1 @@\n-old\n+new\n"
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = patch_data
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            from pbuild_ai.modify_mode import _resolve_url_references
            _resolve_url_references(ctx)

        expected = "Name: testpkg\nVersion: 1.0\nPatch0: fix.patch\n"
        self.assertEqual(spec_path.read_text(), expected,
                         "Spec must reference local fix.patch")
        downloaded = spec_path.parent / "fix.patch"
        self.assertTrue(downloaded.exists(), "fix.patch must be downloaded")
        self.assertEqual(downloaded.read_bytes(), patch_data,
                         "Downloaded content must match")

    # --- Source URL ---

    def test_source_url_downloaded(self):
        content = "Name: testpkg\nVersion: 1.0\nSource0: https://example.com/test-1.0.tar.gz\n"
        ctx, spec_path = make_ctx(self.tmpdir, content)

        archive_data = b"gzip-compressed-content"
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = archive_data
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            from pbuild_ai.modify_mode import _resolve_url_references
            _resolve_url_references(ctx)

        expected = "Name: testpkg\nVersion: 1.0\nSource0: test-1.0.tar.gz\n"
        self.assertEqual(spec_path.read_text(), expected)
        downloaded = spec_path.parent / "test-1.0.tar.gz"
        self.assertTrue(downloaded.exists())
        self.assertEqual(downloaded.read_bytes(), archive_data)

    # --- Multiple URL references ---

    def test_multiple_urls(self):
        content = (
            "Name: testpkg\nVersion: 1.0\n"
            "Source0: https://example.com/test-1.0.tar.gz\n"
            "Patch0: https://example.com/fix1.patch\n"
            "Patch1: https://example.com/fix2.patch\n"
        )
        ctx, spec_path = make_ctx(self.tmpdir, content)

        responses = {
            "test-1.0.tar.gz": b"tarball-content",
            "fix1.patch": b"patch1-content",
            "fix2.patch": b"patch2-content",
        }

        def side_effect(req, **_kwargs):
            mock_resp = MagicMock()
            mock_resp.__enter__.return_value = mock_resp
            for fname, data in responses.items():
                if fname in req.full_url:
                    mock_resp.read.return_value = data
                    return mock_resp
            mock_resp.read.return_value = b""
            return mock_resp

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = side_effect

            from pbuild_ai.modify_mode import _resolve_url_references
            _resolve_url_references(ctx)

        for fname in responses:
            p = spec_path.parent / fname
            self.assertTrue(p.exists(), f"{fname} must exist")
            self.assertEqual(p.read_bytes(), responses[fname],
                             f"{fname} content must match")

    # --- Unsafe URL (localhost) skipped ---

    def test_unsafe_url_skipped(self):
        content = "Name: testpkg\nVersion: 1.0\nPatch0: http://localhost/fix.patch\n"
        ctx, spec_path = make_ctx(self.tmpdir, content)

        from pbuild_ai.modify_mode import _resolve_url_references
        with patch("urllib.request.urlopen") as mock_urlopen:
            _resolve_url_references(ctx)
            mock_urlopen.assert_not_called()

        self.assertEqual(spec_path.read_text(), content,
                         "Unsafe URL must be left unchanged")

    # --- Inline comment preserved ---

    def test_inline_comment_preserved(self):
        content = "Name: testpkg\nVersion: 1.0\nPatch0: https://example.com/fix.patch # workaround for bsc#1234\n"
        ctx, spec_path = make_ctx(self.tmpdir, content)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b"patch-content"
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            from pbuild_ai.modify_mode import _resolve_url_references
            _resolve_url_references(ctx)

        expected = "Name: testpkg\nVersion: 1.0\nPatch0: fix.patch # workaround for bsc#1234\n"
        self.assertEqual(spec_path.read_text(), expected,
                         "Inline comment after URL must be preserved")

    # --- File already exists skips download ---

    def test_existing_file_skips_download(self):
        content = "Name: testpkg\nVersion: 1.0\nPatch0: https://example.com/fix.patch\n"
        ctx, spec_path = make_ctx(self.tmpdir, content)

        # Pre-create the file with different content
        existing = spec_path.parent / "fix.patch"
        existing.write_text("already-here")

        with patch("urllib.request.urlopen") as mock_urlopen:
            from pbuild_ai.modify_mode import _resolve_url_references
            _resolve_url_references(ctx)
            mock_urlopen.assert_not_called()

        # Spec must still be rewritten to local reference
        expected = "Name: testpkg\nVersion: 1.0\nPatch0: fix.patch\n"
        self.assertEqual(spec_path.read_text(), expected,
                         "Existing file must still trigger spec rewrite")
        self.assertEqual(existing.read_text(), "already-here",
                         "Existing file must not be overwritten")

    # --- Download failure leaves spec unchanged ---

    def test_download_failure_leaves_spec(self):
        content = "Name: testpkg\nVersion: 1.0\nPatch0: https://example.com/fix.patch\n"
        ctx, spec_path = make_ctx(self.tmpdir, content)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = Exception("Connection refused")

            from pbuild_ai.modify_mode import _resolve_url_references
            _resolve_url_references(ctx)

        self.assertEqual(spec_path.read_text(), content,
                         "Spec must remain unchanged on download failure")


if __name__ == "__main__":
    unittest.main()
