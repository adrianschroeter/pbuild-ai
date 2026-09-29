"""Tests for the _service version sync, Ollama's repetition abort, empty
analysis answers and cosmetic-only spec rewrites."""

import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules.setdefault('yaml', _yaml)

from pbuild_ai.llm_client import LlmAnalyzer
from pbuild_ai.pbuild_ai import _is_cosmetic_spec_change
from pbuild_ai.service_file import bump_service_version, local_source_files, sync_service_version
from pbuild_ai.workspace import RpmSourceManager

SERVICE = """<services>
  <service name="tar_scm" mode="manual">
    <param name="url">https://github.com/home-assistant-libs/aioshelly.git</param>
    <param name="scm">git</param>
    <param name="revision">v13.4.0</param>
    <param name="versionformat">@PARENT_TAG@</param>
    <param name="versionrewrite-pattern">v(.*)</param>
  </service>
  <service name="recompress" mode="manual">
    <param name="file">*.tar</param>
    <param name="compression">xz</param>
  </service>
</services>
"""

SPEC = """Name:           python-aioshelly
Version:        13.5.0
Release:        0
Source:         aioshelly-%version.tar.xz
"""

REPEAT = RuntimeError('HTTP Error 500: Internal Server Error — '
                      '{"error":"prediction aborted, token repeat limit reached"}')


class TestBumpServiceVersion(unittest.TestCase):
    def test_revision_tag_with_prefix(self):
        text, changed = bump_service_version(SERVICE, "13.4.0", "13.5.0")
        self.assertIn('<param name="revision">v13.5.0</param>', text)
        self.assertEqual(changed, [("revision", "v13.4.0", "v13.5.0")])
        # patterns are untouched
        self.assertIn("@PARENT_TAG@", text)

    def test_version_param_and_no_partial_matches(self):
        svc = ('<param name="version">1.2</param>\n'
               '<param name="revision">1.2.3</param>\n'
               '<param name="url">https://x/1.2/</param>\n')
        text, changed = bump_service_version(svc, "1.2", "1.3")
        self.assertIn('<param name="version">1.3</param>', text)
        self.assertIn('<param name="revision">1.2.3</param>', text)
        self.assertIn("https://x/1.2/", text)
        self.assertEqual(len(changed), 1)

    def test_nothing_to_do(self):
        self.assertEqual(bump_service_version(SERVICE, "9.9", "10.0"), (SERVICE, []))
        self.assertEqual(bump_service_version(SERVICE, "1", "1"), (SERVICE, []))


class TestLocalSourceFiles(unittest.TestCase):
    def test_macros_expanded_urls_skipped(self):
        spec = ("Name: python-foo\nVersion: 2.0\n%define pypi_name foo\n"
                "Source0: %{pypi_name}-%{version}.tar.xz\n"
                "Source1: https://example.org/%name-%version.tar.gz\n"
                "Source2: %name.changes\n")
        self.assertEqual(local_source_files(spec), ["foo-2.0.tar.xz", "python-foo.changes"])


class TestSyncServiceVersion(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pbuild_svc_"))
        self.spec = self.dir / "python-aioshelly.spec"
        self.spec.write_text(SPEC)
        (self.dir / "_service").write_text(SERVICE)
        self.logs = []
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, cmd, cwd=None, **kw):
        self.calls.append((cmd, cwd))
        (Path(cwd) / "aioshelly-13.5.0.tar.xz").write_text("x")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def test_bumps_and_runs_services(self):
        ok = sync_service_version(self.spec, "13.4.0", "13.5.0", run=self._run,
                                  which=lambda n: "/usr/bin/osc", log=self.logs.append)
        self.assertTrue(ok)
        self.assertIn("v13.5.0", (self.dir / "_service").read_text())
        self.assertEqual(self.calls, [(["osc", "service", "manualrun"], str(self.dir))])

    def test_no_osc(self):
        ok = sync_service_version(self.spec, "13.4.0", "13.5.0", run=self._run,
                                  which=lambda n: None, log=self.logs.append)
        self.assertFalse(ok)
        self.assertEqual(self.calls, [])
        self.assertIn("osc service manualrun", self.logs[-1])
        self.assertIn("v13.5.0", (self.dir / "_service").read_text())

    def test_sources_present_no_run(self):
        (self.dir / "aioshelly-13.5.0.tar.xz").write_text("x")
        self.assertTrue(sync_service_version(self.spec, "13.4.0", "13.5.0", run=self._run,
                                             which=lambda n: "/usr/bin/osc", log=self.logs.append))
        self.assertEqual(self.calls, [])

    def test_failed_run_reported(self):
        def failing(cmd, cwd=None, **kw):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="tag not found")
        self.assertFalse(sync_service_version(self.spec, "13.4.0", "13.5.0", run=failing,
                                              which=lambda n: "/usr/bin/osc", log=self.logs.append))
        self.assertIn("tag not found", self.logs[-1])

    def test_no_service_file(self):
        (self.dir / "_service").unlink()
        self.assertIsNone(sync_service_version(self.spec, "13.4.0", "13.5.0", log=self.logs.append))


