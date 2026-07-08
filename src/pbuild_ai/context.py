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
    dist: Optional[str] = None
    allow_tool_scripts: bool = False
    deep_analyze: bool = False
    fix_attempts: int = 25
    prompt_hint: Optional[str] = None
    update_version: Optional[str] = None
    update_only: bool = False
    generate_prompt: Optional[str] = None
    modify_prompt: Optional[str] = None
    debug: bool = False
    shell_after_build: bool = False
    interactive: bool = False
    ollama_server: Optional[str] = None  # --openai-server
    ollama_model_arg: Optional[str] = None
    ollama_timeout: int = 900  # --ollama-timeout
    ollama_options: dict = field(default_factory=dict)  # --ollama-option
    email: str = ""
    analyze_mode: bool = False
    max_rounds: int = 15
    build_log: Optional[str] = None
    program_start: float = 0.0

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
    default_error_prompt: str = (
        "The RPM build failed. Analyze the log and explain the error precisely. "
        "Describe the fix needed — do NOT include the full spec file in your response, "
        "only describe what lines need to change. If you are unsure about the root cause "
        "and need to investigate interactively inside the build environment, include "
        "[DEEP_ANALYZE] in your response.\n\n"
        "If you detect an out-of-memory (OOM) error (e.g., 'Killed', 'signal 9', "
        "'Out of memory', 'Cannot allocate memory', 'vm.max_map_count'): suggest "
        "increasing VM memory via 'pbuild --vm-memory 4096' or higher, or setting "
        "parallel jobs to 1 via 'pbuild --jobs 1'.\n\n"
        "If you see 'fg: no job control' together with a literal '%{macro_name' or '%macro_name' "
        "in the build output (the macro was not expanded by RPM): the macro is not "
        "defined. Either replace the macro usage, or add a BuildRequires for the "
        "package that provides it. Common examples: %suse_update_config needs "
        "BuildRequires: autoconf, %pkgconfig(...) needs BuildRequires: pkgconfig(...), "
        "%perl_make_install needs BuildRequires: perl-macros.\n\n"
        "If the build log shows %files section failures:\n"
        "- 'File not found: ...' or 'Unable to find ... under ...': the quickest way "
        "to debug is interactive deep investigation via [DEEP_ANALYZE]. Once inside "
        "the build environment shell, use \"rpmbuild -bi --short-circuit "
        "~/rpmbuild/SOURCES/<spec>.spec\" (or \"rpmbuild -bb --short-circuit\") "
        "to skip earlier phases and re-run just the %install / %files checking phase. "
        "This dramatically speeds up iteration when fixing file lists. To determine "
        "which files a package installs, use read_file_from_archive to inspect files "
        "inside the LOCAL source tarball — do NOT use web_fetch to fetch source files "
        "from upstream websites, as the web version may differ from the local tarball.\n"
        "- 'Installed (but unpackaged) file(s) found:' warnings: this is a "
        "straightforward fix. Do NOT use [DEEP_ANALYZE]. Instead, add every single "
        "file from the list to the %files section. Do not skip any files — paths like "
        "/usr/share/info, /usr/share/man, /usr/share/doc etc. are NOT automatically "
        "handled. Use the appropriate RPM path macros where possible.\n"
        "Note: debuginfo/debugsource subpackages are automatically generated by RPM "
        "for any binaries listed in %files. Do NOT create %package, %description, or "
        "%files sections for -debuginfo or -debugsource manually — they will conflict "
        "with the auto-generated ones.\n\n"
        "If the build log shows a '%prep' failure with 'cd: <dirname>: No such file or directory' "
        "in the build output: the source tarball extracts to a different top-level directory "
        "than %setup -n (or the manual cd command) expects. Do NOT use [DEEP_ANALYZE]. Instead, "
        "instruct the fix to use list_archive on the local source tarball to determine the actual "
        "directory name, then correct %setup -n or the cd command accordingly. "
        "This is a quick fix — do NOT waste time on interactive shell investigation.\n\n"
        "Do NOT add or modify BuildRoot: tags in the spec. The BuildRoot tag is "
        "obsolete — modern RPM sets the build root automatically, and changing it "
        "will not fix build failures."
    )
