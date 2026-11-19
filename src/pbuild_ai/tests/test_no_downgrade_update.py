"""An auto-discovered version older than the packaged one must be a no-op.

Real-world trigger: gufo.spec at 0.35.0 with the lang_rust skill — crates.io
still reports 0.5.4 for the crate name, which the pre-check used to accept as
"latest version" and hand to the AI as an update target.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

# Mock yaml before any pbuild_ai import
_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules.setdefault('yaml', _yaml)

from pbuild_ai.pbuild_ai import _is_downgrade


class TestIsDowngrade(unittest.TestCase):

    def test_older_minor_is_a_downgrade(self):
        # crates.io gufo 0.5.4 vs packaged 0.35.0: '5' > '3' as a string,
        # but numerically the API result is the older release.
        self.assertTrue(_is_downgrade("0.5.4", "0.35.0"))

    def test_newer_and_equal_are_not_downgrades(self):
        self.assertFalse(_is_downgrade("0.35.1", "0.35.0"))
        self.assertFalse(_is_downgrade("1.0", "0.35.0"))
        self.assertFalse(_is_downgrade("0.35.0", "0.35.0"))

    def test_nothing_to_compare_against(self):
        self.assertFalse(_is_downgrade("0.5.4", None))
        self.assertFalse(_is_downgrade("0.5.4", ""))
        self.assertFalse(_is_downgrade("", "0.35.0"))
        self.assertFalse(_is_downgrade(None, "0.35.0"))


SPEC = """\
Name:           gufo
Version:        0.35.0
Release:        1
Summary:        Gufo
License:        MIT
URL:            https://crates.io/crates/gufo
Source:         %{name}-%{version}.tar.gz

%description
Gufo AI CLI built with cargo.

%build
%{cargo_build}
"""


class TestUpdateOnlyIsNoOp(unittest.TestCase):

    def _run_update_only(self):
        tmpdir = tempfile.mkdtemp(prefix="pbuild_nodg_")
        self.addCleanup(shutil.rmtree, tmpdir, True)
        spec = os.path.join(tmpdir, "gufo.spec")
        with open(spec, "w") as f:
            f.write(SPEC)
        script = textwrap.dedent(f"""\
        import json, sys, types, urllib.request
        from unittest.mock import patch
        _yaml = types.ModuleType('yaml'); _yaml.YAMLError = Exception
        sys.modules['yaml'] = _yaml
        sys.path.insert(0, {SRC_DIR!r})
        sys.argv = ["pbuild-ai", "--update-only", "--non-interactive", {tmpdir!r}]

        called = []
        def fake_urlopen(req, timeout=None):
            called.append(getattr(req, 'full_url', str(req)))
            payload = {{"crate": {{"max_stable_version": "0.5.4",
                                   "newest_version": "0.5.4"}}}}
            class _Resp:
                def read(self):
                    return json.dumps(payload).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
            return _Resp()

        from pbuild_ai.llm_client import LlmAnalyzer
        import pbuild_ai.pbuild_ai as pa
        seen = []
        def fake(self, messages, *a, **k):
            seen.append(messages[1]["content"] if len(messages) > 1 else "")
            return []
        with patch.object(LlmAnalyzer, 'call_with_tools', fake), \\
                patch.object(LlmAnalyzer, 'analyze', return_value=""), \\
                patch.object(LlmAnalyzer, 'print_stats'), \\
                patch.object(urllib.request, 'urlopen', fake_urlopen):
            try:
                pa.main()
            except SystemExit:
                pass
        print("AI_CALLS:", len(seen))
        print("API_CALLS:", sorted(set(called)))
        """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                timeout=120, cwd=tmpdir,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return result.stdout + result.stderr, spec

    def test_stale_registry_ends_in_no_op(self):
        out, spec = self._run_update_only()
        # The stale crates.io entry is named and refused...
        self.assertIn("0.5.4", out)
        self.assertIn("older than current 0.35.0", out)
        self.assertIn("Nothing to update", out)
        # ...not accepted as "the latest version"...
        self.assertNotIn("Found latest version 0.5.4", out)
        # ...and the AI is never involved.
        self.assertIn("AI_CALLS: 0", out)
        self.assertIn("API_CALLS: ['https://crates.io/api/v1/crates/gufo']", out)
        # The spec is left untouched.
        with open(spec) as f:
            self.assertIn("Version:        0.35.0", f.read())

    def test_stale_github_releases_end_in_no_op(self):
        # Same protection for the generic GitHub fallback: the spec is ahead
        # of what the forge releases expose (git-based packaging, 0.35.0).
        spec_txt = SPEC.replace(
            "URL:            https://crates.io/crates/gufo\n"
            "Source:         %{name}-%{version}.tar.gz",
            "Source:         https://github.com/gufo-rs/gufo/archive/%{name}-%{version}.tar.gz")
        tmpdir = tempfile.mkdtemp(prefix="pbuild_nodg_gh_")
        self.addCleanup(shutil.rmtree, tmpdir, True)
        spec = os.path.join(tmpdir, "gufo.spec")
        with open(spec, "w") as f:
            f.write(spec_txt)
        script = textwrap.dedent(f"""\
        import json, sys, types, urllib.request
        from unittest.mock import patch
        _yaml = types.ModuleType('yaml'); _yaml.YAMLError = Exception
        sys.modules['yaml'] = _yaml
        sys.path.insert(0, {SRC_DIR!r})
        sys.argv = ["pbuild-ai", "--update-only", "--non-interactive", {tmpdir!r}]

        def fake_urlopen(req, timeout=None):
            payload = [{{"tag_name": "0.5.4", "body": "old", "prerelease": False,
                        "draft": False, "html_url": "https://github.com/gufo-rs/gufo/releases/tag/0.5.4"}}]
            class _Resp:
                def read(self):
                    return json.dumps(payload).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
            return _Resp()

        from pbuild_ai.llm_client import LlmAnalyzer
        import pbuild_ai.pbuild_ai as pa
        seen = []
        def fake(self, messages, *a, **k):
            seen.append(messages[1]["content"] if len(messages) > 1 else "")
            return []
        with patch.object(LlmAnalyzer, 'call_with_tools', fake), \\
                patch.object(LlmAnalyzer, 'analyze', return_value=""), \\
                patch.object(LlmAnalyzer, 'print_stats'), \\
                patch.object(urllib.request, 'urlopen', fake_urlopen):
            try:
                pa.main()
            except SystemExit:
                pass
        print("AI_CALLS:", len(seen))
        """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                timeout=120, cwd=tmpdir,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        out = result.stdout + result.stderr
        self.assertIn("GitHub releases top out at 0.5.4", out)
        self.assertIn("Nothing to update", out)
        self.assertNotIn("Found latest version 0.5.4", out)
        self.assertIn("AI_CALLS: 0", out)
        with open(spec) as f:
            self.assertIn("Version:        0.35.0", f.read())


if __name__ == "__main__":
    unittest.main()