class TestRepeatLimitRetry(unittest.TestCase):
    def setUp(self):
        self.ai = LlmAnalyzer(model="m")
        self.ai._openai_mode = False
        self.sent = []

    def _patch(self, responses):
        def once(url, payload):
            self.sent.append(payload)
            r = responses[len(self.sent) - 1]
            if isinstance(r, Exception):
                raise r
            return r
        return patch.object(self.ai, "_request_once", side_effect=once)

    def test_retries_with_less_repetition(self):
        with self._patch([REPEAT, REPEAT, {"response": "ok"}]):
            result = self.ai._request("u", {"model": "m", "prompt": "p",
                                            "options": {"temperature": 0.6}})
        self.assertEqual(result, {"response": "ok"})
        self.assertEqual(self.sent[1]["options"], {"temperature": 0.8, "repeat_penalty": 1.2})
        self.assertNotIn("think", self.sent[1])
        self.assertIs(self.sent[2]["think"], False)

    def test_gives_up_after_retries(self):
        with self._patch([REPEAT] * 3):
            with self.assertRaises(RuntimeError):
                self.ai._request("u", {"model": "m", "prompt": "p"})
        self.assertEqual(len(self.sent), 3)

    def test_other_errors_not_retried(self):
        with self._patch([RuntimeError("HTTP Error 404: Not Found")]):
            with self.assertRaises(RuntimeError):
                self.ai._request("u", {"model": "m", "prompt": "p"})
        self.assertEqual(len(self.sent), 1)

    def test_analyze_survives_repeat_limit(self):
        with patch.object(self.ai, "_request", side_effect=REPEAT):
            self.assertEqual(self.ai.analyze("sys", "ctx"), "(model returned empty response)")

    def test_tool_loop_survives_repeat_limit(self):
        tmp = tempfile.mkdtemp(prefix="pbuild_rep_")
        try:
            self.ai._chat_supported = True
            with patch.object(self.ai, "_request", side_effect=REPEAT):
                results = self.ai.call_with_tools(
                    [{"role": "user", "content": "x"}], [], RpmSourceManager(tmp),
                    workspace_dir=tmp, max_rounds=3)
            self.assertEqual(results, [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestEmptyAnalysisRetry(unittest.TestCase):
    def test_retries_once_without_thinking(self):
        ai = LlmAnalyzer(model="m")
        ai._openai_mode = False
        sent = []

        def req(url, payload):
            sent.append(payload)
            return {"response": ""} if len(sent) == 1 else {"response": "The tarball is missing."}

        with patch.object(ai, "_request", side_effect=req):
            self.assertEqual(ai.analyze("sys", "ctx"), "The tarball is missing.")
        self.assertNotIn("think", sent[0])
        self.assertIs(sent[1]["think"], False)


class TestCosmeticSpecChange(unittest.TestCase):
    OLD = ("Source:         aioshelly-%version.tar.xz\n"
           "%prep\nsed -i 's,x,\"%version\",' pyproject.toml\n\n%files\n%doc README.md\n\n%changelog\n")

    def test_braces_whitespace_changelog_only(self):
        new = ("Source: aioshelly-%{version}.tar.xz\n"
               "%prep\nsed -i 's,x,\"%{version}\",' pyproject.toml\n%files\n%doc README.md\n")
        self.assertTrue(_is_cosmetic_spec_change(self.OLD, new))

    def test_real_change(self):
        new = self.OLD.replace("%prep\n", "%prep\n%autosetup -p1\n")
        self.assertFalse(_is_cosmetic_spec_change(self.OLD, new))

    def test_identical_is_not_cosmetic(self):
        self.assertFalse(_is_cosmetic_spec_change(self.OLD, self.OLD))


if __name__ == "__main__":
    unittest.main()
