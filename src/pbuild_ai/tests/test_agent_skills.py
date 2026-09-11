"""Tests for loading workspace agent skills from .agents/skills/*.md."""

import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

# Mock yaml before any pbuild_ai import (headless test host has no yaml)
_yaml = types.ModuleType('yaml')
_yaml.YAMLError = Exception
sys.modules['yaml'] = _yaml

from pbuild_ai.workspace import RpmSourceManager
from pbuild_ai.pbuild_ai import _agent_skill_blocks, _mandated_script_failed, _unique_script_refs


class TestReadAgentSkills(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="pbuild_agent_skills_")
        self.base = Path(self.tmpdir)
        self.skills_dir = self.base / ".agents" / "skills"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _manager(self):
        return RpmSourceManager(self.base)

    def test_missing_dir_returns_empty(self):
        self.assertEqual(self._manager().read_agent_skills(), [])

    def test_empty_skills_dir_returns_empty(self):
        self.skills_dir.mkdir(parents=True)
        self.assertEqual(self._manager().read_agent_skills(), [])

    def test_loads_markdown_skills_sorted(self):
        self.skills_dir.mkdir(parents=True)
        (self.skills_dir / "B.md").write_text("bbb")
        (self.skills_dir / "A.md").write_text("aaa")
        (self.skills_dir / "run.sh").write_text("#!/bin/sh\n")
        skills = self._manager().read_agent_skills()
        self.assertEqual(skills, [("A", "aaa"), ("B", "bbb")])


class TestAgentSkillBlocks(unittest.TestCase):
    def test_empty_input_renders_empty(self):
        self.assertEqual(_agent_skill_blocks([]), "")

    def test_renders_named_blocks(self):
        out = _agent_skill_blocks([("OLLAMA", "rule one"), ("GEN", "rule two")])
        self.assertIn("--- Skill: OLLAMA ---\nrule one", out)
        self.assertIn("--- Skill: GEN ---\nrule two", out)


class TestUniqueScriptRefs(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(_unique_script_refs([]), [])

    def test_deduplicates_bare_and_path_forms(self):
        out = _unique_script_refs(
            ["update_references.sh", ".agents/skills/update_references.sh"])
        self.assertEqual(out, [".agents/skills/update_references.sh"])

    def test_distinct_names_kept_in_order(self):
        out = _unique_script_refs([".agents/skills/a.sh", "b.sh"])
        self.assertEqual(out, [".agents/skills/a.sh", "b.sh"])

    def test_prefers_first_occurrence_when_both_are_paths(self):
        out = _unique_script_refs(["tool-scripts/x.sh", ".agents/x.sh"])
        self.assertEqual(out, ["tool-scripts/x.sh"])


class TestMandatedScriptFailed(unittest.TestCase):
    POST = [".agents/skills/update_references.sh"]

    def test_no_scripts_no_failure(self):
        self.assertIsNone(_mandated_script_failed(["run_tool_script: OK"], []))

    def test_no_run_tool_script_results_ok(self):
        self.assertIsNone(_mandated_script_failed(["read_file: content"], self.POST))

    def test_success_not_flagged(self):
        results = ["run_tool_script: OK",
                   "run_tool_script: updated references\nall good"]
        self.assertIsNone(_mandated_script_failed(results, self.POST))

    def test_failed_script_detected_from_results(self):
        results = ["run_tool_script: Error: Script failed (exit 127):\n"
                   ".../update_references.sh: line 8: osc: command not found"]
        self.assertEqual(_mandated_script_failed(results, self.POST),
                         "update_references.sh")

    def test_malformed_call_then_success_not_flagged_from_messages(self):
        # The model first calls run_tool_script without a script_name (error),
        # then a correctly-formed call that successfully runs the mandated
        # script.  That is NOT a failure and must not abort.
        msgs = [
            {"role": "system", "content": "run updates"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "run_tool_script",
                                          "arguments": {"script": ".agents/skills/update_references.sh"}}}]},
            {"role": "tool", "name": "run_tool_script",
             "content": "Error: run_tool_script requires a script_name."},
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "run_tool_script",
                                          "arguments": {"script_name": ".agents/skills/update_references.sh"}}}]},
            {"role": "tool", "name": "run_tool_script",
             "content": "Running source_service 'download_files' ...\nall good"},
        ]
        self.assertIsNone(_mandated_script_failed([], self.POST, messages=msgs))

    def test_malformed_call_then_failure_detected_from_messages(self):
        msgs = [
            {"role": "system", "content": "run updates"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "run_tool_script",
                                          "arguments": {"script_name": ".agents/skills/update_references.sh"}}}]},
            {"role": "tool", "name": "run_tool_script",
             "content": "Error: Script failed (exit 127):\n...: osc: command not found"},
        ]
        self.assertEqual(_mandated_script_failed([], self.POST, messages=msgs),
                         "update_references.sh")

    def test_string_arguments_handled(self):
        msgs = [
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "run_tool_script",
                                          "arguments": '{"script_name": ".agents/skills/update_references.sh"}'}}]},
            {"role": "tool", "name": "run_tool_script",
             "content": "Error executing script: boom"},
        ]
        self.assertEqual(_mandated_script_failed([], self.POST, messages=msgs),
                         "update_references.sh")

    def test_unrelated_errors_ignored(self):
        results = ["edit_file: Error: old_string not found",
                   "run_tool_script: Error: some other script failed"]
        self.assertIsNone(_mandated_script_failed(results, self.POST))


if __name__ == "__main__":
    unittest.main()