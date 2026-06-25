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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class PbuildContext:
    """Shared state for all pbuild-ai mode handlers."""
    # CLI args / config
    workspace_dir: str
    root_dir: Optional[str] = None
    package_filter: Optional[str] = None
    fix_mode: bool = False
    show_buildlog: bool = False
    project_mode: bool = False
    do_clean: bool = False
    vm_type: Optional[str] = None
    vm_memory: Optional[str] = None
    preset: Optional[str] = None
    allow_tool_scripts: bool = False
    deep_analyze: bool = False
    fix_attempts: int = 10
    all_mode: bool = False
    prompt_hint: Optional[str] = None
    update_version: Optional[str] = None
    generate_prompt: Optional[str] = None
    modify_prompt: Optional[str] = None
    debug: bool = False
    shell_after_build: bool = False
    interactive: bool = False
    ollama_server: Optional[str] = None
    ollama_model_arg: Optional[str] = None

    # Derived at startup
    full_context: str = ""
    max_all_attempts: int = 50
    skills_dir: Path = field(default_factory=lambda: Path(__file__).parent / "skills")

    # Runtime objects (set after construction)
    manager: Optional[object] = None
    ollama: Optional[object] = None
    skill_manager: Optional[object] = None
    tools: list = field(default_factory=list)
    spec_files: list = field(default_factory=list)
    packages: list = field(default_factory=list)

    # Prompt templates
    default_spec_prompt: str = "Check this RPM Spec-file for errors or missing best practices. Keep it brief:"
    default_error_prompt: str = "The RPM build failed. Analyze the log and explain the error precisely. Provide a solution. If you are unsure about the root cause and need to investigate interactively inside the build environment, include [DEEP_ANALYZE] in your response."
