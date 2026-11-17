"""Tests for BuildRequires package lookups via the gitexplorer.opensuse.org API.

The AI must depend on PACKAGE NAMES, never on file paths like
BuildRequires: /usr/bin/protoc — the search_package tool and the build-log
injection resolve file paths to real package names."""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Mock yaml before any pbuild_ai import to prevent ModuleNotFoundError
if 'yaml' not in sys.modules:
    _yaml = types.ModuleType('yaml')
    _yaml.YAMLError = Exception
    sys.modules['yaml'] = _yaml

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.skills.unresolvable_skill import (
    parse_unresolved_package_from_log,
    parse_unresolved_package_with_constraint,
    parse_filepath_buildrequires,
    spec_write_hint,
)
from pbuild_ai.tools import build_tools_list, execute_tool_calls
import pbuild_ai.query_gitexplorer as qg

SOLVER_LOG = ("Loading repository data...Solving packages...Problem: "
              "nothing provides /usr/bin/protoc needed by mypkg-1.0-1.x86_64")

PROTOBUF_RESULTS = [
    {"package_name": "protobuf21-devel", "tracking_branch": "slfo-1.2"},
    {"package_name": "protobuf-devel", "tracking_branch": "factory"},
]


class TestUnresolvedPathParsing(unittest.TestCase):
    def test_file_path_dependency_parsed(self):
        self.assertEqual(parse_unresolved_package_from_log(SOLVER_LOG),
                         "/usr/bin/protoc")

    def test_file_path_dependency_with_constraint(self):
        parsed = parse_unresolved_package_with_constraint(SOLVER_LOG)
        self.assertEqual(parsed, ("/usr/bin/protoc", None, None))

    def test_plain_package_names_still_parsed(self):
        log = "Problem: nothing provides libfoo-devel >= 1.2 needed by bar-1.x86_64"
        self.assertEqual(parse_unresolved_package_from_log(log), "libfoo-devel")
        self.assertEqual(parse_unresolved_package_with_constraint(log),
                         ("libfoo-devel", ">=", "1.2"))

    def test_no_match(self):
        self.assertIsNone(parse_unresolved_package_from_log("Build succeeded"))


class TestInjectGitexplorerResults(unittest.TestCase):
    def test_file_path_dep_uses_files_endpoint(self):
        from pbuild_ai.pbuild_ai import _inject_gitexplorer_results
        with patch('pbuild_ai.query_gitexplorer.query_package_by_file',
                   return_value=PROTOBUF_RESULTS) as m_file, \
             patch('pbuild_ai.query_gitexplorer.query_package_by_name') as m_name:
            out = _inject_gitexplorer_results("BASE", SOLVER_LOG)
        m_file.assert_called_once_with("/usr/bin/protoc")
        m_name.assert_not_called()
        self.assertIn("Packages providing '/usr/bin/protoc'", out)
        self.assertIn("protobuf21-devel", out)
        self.assertIn("never the file path", out)

    def test_package_name_dep_uses_packages_endpoint(self):
        from pbuild_ai.pbuild_ai import _inject_gitexplorer_results
        log = "Problem: nothing provides Mesa-devel needed by foo-1.x86_64"
        with patch('pbuild_ai.query_gitexplorer.query_package_by_file') as m_file, \
             patch('pbuild_ai.query_gitexplorer.query_package_by_name',
                   return_value=[{"package_name": "Mesa-devel", "tracking_branch": "factory"}]) as m_name:
            out = _inject_gitexplorer_results("BASE", log)
        m_file.assert_not_called()
        m_name.assert_called_once_with("Mesa-devel")
        self.assertIn("Packages matching 'Mesa-devel'", out)


