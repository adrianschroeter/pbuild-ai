# Copyright (C) 2026 SUSE Linux Products GmbH / Adrian Schröter <adrian@suse.de>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import importlib.util
import re
from pathlib import Path


class SkillManager:
    def __init__(self, skills_dir):
        self.skills_dir = Path(skills_dir).resolve()
        self.skills = []
        self._named_skills = {}
        self.base_skill_content = None
        self.deep_analyze_prompts = []
        self.activated_skills = set()
        self._load_skills()
        self._load_base_skill()

    def _load_base_skill(self):
        base_skill_file = self.skills_dir / "OPENSUSE.md"
        if base_skill_file.is_file():
            self.base_skill_content = base_skill_file.read_text(encoding="utf-8")
            print(f"[SKILL LOADED] OPENSUSE.md (Base skill)")

    def _load_skills(self):
        if not self.skills_dir.is_dir():
            print(f"[INFO] Skills directory {self.skills_dir} not found. Creating it.")
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            return

        for skill_file in self.skills_dir.glob("*.py"):
            try:
                spec = importlib.util.spec_from_file_location(skill_file.stem, skill_file)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if hasattr(module, 'SKILL_NAME'):
                    self._named_skills[module.SKILL_NAME] = module
                if hasattr(module, 'TARGET_PATTERN') or hasattr(module, 'CONTENT_PATTERN') or hasattr(module, 'PROMPT_PATTERN'):
                    self.skills.append(module)
                if hasattr(module, 'DEEP_ANALYZE_PROMPT'):
                    self.deep_analyze_prompts.append(module.DEEP_ANALYZE_PROMPT.strip())
            except Exception as e:
                print(f"[WARNING] Could not load skill {skill_file.name}: {e}")

    def _print_skill_active(self, skill, filename, match_type):
        """Print a consistent [SKILL ACTIVE] message."""
        skill_name = getattr(skill, 'SKILL_NAME', None) or (Path(skill.__file__).name if hasattr(skill, '__file__') else "unknown")
        self.activated_skills.add(skill_name)
        if match_type == "prompt":
            print(f"[SKILL ACTIVE] {skill_name} (prompt match)")
        else:
            print(f"[SKILL ACTIVE] {skill_name} for {filename}")

    def _matches_skill(self, skill, filename, content, prompt):
        """Check if a skill matches the given filename, content, or prompt."""
        if hasattr(skill, 'TARGET_PATTERN') and re.search(skill.TARGET_PATTERN, filename):
            return True
        if content is not None and hasattr(skill, 'CONTENT_PATTERN') and re.search(skill.CONTENT_PATTERN, content):
            return True
        if prompt is not None and hasattr(skill, 'PROMPT_PATTERN') and re.search(skill.PROMPT_PATTERN, prompt):
            return True
        return False

    def get_skill_for(self, filename, content=None, prompt=None):
        for skill in self.skills:
            if self._matches_skill(skill, filename, content, prompt):
                self._print_skill_active(skill, filename, "prompt" if prompt and hasattr(skill, 'PROMPT_PATTERN') and re.search(skill.PROMPT_PATTERN, prompt) else "content")
                return skill
        return None

    def get_skills_for(self, filename, content=None, prompt=None):
        """Return ALL skills matching the given filename, content, or prompt."""
        matches = []
        for skill in self.skills:
            if self._matches_skill(skill, filename, content, prompt):
                self._print_skill_active(skill, filename, "prompt" if prompt and hasattr(skill, 'PROMPT_PATTERN') and re.search(skill.PROMPT_PATTERN, prompt) else "content")
                matches.append(skill)
        return matches

    def get_skill_by_name(self, name):
        return self._named_skills.get(name)

    def get_deep_analyze_prompt(self):
        return "\n\n".join(self.deep_analyze_prompts) if self.deep_analyze_prompts else ""
