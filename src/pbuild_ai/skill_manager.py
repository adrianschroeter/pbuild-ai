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
        self.base_skill_content = None
        self.deep_analyze_prompts = []
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
                if hasattr(module, 'TARGET_PATTERN'):
                    self.skills.append(module)
                    print(f"[SKILL LOADED] {skill_file.name} (auto loaded)")
                if hasattr(module, 'DEEP_ANALYZE_PROMPT'):
                    self.deep_analyze_prompts.append(module.DEEP_ANALYZE_PROMPT.strip())
            except Exception as e:
                print(f"[WARNING] Could not load skill {skill_file.name}: {e}")

    def get_skill_for(self, filename, content=None):
        for skill in self.skills:
            if re.search(skill.TARGET_PATTERN, filename):
                return skill
            if content is not None and hasattr(skill, 'CONTENT_PATTERN') and re.search(skill.CONTENT_PATTERN, content):
                return skill
        return None

    def get_deep_analyze_prompt(self):
        return "\n\n".join(self.deep_analyze_prompts) if self.deep_analyze_prompts else ""