class TestSearchPackageTool(unittest.TestCase):
    def _run(self, tool_input, mock_file=None, mock_name=None):
        calls = [("search_package", tool_input)]
        manager = MagicMock()
        manager._is_safe_path.return_value = True
        with patch('pbuild_ai.query_gitexplorer.query_package_by_file',
                   return_value=mock_file or []) as mf, \
             patch('pbuild_ai.query_gitexplorer.query_package_by_name',
                   return_value=mock_name or []) as mn:
            results = execute_tool_calls(calls, manager, tempfile.mkdtemp())
        return results[0], mf, mn

    def test_tool_is_advertised(self):
        names = [t["function"]["name"] for t in build_tools_list()]
        self.assertIn("search_package", names)
        schema = next(t for t in build_tools_list()
                      if t["function"]["name"] == "search_package")["function"]
        self.assertIn("/usr/bin/protoc", schema["description"])
        self.assertEqual(schema["parameters"]["required"], ["query"])

    def test_file_lookup(self):
        out, mf, mn = self._run({"query": "/usr/bin/protoc", "search_by": "file"},
                                mock_file=PROTOBUF_RESULTS)
        mf.assert_called_once_with("/usr/bin/protoc")
        self.assertIn("protobuf21-devel", out)
        self.assertIn("PACKAGE NAMES", out)

    def test_name_lookup_default(self):
        out, mf, mn = self._run({"query": "protobuf"},
                                mock_name=PROTOBUF_RESULTS)
        mn.assert_called_once_with("protobuf")
        self.assertIn("protobuf-devel", out)

    def test_no_results_warns_against_inventing_paths(self):
        out, _, _ = self._run({"query": "/usr/bin/nonexistent", "search_by": "file"})
        self.assertIn("No packages found", out)
        self.assertIn("file-path", out)

    def test_missing_query(self):
        out, _, _ = self._run({})
        self.assertTrue(out.startswith("Error"))

    def test_bad_search_by(self):
        out, _, _ = self._run({"query": "x", "search_by": "url"})
        self.assertTrue(out.startswith("Error"))

    def test_network_error_is_reported_not_raised(self):
        calls = [("search_package", {"query": "foo", "search_by": "name"})]
        manager = MagicMock()
        with patch('pbuild_ai.query_gitexplorer.query_package_by_name',
                   side_effect=OSError("boom")):
            results = execute_tool_calls(calls, manager, tempfile.mkdtemp())
        self.assertIn("gitexplorer query failed", results[0])


class TestFilepathRequireHint(unittest.TestCase):
    def _write_spec(self, content, providers=None, raise_exc=False):
        ws = tempfile.mkdtemp()
        manager = MagicMock()
        manager._is_safe_path.return_value = True
        side = OSError("down") if raise_exc else None
        with patch('pbuild_ai.query_gitexplorer.query_package_by_file',
                   return_value=providers or [], side_effect=side) as mf:
            out = execute_tool_calls(
                [("write_file", {"path": "foo.spec", "content": content})],
                manager, ws)[0]
        return out, mf

    def test_filepath_buildrequires_triggers_lookup_and_hint(self):
        out, mf = self._write_spec(
            "Name: foo\nBuildRequires: /usr/bin/protoc\n",
            providers=PROTOBUF_RESULTS)
        mf.assert_called_once_with("/usr/bin/protoc", limit=10)
        self.assertIn("File-path BuildRequires", out)
        self.assertIn("protobuf21-devel", out)

    def test_no_providers_still_hints(self):
        out, _ = self._write_spec("Name: foo\nBuildRequires: /usr/bin/nope\n")
        self.assertIn("File-path BuildRequires", out)
        self.assertIn("No package provides", out)

    def test_lookup_failure_keeps_base_hint(self):
        out, _ = self._write_spec("Name: foo\nBuildRequires: /usr/bin/protoc\n",
                                  raise_exc=True)
        self.assertIn("search_package", out)
        self.assertTrue(out.startswith("OK: Wrote"))

    def test_clean_spec_has_no_hint(self):
        out, mf = self._write_spec("Name: foo\nBuildRequires: protobuf21-devel\n")
        mf.assert_not_called()
        self.assertNotIn("File-path BuildRequires", out)

    def test_macro_values_are_not_flagged(self):
        out, mf = self._write_spec(
            "Name: foo\nBuildRequires: %{python_module setuptools}\n"
            "Requires: /usr/bin/env\n")
        mf.assert_not_called()
        self.assertNotIn("File-path BuildRequires", out)

    def test_edit_file_result_carries_hint(self):
        ws = tempfile.mkdtemp()
        spec = Path(ws) / "foo.spec"
        spec.write_text("Name: foo\nBuildRequires: protobuf21-devel\n")
        manager = MagicMock()
        manager._is_safe_path.return_value = True
        manager.read_file_safe.return_value = spec.read_text()
        with patch('pbuild_ai.query_gitexplorer.query_package_by_file',
                   return_value=PROTOBUF_RESULTS):
            out = execute_tool_calls(
                [("edit_file", {"path": "foo.spec",
                                "old_string": "BuildRequires: protobuf21-devel",
                                "new_string": "BuildRequires: /usr/bin/protoc"})],
                manager, ws)[0]
        self.assertIn("File-path BuildRequires", out)

    def test_hint_only_for_spec_files(self):
        hint = spec_write_hint("/tmp/notes.txt", "BuildRequires: /usr/bin/protoc\n")
        self.assertEqual(hint, "")


