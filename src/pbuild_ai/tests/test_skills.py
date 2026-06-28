"""Unit tests for SkillManager: matching and merging of multiple skills."""

import os
import sys
import unittest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from pbuild_ai.skill_manager import SkillManager

SKILLS_DIR = os.path.join(os.path.dirname(__file__), "..", "skills")


class TestSkillManager(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sm = SkillManager(SKILLS_DIR)

    # -- get_skill_for (first-match, backward compat) --

    def test_first_match_by_filename(self):
        skill = self.sm.get_skill_for("python-foo.spec", content="")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.__name__, "python_skill")

    def test_first_match_by_content(self):
        skills = self.sm.get_skills_for(
            "foo.spec",
            content="BuildRequires: pkgconfig(Qt5Core)\nBuildRequires: ffmpeg-devel",
        )
        names = {s.__name__ for s in skills}
        self.assertIn("qt5_skill", names)
        self.assertIn("ffmpeg_skill", names)

    def test_first_match_by_prompt(self):
        skill = self.sm.get_skill_for("foo.spec", prompt="clean up this spec")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.__name__, "cleanup_skill")

    def test_no_match_returns_none(self):
        skill = self.sm.get_skill_for("foo.spec", content="irrelevant content\nno triggers")
        self.assertIsNone(skill)

    # -- get_skills_for (all matches) --

    def test_multiple_skills_match_content(self):
        content = (
            "BuildRequires: pkgconfig(Qt5Core)\n"
            "BuildRequires: ffmpeg-devel\n"
        )
        skills = self.sm.get_skills_for("test.spec", content=content)
        names = {s.__name__ for s in skills}
        self.assertIn("qt5_skill", names)
        self.assertIn("ffmpeg_skill", names)
        self.assertGreaterEqual(len(skills), 2)

    def test_multiple_skills_match_with_prompt(self):
        skills = self.sm.get_skills_for(
            "test.spec",
            content="BuildRequires: pkgconfig(Qt5Core)",
            prompt="clean up this spec",
        )
        names = {s.__name__ for s in skills}
        self.assertIn("qt5_skill", names)
        self.assertIn("cleanup_skill", names)

    def test_single_skill_match(self):
        skills = self.sm.get_skills_for("python-foo.spec", content="irrelevant")
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].__name__, "python_skill")

    def test_no_skills_match(self):
        skills = self.sm.get_skills_for(
            "test.spec", content="Name: test\nVersion: 1.0"
        )
        self.assertEqual(len(skills), 0)

    # -- Prompt merging helpers (mirrors code in pbuild_ai.py) --

    def test_spec_prompt_merging(self):
        content = (
            "BuildRequires: pkgconfig(Qt5Widgets)\n"
            "BuildRequires: ffmpeg-devel\n"
        )
        skills = self.sm.get_skills_for("test.spec", content=content)
        self.assertGreaterEqual(len(skills), 2)

        prompt_parts = []
        for s in skills:
            sp = getattr(s, "OLLAMA_SPEC_PROMPT", "")
            if sp:
                prompt_parts.append(f"--- Skill: {s.__name__} ---\n{sp}")

        merged = "\n\n".join(prompt_parts)
        self.assertIn("Qt5 BuildRequires rules", merged)
        self.assertIn("FFmpeg BuildRequires rules", merged)
        self.assertIn("--- Skill: qt5_skill ---", merged)
        self.assertIn("--- Skill: ffmpeg_skill ---", merged)

    def test_error_prompt_merging(self):
        content = (
            "BuildRequires: pkgconfig(Qt5Core)\n"
            "BuildRequires: ffmpeg-devel\n"
        )
        skills = self.sm.get_skills_for("test.spec", content=content)
        self.assertGreaterEqual(len(skills), 2)

        error_parts = []
        for s in skills:
            ep = getattr(s, "OLLAMA_ERROR_PROMPT", "")
            if ep:
                error_parts.append(ep)

        merged = "\n\n".join(error_parts)
        self.assertIn("Qt5", merged)
        self.assertIn("FFmpeg", merged)

    def test_fix_content_chaining(self):
        content = (
            "BuildRequires: pkgconfig(Qt5Core)\n"
            "BuildRequires: ffmpeg-devel\n"
            'BuildRequires: %{python_module pytest}\n'
        )
        skills = self.sm.get_skills_for("test-python.spec", content=content)
        fix_funcs = [getattr(s, "fix_content", None) for s in skills if hasattr(s, "fix_content") and callable(s.fix_content)]

        if fix_funcs:
            def chained(content):
                for f in fix_funcs:
                    content = f(content)
                return content

            result = chained("some %{__python3} setup.py install text")
            self.assertIn("py3_install", result)

    # -- Named skills still work --

    def test_named_skills(self):
        vr = self.sm.get_skill_by_name("version_research")
        self.assertIsNotNone(vr)
        gm = self.sm.get_skill_by_name("generate_mode")
        self.assertIsNotNone(gm)

    def test_named_skill_not_found(self):
        self.assertIsNone(self.sm.get_skill_by_name("nonexistent_skill"))

    # -- Individual skill pattern coverage --

    def test_qt5_skill_patterns(self):
        for line in [
            "BuildRequires: pkgconfig(Qt5Core)",
            "BuildRequires: python3-qt5",
            "BuildRequires: qt5-base-devel",
        ]:
            skills = self.sm.get_skills_for("test.spec", content=line)
            self.assertIn("qt5_skill", {s.__name__ for s in skills},
                          f"qt5_skill should match: {line}")

    def test_ffmpeg_skill_patterns(self):
        for line in [
            "BuildRequires: ffmpeg-devel",
            "BuildRequires: ffmpeg-7-devel",
            "BuildRequires: ffmpeg-8-devel",
            "BuildRequires: ffmpeg-7-avcodec-devel",
            "BuildRequires: ffmpeg-8-avformat-devel",
            "BuildRequires: ffmpeg-6-swresample-devel",
        ]:
            skills = self.sm.get_skills_for("test.spec", content=line)
            self.assertIn("ffmpeg_skill", {s.__name__ for s in skills},
                          f"ffmpeg_skill should match: {line}")

    def test_cleanup_skill_patterns(self):
        for prompt in [
            "clean up this spec",
            "cleanup the spec",
            "standardize this spec",
            "standardise the spec",
            "run spec-cleaner",
            "normalize spec file",
            "normalise spec file",
            "format spec",
            "lint spec",
            "tidy up spec",
            "sanitize spec",
        ]:
            skills = self.sm.get_skills_for("test.spec", prompt=prompt)
            self.assertIn("cleanup_skill", {s.__name__ for s in skills},
                          f"cleanup_skill should match prompt: {prompt}")

    def test_python_skill_patterns(self):
        for name in [
            "python-foo.spec",
            "python-bar.spec",
        ]:
            skills = self.sm.get_skills_for(name, content="irrelevant")
            self.assertIn("python_skill", {s.__name__ for s in skills},
                          f"python_skill should match filename: {name}")

    def test_remoteasset_skill_patterns(self):
        for prompt in [
            "convert obs_scm to RemoteAsset",
            "replace _service with RemoteAsset",
            "migrate obs_scm to remote asset",
        ]:
            skills = self.sm.get_skills_for("test.spec", prompt=prompt)
            self.assertIn("remoteasset_skill", {s.__name__ for s in skills},
                          f"remoteasset_skill should match prompt: {prompt}")


if __name__ == "__main__":
    unittest.main()