class TestAutoDepsSkipsFilePaths(unittest.TestCase):
    def test_file_path_dep_is_not_auto_created(self):
        from pbuild_ai.pbuild_ai import _handle_auto_deps
        ctx = MagicMock()
        with patch('pbuild_ai.pbuild_ai._migrate_to_project_mode') as mig, \
             patch('pbuild_ai.pbuild_ai._check_package_exists') as chk:
            result = _handle_auto_deps(SOLVER_LOG, tempfile.mkdtemp(), "mypkg", ctx)
        self.assertIsNone(result)
        mig.assert_not_called()
        chk.assert_not_called()


class TestLookupCache(unittest.TestCase):
    """gitexplorer lookups are cached per process so the fix loop, the write
    hints and search_package do not re-query the same dependency."""

    def setUp(self):
        qg.clear_cache()
        self.addCleanup(qg.clear_cache)

    def test_repeated_file_query_fetches_once(self):
        with patch.object(qg, "_fetch_by_file", return_value=list(PROTOBUF_RESULTS)) as f:
            r1 = qg.query_package_by_file("/usr/bin/protoc")
            r2 = qg.query_package_by_file("/usr/bin/protoc", limit=1)
        self.assertEqual(f.call_count, 1)
        self.assertEqual(r1, PROTOBUF_RESULTS)
        self.assertEqual(r2, PROTOBUF_RESULTS[:1])

    def test_repeated_name_query_fetches_once(self):
        with patch.object(qg, "_fetch_by_name", return_value=list(PROTOBUF_RESULTS)) as f:
            qg.query_package_by_name("protobuf")
            qg.query_package_by_name("protobuf")
        self.assertEqual(f.call_count, 1)

    def test_clear_cache_forces_refetch(self):
        with patch.object(qg, "_fetch_by_file", return_value=list(PROTOBUF_RESULTS)) as f:
            qg.query_package_by_file("/usr/bin/x")
            qg.clear_cache()
            qg.query_package_by_file("/usr/bin/x")
        self.assertEqual(f.call_count, 2)

    def test_failure_is_not_cached(self):
        with patch.object(qg, "_fetch_by_file",
                          side_effect=[OSError("down"), list(PROTOBUF_RESULTS)]) as f:
            with self.assertRaises(OSError):
                qg.query_package_by_file("/usr/bin/y")
            got = qg.query_package_by_file("/usr/bin/y")
        self.assertEqual(f.call_count, 2)
        self.assertEqual(got, PROTOBUF_RESULTS)


class TestFilepathBuildrequiresParsing(unittest.TestCase):
    def test_paths_parsed_comments_skipped(self):
        spec = ("Name: foo\n"
                "BuildRequires:  /usr/bin/protoc\n"
                "# BuildRequires: /usr/bin/legacy\n"
                "BuildRequires: /usr/bin/protoc\n"
                "BuildRequires: %{__python3}\n"
                "BuildRequires: protobuf21-devel\n")
        self.assertEqual(parse_filepath_buildrequires(spec), ["/usr/bin/protoc"])

    def test_content_pattern_matches_file_paths(self):
        from pbuild_ai.skills.unresolvable_skill import CONTENT_PATTERN
        import re
        self.assertIsNotNone(re.search(CONTENT_PATTERN, "BuildRequires: /usr/bin/protoc"))
        self.assertIsNotNone(re.search(CONTENT_PATTERN, "BuildRequires:  Mesa-devel\nBuildRequires: OpenGL"))
        self.assertIsNone(re.search(CONTENT_PATTERN, "BuildRequires: protobuf21-devel\nName: foo"))


class TestDependencyFixBuildError(unittest.TestCase):
    """unresolvable_skill.fix_build_error replaces unambiguous file-path
    BuildRequires with the provider's package name before the AI sees the log."""

    SPEC = "Name: foo\nBuildRequires:  /usr/bin/protoc\nBuildRequires: cmake\n"

    def _fix(self, spec_text, log_text, providers, side_effect=None):
        from pbuild_ai.skills.unresolvable_skill import fix_build_error
        with patch('pbuild_ai.query_gitexplorer.query_package_by_file',
                   return_value=providers, side_effect=side_effect) as mf:
            return fix_build_error(Path("foo.spec"), spec_text, log_text, log=lambda *a: None), mf

    def test_sole_provider_is_applied(self):
        new, mf = self._fix(self.SPEC, SOLVER_LOG, PROTOBUF_RESULTS[:1])
        self.assertIn("BuildRequires:  protobuf21-devel", new)
        self.assertNotIn("/usr/bin/protoc", new.split("\n")[1])
        self.assertIn("BuildRequires: cmake", new)

    def test_all_occurrences_replaced(self):
        spec = self.SPEC + "BuildRequires: /usr/bin/protoc\n"
        new, _ = self._fix(spec, SOLVER_LOG, PROTOBUF_RESULTS[:1])
        self.assertNotIn("/usr/bin/protoc", new)

    def test_ambiguous_providers_left_to_ai(self):
        new, _ = self._fix(self.SPEC, SOLVER_LOG, PROTOBUF_RESULTS)
        self.assertIsNone(new)

    def test_path_not_in_log_is_not_touched(self):
        new, mf = self._fix(self.SPEC, "some other failure", PROTOBUF_RESULTS[:1])
        self.assertIsNone(new)
        mf.assert_not_called()

    def test_lookup_failure_leaves_spec_unchanged(self):
        new, _ = self._fix(self.SPEC, SOLVER_LOG, [], side_effect=OSError("offline"))
        self.assertIsNone(new)

    def test_no_providers_leaves_spec_unchanged(self):
        new, _ = self._fix(self.SPEC, SOLVER_LOG, [])
        self.assertIsNone(new)

    def test_clean_spec_short_circuits(self):
        new, mf = self._fix("Name: foo\nBuildRequires: cmake\n", SOLVER_LOG,
                            PROTOBUF_RESULTS[:1])
        self.assertIsNone(new)
        mf.assert_not_called()


class TestPromptGuidance(unittest.TestCase):
    """The prompts must tell the AI to use package names and search_package."""

    def _read(self, rel):
        p = Path(SRC_DIR) / "pbuild_ai" / rel
        return p.read_text(encoding="utf-8")

    def test_opensuse_md_rule(self):
        text = self._read("skills/OPENSUSE.md")
        self.assertIn("never file paths like `BuildRequires: /usr/bin/protoc`", text)
        self.assertIn("search_package", text)

    def test_generate_skill_rule(self):
        text = self._read("skills/generate_mode_skill.py")
        self.assertIn("never file paths like /usr/bin/protoc", text)
        self.assertIn("search_package", text)
        # the prompt is passed through str.format — the new rule must not add braces
        module_ns = {}
        exec(compile(text, "generate_mode_skill.py", "exec"), module_ns)
        rendered = module_ns["GENERATE_SYSTEM_PROMPT"].format(full_context="CTX")
        self.assertIn("search_package", rendered)

    def test_unresolvable_skill_error_prompt(self):
        text = self._read("skills/unresolvable_skill.py")
        module_ns = {}
        exec(compile(text, "unresolvable_skill.py", "exec"), module_ns)
        ep = module_ns["AI_ERROR_PROMPT"]
        self.assertIn("File-path dependencies", ep)
        self.assertIn("protobuf21-devel", ep)
        self.assertIn("search_package", ep)


if __name__ == "__main__":
    unittest.main()
