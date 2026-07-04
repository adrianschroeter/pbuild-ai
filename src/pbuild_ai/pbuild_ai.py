# Copyright (C) 2027 SUSE Linux Products GmbH / Adrian Schröter <adrian@suse.de>
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

import os
import sys
import subprocess
import json
import urllib.request
import re
import argparse
import threading
import time
import shutil
import difflib
import datetime
from pathlib import Path
from pbuild_ai.spinner import _color, AI_COLOR

if __name__ == "__main__" and not __package__:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if __name__ == "__main__" and "--version" in sys.argv:
    import importlib.metadata
    _ver = "undefined (not installed)"
    _local = False
    try:
        _dist = importlib.metadata.distribution("pbuild-ai")
        if _dist:
            _installed_ver = _dist.version
            # Check if the dist-info is in the same site-packages tree as __file__
            _dist_info = Path(_dist.locate_file("")).resolve()
            _running = Path(__file__).resolve()
            _in_site_packages = _dist_info.parent in _running.parents
            if _in_site_packages:
                _ver = _installed_ver
            else:
                _local = True
    except Exception:
        pass
    if _local:
        _pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
        _v = re.search(r'^version\s*=\s*"([^"]+)"', _pyproject.read_text(), re.M) if _pyproject.exists() else None
        _ver = _v.group(1) if _v else _ver
    print(f"pbuild-ai version {_ver}")
    sys.exit(0)

from pbuild_ai.manifest import list_packages
from pbuild_ai.diff_utils import show_diff
from pbuild_ai.tools import execute_tool_calls, build_tools_list
from pbuild_ai.skill_manager import SkillManager
from pbuild_ai.ollama_client import OllamaAnalyzer
from pbuild_ai.workspace import RpmSourceManager
from pbuild_ai.parsing import parse_agents_md_scripts, parse_failed_package, extract_spec, find_rpm_tags, apply_spec_insertions
from pbuild_ai.context import PbuildContext
from pbuild_ai.skills.changelog_skill import CHANGELOG_PROMPT, write_changelog_entry
from pbuild_ai.skills.version_research_skill import VERSION_RESEARCH_SYSTEM_PROMPT, VERSION_UPDATE_PROMPT
from pbuild_ai.generate_mode import run_generate_mode
from pbuild_ai.modify_mode import run_modify_mode

import importlib.metadata as _ilm
_ver = "undefined (not installed)"
try:
    _dist = _ilm.distribution("pbuild-ai")
    if _dist:
        _installed_ver = _dist.version
        _dist_info = Path(_dist.locate_file("")).resolve()
        _running = Path(__file__).resolve()
        _in_site_packages = _dist_info.parent in _running.parents
        if _in_site_packages:
            _ver = _installed_ver
        else:
            _pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
            _v = re.search(r'^version\s*=\s*"([^"]+)"', _pyproject.read_text(), re.M) if _pyproject.exists() else None
            _ver = _v.group(1) if _v else _installed_ver
except Exception:
    pass
__version__ = _ver


def _is_source_or_build_path(name: str) -> bool:
    """Return True if name looks like a build source path, not an RPM package file."""
    if re.search(r'\.(spec|patch|changes|tar\.(gz|xz|bz2)|tar|zip)$', name):
        return True
    if re.search(r'[\\/]\.build\.[\\/]', name) or '/BUILD/' in name or '/SOURCES/' in name:
        return True
    if name.startswith('/'):
        return True
    return False


_ENVIRONMENT_ERROR_PATTERNS = [
    "incomplete setup of /",
    "init_buildsystem",
    "sysrq: Power Off",
    ".mount.img",
    ".mount.swap",
    "kernel panic",
    "cannot mount",
    "corrupted",
    "stale",
    "VFS: Unable to mount root",
]

def _is_environment_error(build_out: str) -> bool:
    """Return True if build output indicates an environment/VM/infrastructure issue
    rather than a spec/packaging issue."""
    if not build_out:
        return False
    _lower = build_out.lower()
    # Check direct patterns
    for _pat in _ENVIRONMENT_ERROR_PATTERNS:
        if _pat in _lower:
            return True
    # Check for pbuild-level errors before any RPM phase
    _first_lines = build_out.split('\n')[:8]
    for _line in _first_lines:
        if "riopp" in _line.lower() and ("error" in _line.lower() or "fail" in _line.lower()):
            return True
        if "pbuild" in _line.lower() and ("error" in _line.lower() or "fail" in _line.lower()):
            return True
    return False

def _is_comment_only_change(original: str, modified: str) -> bool:
    """Return True if the only differences between original and modified
    are comment lines (starting with #) or blank/whitespace-only lines."""
    orig_lines = original.splitlines(keepends=True)
    mod_lines = modified.splitlines(keepends=True)
    diff = list(difflib.unified_diff(orig_lines, mod_lines, n=0))
    for line in diff:
        if line.startswith('---') or line.startswith('+++') or line.startswith('@@'):
            continue
        if line.startswith('-') or line.startswith('+'):
            content = line[1:].strip()
            if content and not content.startswith('#'):
                return False
    return True


def _extract_source_tarball_hint(spec_content: str, spec_path, workspace_dir: str = "") -> str:
    """Extract the local source tarball filename from the spec's Source: line
    and return a hint for the LLM to use read_file_from_archive."""
    if not spec_content:
        return ""
    _macros = {}
    for _kv in re.finditer(r'^(Name|Version):\s*(\S+)', spec_content, re.M):
        _macros[_kv.group(1).lower()] = _kv.group(2)
    for line in spec_content.split('\n'):
        m = re.match(r'^Source\d*:\s*(\S+)', line, re.I)
        if m:
            url = m.group(1).strip()
            _expanded = url
            for _key, _val in _macros.items():
                _expanded = _expanded.replace(f'%{{{_key}}}', _val)
            from urllib.parse import urlparse
            fname = Path(urlparse(_expanded).path).name if '://' in _expanded else Path(_expanded).name
            if not fname or fname == '.':
                fname = _expanded
            if fname.endswith(('.tar.gz', '.tar.bz2', '.tar.xz', '.tar', '.zip')):
                rel = ""
                if workspace_dir:
                    try:
                        rel = str(Path(spec_path).relative_to(Path(workspace_dir)))
                    except Exception:
                        rel = str(spec_path)
                tarball_path = str(Path(rel).parent / fname) if rel and Path(rel).parent != Path('.') else fname
                return (f"Local source tarball: {tarball_path} — use read_file_from_archive"
                        f"(archive_path=\"{tarball_path}\", file_path=\"...\") to inspect its contents.\n\n")
            break
    return ""


def _extract_error_context(text, context_lines=3, max_errors=30):
    """Extract lines containing 'error:' plus surrounding context from build output.
    Prepend these before the full log so they survive MAX_PROMPT_CHARS truncation.
    Returns empty string if no error lines found.
    """
    if not text:
        return ''
    lines = text.split('\n')
    error_indices = set()
    for i, line in enumerate(lines):
        if 'error:' in line.lower():
            for j in range(max(0, i - context_lines), min(len(lines), i + 1)):
                error_indices.add(j)
    if not error_indices:
        return ''
    sorted_idx = sorted(error_indices)[:max_errors * (context_lines + 1)]
    result = ['--- Build errors (with context) ---']
    prev = -999
    for idx in sorted_idx:
        if idx > prev + 1:
            result.append('...')
        result.append(lines[idx])
        prev = idx
    result.append('--- End ---')
    return '\n'.join(result)


def _inject_gitexplorer_results(error_prompt: str, build_out: str) -> str:
    """Enrich error_prompt with package lookup results from gitexplorer API.
    Extracts missing filenames, unresolvable package names, and unowned
    directories from build_out, queries the API, and appends results to
    the prompt. Skips source/build paths (spec, patch, tar, etc.).
    """
    try:
        from pbuild_ai.query_gitexplorer import query_package_by_file, query_package_by_name, format_results
        from pbuild_ai.skills.unresolvable_skill import (
            parse_missing_filename_from_log,
            parse_unresolved_package_from_log,
            parse_unowned_directory_from_log,
        )

        if not any([
            parse_missing_filename_from_log(build_out),
            parse_unresolved_package_from_log(build_out),
            parse_unowned_directory_from_log(build_out),
        ]):
            return error_prompt

        lines = []

        filename = parse_missing_filename_from_log(build_out)
        if filename and not _is_source_or_build_path(filename):
            print(f"[GITEXPLORER] Querying files endpoint for: {filename}")
            results = query_package_by_file(filename)
            if results:
                lines.append(f"Packages providing '{filename}':")
                lines.append(format_results(results))

        pkg = parse_unresolved_package_from_log(build_out)
        if pkg:
            print(f"[GITEXPLORER] Querying packages endpoint for: {pkg}")
            results = query_package_by_name(pkg)
            if results:
                lines.append(f"Packages matching '{pkg}':")
                lines.append(format_results(results))

        directory = parse_unowned_directory_from_log(build_out)
        if directory:
            print(f"[GITEXPLORER] Querying files endpoint for unowned directory: {directory}")
            results = query_package_by_file(directory)
            if results:
                lines.append(f"Packages providing directory '{directory}':")
                lines.append(format_results(results))

        if lines:
            enriched = error_prompt + "\n\n" + "\n".join(lines)
            return enriched
    except Exception:
        pass
    return error_prompt


def _apply_analysis_fix_patterns(spec: Path, spec_content: str, error_analysis: str) -> str | None:
    """Apply well-known fix patterns directly from the analysis text without a model call.
    
    Returns the new spec content if a pattern was applied, or None if no pattern matched.
    """
    lines = spec_content.split("\n")
    analysis_lower = error_analysis.lower()
    modified = False

    # Pattern: %setup → %autosetup in %prep section
    if "%autosetup" in analysis_lower and not any("%autosetup" in l for l in lines):
        for i, line in enumerate(lines):
            stripped = line.strip()
            if re.match(r'^%setup(?:$|[-\s])', stripped):
                lines[i] = line.replace(stripped, "%autosetup")
                modified = True
                break

    if modified:
        new_content = "\n".join(lines)
        show_diff(spec_content, new_content, spec)
        return new_content
    return None


def _run_build_guard(spec, manager, ollama, full_context, error_prompt, ctx, program_start,
                     run_fix_loop_func):
    """Execute pbuild for a spec if fix_mode or update_version is set, otherwise skip.
    
    Returns enriched error_prompt (or original if gitexplorer wasn't triggered).
    """
    if ctx.fix_mode or ctx.update_version is not None:
        PACKAGE_FILTER = ctx.package_filter
        PROJECT_MODE = ctx.project_mode
        PRESET = ctx.preset
        SHOW_BUILDLOG = ctx.show_buildlog

        if PACKAGE_FILTER:
            package_name = spec.stem
            print(f"[INFO] Building single package: {package_name}...")
            build_success, build_out = manager.run_project_build(package_name, preset=PRESET, stream_output=SHOW_BUILDLOG)
        elif PROJECT_MODE:
            package_name = spec.stem
            print(f"[INFO] Building {package_name} from project directory...")
            build_success, build_out = manager.run_project_build(package_name, preset=PRESET, stream_output=SHOW_BUILDLOG)
        else:
            print("[INFO] Single package mode (no _manifest found). Running orphan build...")
            build_success, build_out = manager.run_orphan_build(stream_output=SHOW_BUILDLOG)

        INCOMPLETE_SETUP_MSG = "It seems that there was an incomplete setup of /"
        if not build_success and INCOMPLETE_SETUP_MSG in build_out:
            print(f"[RETRY] Incomplete setup detected. Retrying with --clean...")
            if PACKAGE_FILTER or PROJECT_MODE:
                build_success, build_out = manager.run_project_build(package_name, preset=PRESET, stream_output=SHOW_BUILDLOG, force_clean=True)
            else:
                build_success, build_out = manager.run_orphan_build(stream_output=SHOW_BUILDLOG, force_clean=True)

        if not build_success:
            if not manager.build_phase_reached(package_name=spec.stem):
                print(f"[RETRY] Build did not reach build phase. Retrying with --clean...")
                if PACKAGE_FILTER or PROJECT_MODE:
                    build_success, build_out = manager.run_project_build(package_name, preset=PRESET, stream_output=SHOW_BUILDLOG, force_clean=True)
                else:
                    build_success, build_out = manager.run_orphan_build(stream_output=SHOW_BUILDLOG, force_clean=True)
                if build_success:
                    print(f"\n[OK] Build for {spec.name} succeeded after --clean retry.")
                else:
                    print(f"[RETRY] Clean build also failed. Proceeding with fix mode.")

        if not build_success and build_out:
            error_prompt = _inject_gitexplorer_results(error_prompt, build_out)

        if build_success:
            print(f"\n[OK] Build for {spec.name} succeeded.")
        else:
            print(f"\n[ERROR] Build for {spec.name} failed. Consulting {ollama.model}...")
            if not ctx.fix_mode:
                _err_ctx = _extract_error_context(build_out)
                error_analysis = ollama.analyze(error_prompt, f"{_err_ctx}\n\n{build_out}" if _err_ctx else build_out, full_context)
                print(f"\n{_color(AI_COLOR, '--- OLLAMA ERROR ANALYSIS ---')}\n{error_analysis}\n{_color(AI_COLOR, '-----------------------------')}\n")
                ollama._write_analysis_file(error_analysis)

        if ctx.fix_mode and not build_success:
            pkg_name = package_name if 'package_name' in dir() else spec.stem
            if PROJECT_MODE:
                rebuild_func = lambda p, force_clean=False: manager.run_project_build(p, stream_output=SHOW_BUILDLOG, force_clean=force_clean)
            else:
                rebuild_func = lambda p, force_clean=False: manager.run_orphan_build(stream_output=SHOW_BUILDLOG, force_clean=force_clean)
            run_fix_loop_func(spec, pkg_name, build_out, error_prompt, rebuild_func, exit_on_no_changes=True)

    return error_prompt


def _check_arg_conflicts(parser, args):
    """Validate argument conflicts and call parser.error() on violation."""
    if args.fix:
        _fix_conflicts = []
        if args.analyze:
            _fix_conflicts.append('--analyze')
        if args.changelog:
            _fix_conflicts.append('--changelog')
        if _fix_conflicts:
            parser.error(f"--fix cannot be used with: {', '.join(_fix_conflicts)}")
    if args.analyze:
        _analyze_conflicts = []
        if args.update or args.update_only:
            _analyze_conflicts.append('--update')
        if args.generate:
            _analyze_conflicts.append('--generate')
        if args.changelog:
            _analyze_conflicts.append('--changelog')
        if args.modify:
            _analyze_conflicts.append('--modify')
        if _analyze_conflicts:
            parser.error(f"--analyze cannot be used with: {', '.join(_analyze_conflicts)}")


def _check_update_hints(results, messages, spec_files, spec_originals, updated_packages, manager):
    """After an Ollama phase, check for [REBUILD: pkg] hints and cross-package spec edits."""
    if messages:
        assistant_text = " ".join(m.get("content", "") or "" for m in messages if m.get("role") == "assistant")
        for m in re.finditer(r'\[REBUILD:\s*(\S+?)(?:\.spec)?\]', assistant_text):
            pkg = m.group(1)
            for spec in spec_files:
                if spec.stem == pkg:
                    if spec not in updated_packages:
                        updated_packages.add(spec)
                        print(f"[UPDATE] AI hinted rebuild for {spec.name}")
                    break
    for spec in spec_files:
        current = manager.read_file_safe(spec)
        original = spec_originals.get(spec)
        if original is not None and current != original and spec not in updated_packages:
            updated_packages.add(spec)
            print(f"[UPDATE] AI modified {spec.name} ({len(current)}b vs {len(original)}b original)")


# ==========================================
# Main Application Logic
# ==========================================
if __name__ == "__main__":
    import builtins as _builtins
    _original_print = _builtins.print
    def _flush_print(*args, **kwargs):
        kwargs.setdefault("flush", True)
        _original_print(*args, **kwargs)
    _builtins.print = _flush_print

    _orig_sys_exit = sys.exit
    def _exit(code=0):
        print(f"[EXIT] Exiting with code {code}.")
        _orig_sys_exit(code)
    sys.exit = _exit

    parser = argparse.ArgumentParser(
        description="RPM packager helper with AI-powered build-fix and version-update.\n"
                    "Main commands: --analyze, --fix, --update, --generate, --modify",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("workspace_dir", help="Path to the workspace directory")
    parser.add_argument("package_name", nargs="?", default=None, help="Package name to focus on (only in project mode)")
    parser.add_argument("--analyze", "-a", action="store_true", help="Main command: analyze spec files and exit (default). Conflicts with --fix, --update, --generate, --changelog, --modify.")
    parser.add_argument("--version", action="store_true", help="Show version and exit")
    parser.add_argument("--fix", "-f", action="store_true", help="Main command: apply AI-suggested fixes to build failures and run test builds to verify")
    parser.add_argument("--update", "-u", action="store_true", help="Main command: update to latest upstream version (also enables --fix). Use --update=VERSION for a specific version.")
    parser.add_argument("--generate", default=None, help="Main command: generate a new package from scratch based on the given prompt")
    parser.add_argument("--modify", "-m", default=None, help="Main command: send a modification prompt + sources to Ollama, apply changes locally, then exit (no build)")
    parser.add_argument("--root", default=None, help="Root directory for pbuild (passed as --root to pbuild)")
    parser.add_argument("--show-buildlog", "-L", action="store_true", help="Show the pbuild build log output")
    parser.add_argument("--build-log", default=None, help="Write the full pbuild build log to this file")
    parser.add_argument("--shell-after-build", action="store_true", help="Open a shell in the build environment on failure for debugging")
    parser.add_argument("--vm-type", default=None, help="VM type for pbuild (e.g., kvm, qemu)")
    parser.add_argument("--vm-memory", default=None, help="VM memory for pbuild (e.g., 4096)")
    # Pre-process --update=VERSION and --update-only=VERSION into flag + --update-version=VERSION
    # to disallow the ambiguous space-separated --update VERSION syntax
    update_version_value = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg.startswith("--update=") or arg.startswith("-u="):
            prefix = arg.split("=", 1)[0]
            update_version_value = arg.split("=", 1)[1]
            sys.argv[i] = f"{prefix}"
            sys.argv.insert(i + 1, f"--update-version={update_version_value}")
            break
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg.startswith("--update-only="):
            update_version_value = arg.split("=", 1)[1]
            sys.argv[i] = "--update-only"
            sys.argv.insert(i + 1, f"--update-version={update_version_value}")
            break

    parser.add_argument("--update-only", action="store_true", help="Update sources to the latest upstream version, then exit (no test build). Use --update-only=VERSION for a specific version.")
    parser.add_argument("--update-version", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--preset", default=None, help="Preset name to pass to pbuild")
    parser.add_argument("--allow-tool-scripts", action="store_true", help="Allow execution of scripts from <workspace>/tool-scripts/")
    parser.add_argument("--debug", "-D", action="store_true", help="Print raw JSON responses from Ollama")
    parser.add_argument("--fix-attempts", type=int, default=10, help="Max fix retry attempts per package (default: 10, resets for each package)")
    parser.add_argument("--max-rounds", type=int, default=15, help="Max tool-call rounds per fix attempt (default: 15, 0 = unlimited)")
    parser.add_argument("--deep-analyze", "-d", action="store_true", help="On build failure, open an interactive shell in the build environment instead of auto-fixing")
    parser.add_argument("--prompt", "-p", default=None, help="Additional hint to include in all analysis prompts sent to Ollama")
    parser.add_argument("--fresh", action="store_true", help="Discard saved .pai.context and start fresh")
    parser.add_argument("-i", "--interactive", action="store_true", help="Ask the user to select which changes to apply when Ollama proposes multiple tool calls")
    parser.add_argument("--openai-server", default=None, help="OpenAI-compatible server URL (overrides OLLAMA_HOST env var, default http://localhost:11434)")
    parser.add_argument("--model", default=None, help="Ollama model name (overrides OLLAMA_MODEL env var, default gemma4)")
    parser.add_argument("--ollama-timeout", type=int, default=None, help="Timeout in seconds for Ollama API requests (default: 900, overrides OLLAMA_TIMEOUT env var)")
    parser.add_argument("--email", default=None, help="Email address for PACKAGE.changes entries. Falls back to EMAIL env var.")
    parser.add_argument("--changelog", action="store_true", help="Prepend a changelog entry for the current version, then exit")
    clean_group = parser.add_mutually_exclusive_group()
    clean_group.add_argument("--clean", action="store_true", default=False, help="Clean build artifacts before building")
    clean_group.add_argument("--no-clean", action="store_true", default=True, help="Do not clean build artifacts (default)")
    args = parser.parse_args()

    if args.version:
        print(f"pbuild-ai version {__version__}")
        sys.exit(0)

    print(f"[PBUILD-AI] Version {__version__}")

    _check_arg_conflicts(parser, args)

    ctx = PbuildContext(
        workspace_dir=args.workspace_dir,
        root_dir=args.root,
        package_filter=args.package_name,
        fix_mode=args.fix or args.update,
        show_buildlog=args.show_buildlog,
        do_clean=args.clean,
        vm_type=args.vm_type,
        vm_memory=args.vm_memory,
        preset=args.preset,
        allow_tool_scripts=args.allow_tool_scripts,
        debug=args.debug,
        deep_analyze=args.deep_analyze,
        fix_attempts=args.fix_attempts,
        prompt_hint=args.prompt,
        update_version=args.update_version or "" if (args.update or args.update_only) else None,
        update_only=args.update_only,
        modify_prompt=args.modify,
        generate_prompt=args.generate,
        ollama_server=args.openai_server,
        ollama_model_arg=args.model,
        ollama_timeout=args.ollama_timeout or int(os.environ.get("OLLAMA_TIMEOUT", "900")),
        shell_after_build=args.shell_after_build,
        interactive=args.interactive,
        email=args.email or os.environ.get("EMAIL", ""),
        analyze_mode=args.analyze,
        max_rounds=args.max_rounds,
        build_log=args.build_log,
        program_start=time.time(),
    )

    # Local aliases for backward compatibility with remaining inline code
    WORKSPACE_DIR = ctx.workspace_dir
    PACKAGE_FILTER = ctx.package_filter
    PROJECT_MODE = (Path(WORKSPACE_DIR) / "_manifest").is_file()
    FIX_MODE = ctx.fix_mode
    SHOW_BUILDLOG = ctx.show_buildlog
    PRESET = ctx.preset
    DO_CLEAN = ctx.do_clean
    ALLOW_TOOL_SCRIPTS = ctx.allow_tool_scripts
    DEBUG = ctx.debug
    EMAIL = ctx.email
    DEEP_ANALYZE = ctx.deep_analyze
    ANALYZE_MODE = ctx.analyze_mode
    if ANALYZE_MODE:
        print("[INFO] Analyze mode (--analyze). Use --fix or --update to build.")
    FIX_ATTEMPTS = ctx.fix_attempts
    PROMPT_HINT = ctx.prompt_hint
    UPDATE_VERSION = ctx.update_version
    INTERACTIVE = ctx.interactive
    MODIFY_PROMPT = ctx.modify_prompt
    GENERATE_PROMPT = ctx.generate_prompt
    OPENAI_SERVER = ctx.ollama_server
    OLLAMA_MODEL_ARG = ctx.ollama_model_arg
    ROOT_DIR = ctx.root_dir
    SKILLS_DIR = Path(__file__).parent / "skills"

    Path(WORKSPACE_DIR).mkdir(exist_ok=True)
    ctx.project_mode = PROJECT_MODE

    context_file_path = Path(WORKSPACE_DIR) / ".pai.context"
    if args.fresh and context_file_path.exists():
        context_file_path.unlink()
        print("[INFO] Discarded saved .pai.context (--fresh).")

    manager = RpmSourceManager(WORKSPACE_DIR, do_clean=DO_CLEAN, vm_type=ctx.vm_type, vm_memory=ctx.vm_memory, shell_after_build=ctx.shell_after_build, preset=PRESET, root_dir=ROOT_DIR, build_log_path=ctx.build_log)
    skill_manager = SkillManager(SKILLS_DIR)
    ctx.manager = manager
    ctx.skill_manager = skill_manager
    
    agents_md_content = manager.read_agents_md()
    if agents_md_content:
        print(f"[INFO] AGENTS.md found. Using for Ollama context.")
    
    # Always include base skill content in the prompt
    base_skill_content = skill_manager.base_skill_content or ""
    if base_skill_content:
        full_context = f"{agents_md_content}\n\n--- Base Skill (OPENSUSE.md) ---\n{base_skill_content}"
    else:
        full_context = agents_md_content
    
    ollama = OllamaAnalyzer(host=OPENAI_SERVER, model=OLLAMA_MODEL_ARG or os.environ.get("OLLAMA_MODEL", "default"), debug=DEBUG, timeout=ctx.ollama_timeout)
    ollama.manager = manager
    ctx.ollama = ollama
    ctx.full_context = full_context

    # Default prompts as fallback
    DEFAULT_SPEC_PROMPT = ctx.default_spec_prompt
    DEFAULT_ERROR_PROMPT = ctx.default_error_prompt
    
    def default_fix(content):
        return content # Does nothing if no skill matches

    # Define allowed tools (file operations within workspace + remote web fetching)
    TOOLS = build_tools_list(interactive=INTERACTIVE)
    ctx.tools = TOOLS

    def apply_build_order(spec_files):
        """Always parse AGENTS.md for build order hints, reorder spec_files, and skip unwanted packages."""
        agents = manager.read_agents_md()
        if not agents and not PACKAGE_FILTER:
            return
        scripts_dir = Path(WORKSPACE_DIR) / "tool-scripts"
        if not scripts_dir.is_dir():
            scripts_dir = Path(WORKSPACE_DIR)  # fallback: just need an existing dir for parsing

        # When a specific package is requested on CLI, prioritize it over AGENTS.md build order
        if PACKAGE_FILTER:
            for i, s in enumerate(spec_files):
                if s.stem == PACKAGE_FILTER:
                    spec_files.insert(0, spec_files.pop(i))
                    break
            # Still apply skip_pkgs (AGENTS.md skip rules) — the requested package is excluded anyway
            _, _, skip_pkgs = parse_agents_md_scripts(agents or "", scripts_dir)
            if skip_pkgs:
                effective_skip = [p for p in skip_pkgs if p != PACKAGE_FILTER]
                if effective_skip != skip_pkgs:
                    print(f"[INFO] Package '{PACKAGE_FILTER}' explicitly requested, ignoring skip rule for it.")
                skipped = [s for s in spec_files if s.stem in effective_skip]
                if skipped:
                    print(f"[INFO] Skipping packages (per AGENTS.md): {', '.join(s.stem for s in skipped)}")
                spec_files[:] = [s for s in spec_files if s.stem not in effective_skip]
            return  # Skip AGENTS.md build order reordering when a specific package is requested

        _, build_order_hints, skip_pkgs = parse_agents_md_scripts(agents, scripts_dir)

        # Filter out skipped packages
        if skip_pkgs:
            skipped = [s for s in spec_files if s.stem in skip_pkgs]
            if skipped:
                print(f"[INFO] Skipping packages (per AGENTS.md): {', '.join(s.stem for s in skipped)}")
            spec_files[:] = [s for s in spec_files if s.stem not in skip_pkgs]

        # Apply build order hints to remaining specs
        if build_order_hints:
            print(f"[INFO] AGENTS.md build order hint: {', '.join(build_order_hints)}")
            hinted = [p for p in build_order_hints if any(s.stem == p for s in spec_files)]
            remaining = [s for s in spec_files if s.stem not in build_order_hints]
            reordered = []
            for pkg_name in hinted:
                for s in list(spec_files):
                    if s.stem == pkg_name:
                        reordered.append(s)
                        break
            reordered.extend(remaining)
            spec_files[:] = reordered

    def run_prebuild_scripts(spec_files):
        """Execute pre-build scripts from tool-scripts/ directory (only if --allow-tool-scripts)."""
        if not ALLOW_TOOL_SCRIPTS:
            return
        
        scripts_dir = Path(WORKSPACE_DIR) / "tool-scripts"
        if not scripts_dir.is_dir():
            print("[INFO] --allow-tool-scripts set but no tool-scripts/ directory found.")
            return

        # Make all scripts executable
        for script in scripts_dir.iterdir():
            if script.is_file():
                script.chmod(script.stat().st_mode | 0o111)
        
        # Parse AGENTS.md for startup script hints
        agents = manager.read_agents_md()
        startup_scripts, _, _ = parse_agents_md_scripts(agents or "", scripts_dir)
        
        # If no startup scripts hinted, run all scripts in sorted order
        if not startup_scripts:
            startup_scripts = sorted(
                f.name for f in scripts_dir.iterdir() if f.is_file()
            )
        
        for script_name in startup_scripts:
            script_path = scripts_dir / script_name
            if not script_path.is_file():
                print(f"[WARN] startup script {script_name} not found in tool-scripts/")
                continue
            print(f"[PREBUILD] Executing {script_name}...")
            try:
                result = subprocess.run(
                    [str(script_path)],
                    capture_output=True, text=True, cwd=WORKSPACE_DIR
                )
                out = (result.stdout or "").strip()
                err = (result.stderr or "").strip()
                if out:
                    print(f"[PREBUILD] {script_name} stdout:\n{out}")
                if err:
                    print(f"[PREBUILD] {script_name} stderr:\n{err}")
                if result.returncode != 0:
                    print(f"[WARN] {script_name} exited with code {result.returncode}")
            except Exception as e:
                print(f"[WARN] Failed to execute {script_name}: {e}")

    def _is_safe_url(url):
        return is_safe_url(url)

    def _resolve_path(path_str, for_write=False):
        from utils import resolve_path as _rp
        return _rp(path_str, WORKSPACE_DIR, for_write=for_write)


    def export_deep_fix_patch(workspace_dir, spec, package_stem):
        """Create a patch from workspace changes and add it to the spec file."""
        patch_name = f"{package_stem}-deep-fix.patch"
        patch_path = Path(workspace_dir) / patch_name
        patch_content = None

        try:
            r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                               cwd=workspace_dir, capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                r2 = subprocess.run(["git", "diff"],
                                    cwd=workspace_dir, capture_output=True, text=True, timeout=30)
                if r2.stdout.strip():
                    patch_content = r2.stdout
                else:
                    r3 = subprocess.run(["git", "diff", "--staged"],
                                        cwd=workspace_dir, capture_output=True, text=True, timeout=30)
                    if r3.stdout.strip():
                        patch_content = r3.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        if not patch_content:
            print("[PATCH] No git diff available. Skipping patch export.")
            return

        patch_path.write_text(patch_content)
        print(f"[PATCH] Exported changes to {patch_name} ({len(patch_content)} bytes)")

        spec_content = spec.read_text(encoding='utf-8')
        if f"Patch0: {patch_name}" in spec_content or f"Patch: {patch_name}" in spec_content:
            print("[PATCH] Patch already referenced in spec. Skipping spec update.")
            return

        lines = spec_content.split('\n')
        patch_num = 0
        for line in lines:
            m = re.match(r'^Patch(\d+):', line)
            if m:
                n = int(m.group(1))
                if n >= patch_num:
                    patch_num = n + 1
            elif re.match(r'^Patch:\s', line):
                if 0 >= patch_num:
                    patch_num = 1

        patch_ref = f"Patch{patch_num}: {patch_name}"

        insert_pos = None
        for i, line in enumerate(lines):
            if line.strip().startswith(('Source', 'Patch')):
                insert_pos = i + 1
            elif line.strip().startswith('%description'):
                if insert_pos is not None:
                    break

        if insert_pos is None:
            insert_pos = len(lines)
        lines.insert(insert_pos, patch_ref)

        setup_line = None
        for i, line in enumerate(lines):
            if line.strip().startswith('%setup'):
                setup_line = i
                break

        if setup_line is not None:
            lines.insert(setup_line + 1, f'%patch{patch_num} -p1')

        spec.write_text('\n'.join(lines), encoding='utf-8')
        print(f"[PATCH] Added {patch_ref} and %patch{patch_num} -p1 to spec.")

    def relocate_patches(tool_results, spec):
        """Move written .patch files to the spec's directory and update the spec to apply them."""
        for result in tool_results:
            if not (result.startswith("write_file: OK: Wrote ") or result.startswith("OK: Wrote ")) or not result.endswith(".patch"):
                continue
            rel_path = result[len("OK: Wrote "):]
            src = _resolve_path(rel_path)
            if not src or not src.exists():
                continue
            dst = spec.parent / src.name
            if src != dst:
                shutil.move(str(src), str(dst))
                print(f"[PATCH] Moved {rel_path} to {dst.relative_to(Path(WORKSPACE_DIR))}")
            patch_name = dst.name
            spec_content = spec.read_text(encoding='utf-8')
            patch_num = 0
            for line in spec_content.split('\n'):
                m = re.match(r'^Patch(\d+):', line)
                if m:
                    n = int(m.group(1))
                    if n >= patch_num:
                        patch_num = n + 1
            patch_ref = f"Patch{patch_num}: {patch_name}"
            if patch_ref in spec_content:
                print(f"[PATCH] {patch_ref} already in spec.")
                return
            lines = spec_content.split('\n')
            insert_pos = None
            for i, line in enumerate(lines):
                if line.strip().startswith(('Source', 'Patch')):
                    insert_pos = i + 1
                elif line.strip().startswith('%description'):
                    if insert_pos is not None:
                        break
            if insert_pos is None:
                insert_pos = len(lines)
            lines.insert(insert_pos, patch_ref)
            for i, line in enumerate(lines):
                if line.strip().startswith('%setup'):
                    lines.insert(i + 1, f'%patch{patch_num} -p1')
                    break
            spec.write_text('\n'.join(lines), encoding='utf-8')
            print(f"[PATCH] Added {patch_ref} and %patch{patch_num} -p1 to spec.")
            break  # only handle first patch per round

    def build_suggested_dependency(analysis, spec_files, manager, ollama, full_context):
        """If Ollama suggests building another package first, build it and return True."""
        spec_map = {s.stem: s for s in spec_files}
        lower = analysis.lower()
        patterns = [
            r'(?:first\s+build|build\s+.*?first|need\s+to\s+build|before\s+building|prerequisite|depends?\s+on)\s+[`\']?([\w][\w\-\.\+]*)',
            r'([\w][\w\-\.\+]*)\s+(?:needs?\s+to\s+be\s+built|must\s+be\s+built|should\s+be\s+built)\s+first',
        ]
        for pat in patterns:
            for m in re.finditer(pat, lower):
                suggested = m.group(1)
                if suggested in spec_map:
                    print(f"\n[DEP] Ollama suggests building '{suggested}' first. Building it now...")
                    dep_spec = spec_map[suggested]
                    dep_skills = skill_manager.get_skills_for(dep_spec.name, manager.read_file_safe(dep_spec))
                    if dep_skills:
                        dep_prompt_parts = [getattr(s, 'OLLAMA_SPEC_PROMPT', '') for s in dep_skills if getattr(s, 'OLLAMA_SPEC_PROMPT', '')]
                        dep_prompt = "\n\n".join(dep_prompt_parts) if dep_prompt_parts else DEFAULT_SPEC_PROMPT
                    else:
                        dep_prompt = DEFAULT_SPEC_PROMPT
                    dep_spec_analysis = ollama.analyze(dep_prompt, manager.read_file_safe(dep_spec), full_context)
                    print(f"-> AI({ollama.model}) says about {dep_spec.name}:\n{dep_spec_analysis}\n")
                    dep_success, dep_out = manager.run_project_build(suggested, stream_output=SHOW_BUILDLOG)
                    if dep_success:
                        print(f"[DEP] '{suggested}' built successfully. Continuing with current package.")
                        return True
                    else:
                        print(f"[DEP WARN] '{suggested}' build also failed. Trying to continue with current package anyway.")
                        return False
        return False

    def run_fix_loop(spec, package_name, initial_build_out, error_prompt, rebuild_func, exit_on_no_changes=False):
        """Shared fix loop: diagnose → apply tool changes → rebuild → repeat."""
        MAX_ATTEMPTS = FIX_ATTEMPTS if FIX_ATTEMPTS > 0 else 999999
        unlimited = FIX_ATTEMPTS == 0
        fix_attempt = 0
        current_build_out = initial_build_out
        messages = None
        build_success2 = False
        _latest_analysis = ""
        _ctx_file = Path(WORKSPACE_DIR) / ".pai.context"
        _prev_spec = ""
        _prev_diff_summary = ""
        _prev_error_context = ""
        _prev_error_analysis = ""
        _attempted_fixes = []
        import hashlib as _hashlib
        _initial_spec_hash = _hashlib.md5(manager.read_file_safe(spec).encode()).hexdigest()
        _spec_version_hashes = [(0, _initial_spec_hash)]

        def _build_attempted_fixes_context():
            if not _attempted_fixes:
                return ""
            lines = ["PREVIOUSLY ATTEMPTED FIXES (ALL FAILED — do NOT suggest these again):"]
            for _att, _diff, _err in _attempted_fixes:
                _diff_short = _diff.replace('\n', ' | ')[:200] if _diff else '(no diff)'
                _err_short = (_err or '')[:200].replace('\n', ' | ')
                lines.append(f"- Attempt {_att}: {_diff_short}")
                lines.append(f"  Result: {_err_short}")
            return "\n".join(lines) + "\n\n"

        # Load saved context (if any) for the same spec
        if _ctx_file.exists():
            try:
                _saved = json.loads(_ctx_file.read_text())
                if _saved.get("spec_path") == str(spec.relative_to(WORKSPACE_DIR)):
                    print(f"[FIX] Loaded saved context from {_ctx_file.name}")
                    messages = _saved.get("messages") or _saved.get("fix_messages", [])
                    if PROMPT_HINT:
                        messages.append({"role": "user", "content": f"--- User Hint ---\n{PROMPT_HINT}"})
                else:
                    print(f"[FIX] Stale context (for {_saved.get('spec_path')}), discarding.")
                    _ctx_file.unlink()
            except Exception as e:
                print(f"[FIX] Corrupt context file: {e}")
                _ctx_file.unlink()
        elif PROMPT_HINT:
            print(f"[FIX] No saved context found, but --prompt will be included in the fix context.")
        if DEEP_ANALYZE:
            print(f"\n[DEEP ANALYZE] Opening interactive shell for {spec.stem}...")
            manager.run_deep_analyze_shell(package_name=spec.stem, ollama=ollama, full_context=full_context, project_mode=PROJECT_MODE, debug=DEBUG, deep_analyze_prompt=skill_manager.get_deep_analyze_prompt())
            print("[DEEP ANALYZE] Shell exited. Terminating pbuild and releasing build root...")
            time.sleep(3)
        while fix_attempt < MAX_ATTEMPTS:
            fix_attempt += 1
            attempt_label = fix_attempt if not unlimited else "∞"
            print(f"\n[FIX MODE] Attempt {attempt_label}/{MAX_ATTEMPTS if not unlimited else '∞'} — Diagnosing build failure...")
            build_out_lower = current_build_out.lower()
            if "unresolvable" in build_out_lower or "nothing provides" in build_out_lower:
                print("[DIAG] Missing build dependencies detected.")
                error_context = current_build_out
            else:
                has_log, log_content = manager.get_build_log(package_name=spec.stem)
                if has_log:
                    print("[DIAG] Build log found. Analyzing...")
                    _err_ctx = _extract_error_context(log_content)
                    error_context = f"Build output:\n{current_build_out[:2000]}"
                    if _err_ctx:
                        error_context += f"\n\n{_err_ctx}"
                    error_context += f"\n\nBuild log:\n{log_content}"
                else:
                    print(f"[DIAG] Warning: {log_content}")
                    # No log found — check unresolvable in build output, otherwise abort (single-mode only)
                    if not PROJECT_MODE and not ("unresolvable" in build_out_lower or "nothing provides" in build_out_lower):
                        print("[BUG] No build log and no unresolvable deps detected. This is likely a bug in pbuild-ai or pbuild. Aborting.")
                        sys.exit(1)
                    error_context = current_build_out
            _files_failure = any(p in build_out_lower for p in (
                "file not found", "unable to find", "unpackaged file",
                "directories not owned by a package",
            ))
            if _files_failure:
                _spec_content_now = manager.read_file_safe(spec)
                _tarball_hint = _extract_source_tarball_hint(_spec_content_now, spec, WORKSPACE_DIR)
                if _tarball_hint:
                    error_context = _tarball_hint + error_context
            if error_context == _prev_error_context and _prev_error_analysis:
                print("[FIX] Error unchanged, reusing previous analysis.")
                error_analysis = _prev_error_analysis
            else:
                _fixes_ctx = _build_attempted_fixes_context()
                error_analysis = ollama.analyze(error_prompt, _fixes_ctx + error_context, full_context)
                _prev_error_context = error_context
                _prev_error_analysis = error_analysis
            _latest_analysis = error_analysis
            print(f"\n{_color(AI_COLOR, '--- OLLAMA ERROR ANALYSIS ---')}\n{error_analysis}\n{_color(AI_COLOR, '-----------------------------')}\n")
            ollama._write_analysis_file(error_analysis)
            # Auto-trigger deep-analyze if Ollama requests it and we aren't already in that mode
            if "[DEEP_ANALYZE]" in error_analysis and not DEEP_ANALYZE:
                print("\n[DEEP ANALYZE] Ollama requested interactive investigation. Opening shell...")
                manager.run_deep_analyze_shell(package_name=spec.stem, ollama=ollama, full_context=full_context, project_mode=PROJECT_MODE, debug=DEBUG, deep_analyze_prompt=skill_manager.get_deep_analyze_prompt())
                time.sleep(3)
                print("[DEEP ANALYZE] Shell exited. Re-analyzing with collected data...")
                deep_context = f"{full_context}\n\n--- Deep investigation data ---\n{manager.deep_exploration[-20000:]}"
                _fixes_ctx = _build_attempted_fixes_context()
                error_analysis = ollama.analyze(error_prompt, _fixes_ctx + error_context, deep_context)
                error_analysis = error_analysis.replace("[DEEP_ANALYZE]", "").strip()
                _latest_analysis = error_analysis
                print(f"\n{_color(AI_COLOR, '--- OLLAMA ERROR ANALYSIS (after deep investigation) ---')}\n{error_analysis}\n{_color(AI_COLOR, '-----------------------------')}\n")
                ollama._write_analysis_file(error_analysis)
            if _is_environment_error(current_build_out):
                print(f"[RETRY] Environment/VM issue detected. Retrying with --clean...")
                if PROJECT_MODE:
                    _retry_ok, _retry_out = manager.run_project_build(package_name, stream_output=SHOW_BUILDLOG, force_clean=True)
                else:
                    _retry_ok, _retry_out = manager.run_orphan_build(stream_output=SHOW_BUILDLOG, force_clean=True)
                if _retry_ok:
                    print(f"\n[OK] Build succeeded after --clean retry.")
                    build_success2 = True
                    break
                print(f"[RETRY] Clean build also failed. Re-analyzing with clean build output...")
                current_build_out = _retry_out
                continue
            if spec_files:
                build_suggested_dependency(error_analysis, spec_files, manager, ollama, full_context)
            print("[FIX MODE] Applying suggested changes via tool calls...")
            spec_content = manager.read_file_safe(spec)
            fix_context = full_context or 'No AGENTS.md'
            if PROMPT_HINT:
                fix_context = f"{fix_context}\n\n--- User Hint (prefer this over generic analysis) ---\n{PROMPT_HINT}"
            if messages is None:
                messages = [
                    {"role": "system", "content": f"""You are an RPM packager assistant. Fix build failures by using tools.

You MUST call one or more of these tools NOW to make changes:
- edit_file(path, old_string, new_string): targeted search-and-replace (PREFER this for small changes). Include enough surrounding lines (full target line + 1-2 lines context) so old_string matches EXACTLY ONE location.
- write_file(path, content): write a file (use only for large rewrites or new files)
- read_file(path, offset?, limit?): read a file (optionally read a portion via offset/limit in characters)
- read_file_from_archive(archive_path, file_path, offset?, limit?): read a file inside a tar/zip archive (optionally partial read)
- web_fetch(url): fetch an HTTPS URL
- git_command(command): run a git command
- run_tool_script(script_name, args): run a script from tool-scripts/

The spec file is already provided in the message below. Use read_file with the given path to read it locally. Do NOT use web_fetch to fetch spec files from any remote git hosting site (src.opensuse.org, src.fedoraproject.org, github.com, etc.).
When you need to inspect source files, use read_file_from_archive with the local source tarball. Do NOT use web_fetch to fetch source files from upstream websites.
Call the tools to make changes. You may need to read files first, then call edit_file or write_file.
Prefer edit_file for targeted changes — it replaces only the matching text and preserves all other lines. IMPORTANT: include enough surrounding lines so old_string matches ONLY ONE location.
IMPORTANT: write_file writes the ENTIRE file. You must include ALL lines.
PRESERVE EVERY LINE YOU ARE NOT CHANGING VERBATIM — do not add, remove, or modify anything beyond the specific fix.
Keep in mind that your changes need to be reviewed. So keep changes minimal unless stated otherwise.
Make all necessary changes now, then stop.
Make only ONE edit to each file. Do NOT rewrite or re-edit a file you have already modified in this session. Plan your change carefully, apply it once, then stop.

AGENTS.md instructions (follow these):
{fix_context}"""},
                 {"role": "user", "content": f"""The build for {spec.name} failed.
Package: {package_name}
Spec file path: {spec.relative_to(WORKSPACE_DIR)}

Error context:
{error_context[:5000]}

Error analysis (the specific fix was identified here):
{error_analysis[:2000]}

Current spec content:
{spec_content[:5000]}

Do NOT explain. Do NOT ask questions. Apply the fix using edit_file or write_file NOW.
IMPORTANT: Your edit_file/write_file calls must use the EXACT paths from your error analysis above — do not substitute different directories or files.
Prefer edit_file for targeted changes — it preserves all other lines.
IMPORTANT: write_file writes the ENTIRE file. Include EVERY line verbatim.
Keep changes minimal unless stated otherwise."""}
                ]
            else:
                # Refresh system prompt (cross-mode compat: loaded from --modify or old --fix)
                if messages and messages[0].get("role") == "system":
                    messages[0] = {"role": "system", "content": f"""You are an RPM packager assistant. Fix build failures by using tools.

You MUST call one or more of these tools NOW to make changes:
- edit_file(path, old_string, new_string): targeted search-and-replace (PREFER this for small changes). Include enough surrounding lines (full target line + 1-2 lines context) so old_string matches EXACTLY ONE location.
- write_file(path, content): write a file (use only for large rewrites or new files)
- read_file(path, offset?, limit?): read a file (optionally read a portion via offset/limit in characters)
- read_file_from_archive(archive_path, file_path, offset?, limit?): read a file inside a tar/zip archive (optionally partial read)
- web_fetch(url): fetch an HTTPS URL
- git_command(command): run a git command
- run_tool_script(script_name, args): run a script from tool-scripts/

The spec file is already provided in the message below. Use read_file with the given path to read it locally. Do NOT use web_fetch to fetch spec files from any remote git hosting site (src.opensuse.org, src.fedoraproject.org, github.com, etc.).
When you need to inspect source files, use read_file_from_archive with the local source tarball. Do NOT use web_fetch to fetch source files from upstream websites.
Call the tools to make changes. You may need to read files first, then call edit_file or write_file.
Prefer edit_file for targeted changes — it replaces only the matching text and preserves all other lines. IMPORTANT: include enough surrounding lines so old_string matches ONLY ONE location.
IMPORTANT: write_file writes the ENTIRE file. You must include ALL lines.
PRESERVE EVERY LINE YOU ARE NOT CHANGING VERBATIM — do not add, remove, or modify anything beyond the specific fix.
Keep in mind that your changes need to be reviewed. So keep changes minimal unless stated otherwise.
Make all necessary changes now, then stop.
Make only ONE edit to each file. Do NOT rewrite or re-edit a file you have already modified in this session. Plan your change carefully, apply it once, then stop.

AGENTS.md instructions (follow these):
{fix_context}"""}
                messages.append({"role": "assistant", "content": (error_analysis or "")[:2000]})
                messages.append({"role": "user", "content": f"""The previous fix attempt did not resolve the build for {package_name}. Here is what was changed:
{_prev_diff_summary}

Here is the new error context:

{error_context[:3000]}

{_build_attempted_fixes_context()}Consult the skill rules (OPENSUSE.md / Build & Packaging Rules) in the system prompt for the exact fix pattern — the solution is almost certainly described there. Do NOT repeat any fix listed above — it already failed. Apply a DIFFERENT fix using write_file now."""})
                MAX_HISTORY = 40
                if len(messages) > MAX_HISTORY:
                    kept = [messages[0]] + messages[-(MAX_HISTORY - 1):]
                    # Strip read_file content from older tool messages, keep edit/write intact
                    for i in range(1, len(kept)):
                        if kept[i].get("role") == "tool" and kept[i].get("name") == "read_file":
                            content = kept[i].get("content", "")
                            if len(content) > 200:
                                kept[i]["content"] = content[:100] + f"\n... (truncated, {len(content)} bytes) ...\n" + content[-50:]
                    messages = kept
            tool_results = ollama.call_with_tools(messages, TOOLS, manager, WORKSPACE_DIR, ALLOW_TOOL_SCRIPTS, interactive=INTERACTIVE, max_rounds=ctx.max_rounds)
            if isinstance(tool_results, str):
                print(f"[FIX ERROR] {tool_results}")
            elif tool_results:
                for r in tool_results:
                    if r.startswith("read_file: ") and not r.startswith("read_file: Error"):
                        line_count = r.count('\n')
                        display = f"read_file: ({line_count} lines)"
                    elif r.startswith(("[Fetched ", "web_fetch: [Fetched ")):
                        display = r.split("\n", 1)[0]
                    elif r.startswith("list_files: "):
                        continue
                    else:
                        display = r[:500] + "..." if len(r) > 500 else r
                    print(f"[FIX] {display}")
                relocate_patches(tool_results, spec)
                if not any(r.startswith(("edit_file: OK: Edited", "write_file: OK: Wrote", "remove_file: OK: Removed", "rename_file: OK: Renamed")) for r in tool_results):
                    print("[FIX] Tool calls were all read-only. Trying rewrite from analysis instead...")
                    tool_results = None
            if not tool_results:
                    print("[FIX] No tool calls received. Asking Ollama to rewrite the spec file...")

                    def try_rewrite():
                        # Extract the explicit fix suggestion from the analysis
                        _fix_snippet = ""
                        _fix_match = re.search(r'\*\*Fix:\*\*\s*(.*?)(?:\n```|\n\n---|\Z)', error_analysis, re.DOTALL)
                        if _fix_match:
                            _fix_snippet = _fix_match.group(1).strip()
                        prompt = f"""The build for {spec.name} failed.

{_build_attempted_fixes_context()}Error (from analysis):
{error_analysis[:2000]}

Current spec:
{spec_content[:5000]}

The analysis identified the fix as:
{_fix_snippet or '(see error analysis above)'}

Apply this exact fix. Your output must be ONLY the complete raw spec file content.
- No markdown formatting
- No code fences
- No explanations, no introductory text, no summary
- Start with the first line of the spec (typically Name: or # or %)
- Output the COMPLETE spec, not just the changed parts
- Just raw spec content and nothing else
- Do NOT repeat any fix listed in the previously attempted fixes above — they all failed."""
                        result = ollama.analyze("You are an RPM spec expert.", prompt, full_context)
                        if not result:
                            return None
                        extracted = extract_spec(result)
                        if extracted and len(extracted) > 50 and any(l.strip().startswith(("Name:", "Summary:", "BuildRequires:")) for l in extracted.split("\n")):
                            return extracted
                        if extracted and any(l.strip().startswith(("Name:", "%")) for l in extracted.split("\n")):
                            return extracted
                        print(f"[FIX] Spec output doesn't look like a spec file. Content preview: {extracted[:200].replace(chr(10), ' | ')}", flush=True)
                        return None

                    spec_fix = try_rewrite()
                    if spec_fix:
                        show_diff(spec_content, spec_fix + "\n", spec)
                        try:
                            with open(spec, 'w', encoding='utf-8') as f:
                                f.write(spec_fix + "\n")
                            print(f"[FIX] Rewrote {spec.name} with the fix.", flush=True)
                        except Exception as ex:
                            print(f"[FIX] Failed to write spec: {ex}")
                            spec_fix = None

                    if not spec_fix:
                        print("[FIX] Spec rewrite failed. Parsing analysis for RPM tag changes...", flush=True)
                        rpm_tags = find_rpm_tags(error_analysis)
                        if rpm_tags:
                            print(f"[FIX] Found RPM tags in analysis: {rpm_tags}", flush=True)
                            spec_lines = spec_content.split("\n")
                            spec_lines, modified = apply_spec_insertions(spec_lines, rpm_tags)
                            if modified:
                                new_content = "\n".join(spec_lines)
                                show_diff(spec_content, new_content, spec)
                                with open(spec, 'w', encoding='utf-8') as f:
                                    f.write(new_content + "\n" if not new_content.endswith("\n") else new_content)
                                print(f"[FIX] Directly patched {spec.name} from analysis hints.", flush=True)
                                spec_fix = new_content
                            else:
                                print(f"[FIX] All tags already present in spec, no changes needed.", flush=True)
                        else:
                            print(f"[FIX] No RPM tags found in analysis. Analysis preview: {error_analysis[:300].replace(chr(10), ' | ')}", flush=True)

                    if not spec_fix:
                        _applied_direct = _apply_analysis_fix_patterns(spec, spec_content, error_analysis)
                        if _applied_direct:
                            print(f"[FIX] Applied fix patterns directly from analysis.", flush=True)
                            spec_fix = _applied_direct

                    if not spec_fix:
                        print("[FIX] Trying to parse change instructions from analysis...", flush=True)
                        spec_lines = spec_content.split("\n")
                        modified = False
                        lower_analysis = error_analysis.lower()
                        for m in re.finditer(r'(?:add|insert)\s*(?:line|tag|dependency)?\s*(?::)?\s*(BuildRequires|Requires|Recommends|Suggests|Supplements|Conflicts|Obsoletes|Provides)\s*:.+', lower_analysis):
                            full_line = m.group(0).strip()
                            tag, val = full_line.split(":", 1)
                            tag = tag.strip().title()
                            val = val.strip()
                            full_line = f"{tag}: {val}" if not val.startswith(":") else f"{tag}:{val}"
                            if full_line.strip() not in [l.strip() for l in spec_lines]:
                                spec_lines.insert(0, full_line)
                                modified = True
                                print(f"[FIX] From text instruction: {full_line}", flush=True)
                        if modified:
                            new_content = "\n".join(spec_lines)
                            show_diff(spec_content, new_content, spec)
                            with open(spec, 'w', encoding='utf-8') as f:
                                f.write(new_content + "\n" if not new_content.endswith("\n") else new_content)
                            print(f"[FIX] Patched {spec.name} from text instructions.", flush=True)

                    if not spec_fix:
                        print("[FIX] Could not apply any fix. Spec unchanged.", flush=True)

            current_spec = manager.read_file_safe(spec)
            changed = current_spec != spec_content
            if changed:
                diff_lines = list(difflib.unified_diff(
                    _prev_spec.splitlines(keepends=True),
                    current_spec.splitlines(keepends=True),
                    fromfile="before",
                    tofile="after",
                    lineterm=""
                ))
                # Keep max 20 lines of diff to avoid bloat
                _prev_diff_summary = "".join(diff_lines[:20])
                if len(diff_lines) > 20:
                    _prev_diff_summary += f"\n... ({len(diff_lines) - 20} more lines)"
                _prev_spec = current_spec
                # Record this fix attempt for anti-loop awareness
                _attempted_fixes.append((fix_attempt, _prev_diff_summary, (error_context or current_build_out or "")[:500]))
                # Circular spec detection: check if new spec matches a previous version
                _spec_hash = _hashlib.md5(current_spec.encode()).hexdigest()
                _match_count = sum(1 for _, h in _spec_version_hashes if h == _spec_hash)
                if _match_count > 0:
                    _prev_att = next(att for att, h in _spec_version_hashes if h == _spec_hash)
                    print(f"[FIX WARNING] Circular fix detected: spec content matches attempt {_prev_att}. The LLM is reverting a previous change.", flush=True)
                    if _match_count >= 2:
                        print(f"[FIX ERROR] Spec has cycled back to a previous version {_match_count} times. Aborting to prevent infinite loop.", flush=True)
                        sys.exit(1)
                _spec_version_hashes.append((fix_attempt, _spec_hash))
            if not changed and not tool_results:
                if messages:
                    _ctx_file.write_text(json.dumps({"version": 1, "mode": "fix", "spec_path": str(spec.relative_to(WORKSPACE_DIR)), "package_name": package_name, "messages": messages, "spec_content": spec_content, "error_context": current_build_out, "error_analysis": _latest_analysis, "timestamp": time.time()}, indent=2))
                    print(f"[FIX] Saved conversation context to {_ctx_file.name} for restart.")
                print("[FIX ERROR] No source changes were made. Aborting rebuild.", flush=True)
                if exit_on_no_changes:
                    sys.exit(1)
                break

            if changed and _is_comment_only_change(spec_content, current_spec):
                print("[INFO] Only comment/whitespace changes — skipping rebuild.")
                if _ctx_file.exists():
                    _ctx_file.unlink()
                break

            print("[FIX MODE] Re-building to verify...", flush=True)
            build_success2, build_out2 = rebuild_func(package_name)
            _build_out_lower = build_out2.lower() if build_out2 else ""
            if build_success2:
                print("[FIX MODE] Build was successful")
            elif "unresolvable" in _build_out_lower or "nothing provides" in _build_out_lower:
                print("[FIX MODE] Build was unresolvable")
            else:
                print("[FIX MODE] Build was failed")

            if build_success2:
                print(f"\n[OK] Fix verified: Build for {spec.name} succeeded after applying changes.")
                if DEEP_ANALYZE:
                    export_deep_fix_patch(WORKSPACE_DIR, spec, spec.stem)
                if _ctx_file.exists():
                    _ctx_file.unlink()
                    print(f"[FIX] Removed saved context ({_ctx_file.name}) after successful build.")
                break
            else:
                print(f"\n[WARN] Fix attempt {fix_attempt} still failing.")
                if _is_environment_error(build_out2):
                    print(f"[RETRY] Environment/VM issue detected (attempt {fix_attempt}). Retrying with --clean...")
                    _retry_ok, _retry_out = rebuild_func(package_name, force_clean=True)
                    if _retry_ok:
                        print(f"\n[OK] Build succeeded after --clean retry.")
                        build_success2 = True
                        break
                    print(f"[RETRY] Clean build also failed. Falling through to re-analysis...")
                    build_out2 = _retry_out
                _fixes_ctx = _build_attempted_fixes_context()
                error_analysis2 = ollama.analyze(error_prompt, _fixes_ctx + build_out2, full_context)
                _latest_analysis = error_analysis2
                print(f"\n{_color(AI_COLOR, f'--- OLLAMA ERROR ANALYSIS (attempt {fix_attempt}) ---')}\n{error_analysis2}\n{_color(AI_COLOR, '------------------------------------------')}\n")
                ollama._write_analysis_file(error_analysis2)
                current_build_out = build_out2

        if not build_success2:
            if messages:
                _ctx_file.write_text(json.dumps({"version": 1, "mode": "fix", "spec_path": str(spec.relative_to(WORKSPACE_DIR)), "package_name": package_name, "messages": messages, "spec_content": spec_content, "error_context": current_build_out, "error_analysis": _latest_analysis, "timestamp": time.time()}, indent=2))
                print(f"[FIX] Saved conversation context to {_ctx_file.name} for restart.")
            label = MAX_ATTEMPTS if not unlimited else "unlimited"
            print(f"[FIX ERROR] All {label} fix attempts exhausted. Build still failing.")
            sys.exit(1)

        return build_success2

    def run_project_fix_loop(spec_files, manager, ollama, skill_manager, base_fc):
        """Run pbuild --abort-on-fail, detect failure, fix, and restart.
        Returns True if all packages built successfully.
        """
        spec_map = {s.stem: s for s in spec_files}
        max_attempts = 50

        for attempt in range(1, max_attempts + 1):
            print(f"\n[PROJECT BUILD] Full project build (attempt {attempt}/{max_attempts})...")
            all_success, all_out = manager.run_full_project_build(stream_output=SHOW_BUILDLOG)
            if all_success:
                print("\n[OK] All packages built successfully.")
                return True

            if attempt >= max_attempts:
                print(f"\n[ERROR] All {max_attempts} full build attempts exhausted.")
                return False

            failed_pkg = parse_failed_package(all_out)
            if not failed_pkg or failed_pkg not in spec_map:
                print(f"\n[ERROR] Could not identify failing package. Build output:\n{all_out[:2000]}")
                return False

            spec = spec_map[failed_pkg]
            print(f"\n[PROJECT BUILD] Package '{failed_pkg}' failed. Running fix loop...")
            ollama.reset_context()
            ollama.reset_stats()

            skills = skill_manager.get_skills_for(spec.name, manager.read_file_safe(spec), prompt=MODIFY_PROMPT)
            if skills:
                for s in skills:
                    print(f"[INFO] Using skill profile: {s.__name__}")
                error_prompt_parts = []
                fix_funcs = []
                skill_ctx_parts = []
                for s in skills:
                    ep = getattr(s, 'OLLAMA_ERROR_PROMPT', '')
                    if ep:
                        error_prompt_parts.append(ep)
                    ff = getattr(s, 'fix_content', None)
                    if ff:
                        fix_funcs.append(ff)
                    sc = getattr(s, 'OLLAMA_SPEC_PROMPT', '')
                    if sc:
                        skill_ctx_parts.append(f"--- Skill: {s.__name__} ---\n{sc}")
                error_prompt = "\n\n".join(error_prompt_parts) if error_prompt_parts else DEFAULT_ERROR_PROMPT
                if fix_funcs:
                    def chained_fix(content):
                        for f in fix_funcs:
                            content = f(content)
                        return content
                    fix_func = chained_fix
                else:
                    fix_func = default_fix
                local_fc = f"{base_fc}\n\n" + "\n\n".join(skill_ctx_parts) if skill_ctx_parts else base_fc
            else:
                error_prompt = DEFAULT_ERROR_PROMPT
                fix_func = default_fix
                local_fc = base_fc

            if PROMPT_HINT:
                local_fc = f"{local_fc}\n\n--- User Hint (prefer this over generic analysis) ---\n{PROMPT_HINT}"

            # Enrich error_prompt with gitexplorer API results
            if all_out:
                error_prompt = _inject_gitexplorer_results(error_prompt, all_out)

            if not manager.build_phase_reached(package_name=failed_pkg):
                print(f"[PROJECT BUILD] Build did not reach build phase. Retrying {failed_pkg} with --clean...")
                build_ok, build_out2 = manager.run_project_build(failed_pkg, stream_output=SHOW_BUILDLOG, force_clean=True)
                if build_ok:
                    print(f"\n[OK] {failed_pkg} succeeded after --clean retry.")
                    continue
                else:
                    all_out = build_out2
                    print(f"[PROJECT BUILD] Clean build also failed. Proceeding with fix loop.")

            if not run_fix_loop(spec, failed_pkg, all_out, error_prompt,
                lambda p, force_clean=False: manager.run_project_build(p, stream_output=SHOW_BUILDLOG, force_clean=force_clean)):
                return False

        return True

    try:
        # Get project packages list (used for building with correct relative paths)
        packages = list_packages(WORKSPACE_DIR) if PROJECT_MODE else []

        spec_files = [f for f in Path(WORKSPACE_DIR).rglob("*.spec") if manager._is_safe_path(f)]

        # For project mode, filter to only manifest packages; skip others
        if PROJECT_MODE and packages:
            spec_files = [s for s in spec_files if any(p[0] == s.stem for p in packages)]

        # Deduplicate by stem — each package built once
        seen_stems = set()
        deduped = []
        for s in spec_files:
            if s.stem not in seen_stems:
                seen_stems.add(s.stem)
                deduped.append(s)
        spec_files = deduped

        # Always parse AGENTS.md for build order hints (independent of --allow-tool-scripts)
        apply_build_order(spec_files)

        # When a specific package is requested on CLI, filter to only that package
        if PACKAGE_FILTER:
            spec_files = [s for s in spec_files if s.stem == PACKAGE_FILTER]
            if not spec_files:
                print(f"[ERROR] Package '{PACKAGE_FILTER}' not found in spec files.")
                sys.exit(1)

        # Execute pre-build scripts from tool-scripts/ (only with --allow-tool-scripts)
        if not PACKAGE_FILTER:
            run_prebuild_scripts(spec_files)
        ctx.spec_files = spec_files

        # --changelog mode: standalone changelog entry for current version
        if args.changelog:
            _email = EMAIL if EMAIL else "<Your Name> <your@email>"
            for _spec in spec_files:
                _v_match = re.search(r'^Version:\s*(\S+)', manager.read_file_safe(_spec), re.M)
                if not _v_match:
                    print(f"[CHANGELOG] Could not determine version from {_spec.name}, skipping.")
                    continue
                _changes_path = _spec.parent / (_spec.stem + '.changes')
                if write_changelog_entry(_changes_path, "", _v_match.group(1), _email):
                    print(f"[CHANGELOG] Added entry for {_spec.stem} ({_v_match.group(1)}).")
                else:
                    print(f"[CHANGELOG] Entry for {_spec.stem} ({_v_match.group(1)}) already exists, skipped.")
            sys.exit(0)

        # --generate mode: create a new package from scratch
        if ctx.generate_prompt:
            run_generate_mode(ctx)
            if not FIX_MODE:
                ollama.print_stats(manager=manager, program_start=ctx.program_start, skill_manager=skill_manager)
                sys.exit(0)
            # Re-scan for spec files created by generate mode before entering fix phase
            spec_files = [f for f in Path(WORKSPACE_DIR).rglob("*.spec") if manager._is_safe_path(f)]
            ctx.spec_files = spec_files

        # --modify mode: hand sources + prompt to Ollama, apply changes locally
        if ctx.modify_prompt:
            run_modify_mode(ctx)
            if not FIX_MODE:
                ollama.print_stats(manager=manager, program_start=ctx.program_start, skill_manager=skill_manager)
                sys.exit(0)  # --modify without --fix: only modifies sources, does not build

        # Phase 1: Update pass — update all packages first without building
        updated_packages = set()
        if UPDATE_VERSION is not None:
            base_full_context = full_context
            email_author = EMAIL if EMAIL else "<Your Name> <your@email>"
            spec_originals = {spec: manager.read_file_safe(spec) for spec in spec_files}
            for spec in spec_files:
                ollama.reset_context()
                ollama.reset_stats()

                skills = skill_manager.get_skills_for(spec.name, manager.read_file_safe(spec), prompt=MODIFY_PROMPT)
                if skills:
                    for s in skills:
                        print(f"[INFO] Using skill profile: {s.__name__}")
                    skill_ctx_parts = []
                    for s in skills:
                        skill_ctx = getattr(s, 'OLLAMA_SPEC_PROMPT', '')
                        if skill_ctx:
                            skill_ctx_parts.append(f"--- Skill: {s.__name__} ---\n{skill_ctx}")
                    if skill_ctx_parts:
                        full_context = f"{base_full_context}\n\n" + "\n\n".join(skill_ctx_parts)
                else:
                    print("[INFO] No specific skill found. Using default profile.")

                spec_before_update = manager.read_file_safe(spec)
                if spec in updated_packages:
                    current = manager.read_file_safe(spec)
                    if current == spec_originals.get(spec):
                        print(f"[UPDATE] {spec.name} hinted for rebuild (no source changes).")
                        continue
                    print(f"[UPDATE] {spec.name} already updated by AI. Skipping AI phase...")
                    spec_before_update = spec_originals[spec]
                    target_version = UPDATE_VERSION
                    for line in current.split('\n'):
                        m = re.match(r'^Version:\s*(\S+)', line)
                        if m:
                            target_version = m.group(1)
                            break
                    if not target_version:
                        target_version = 'latest'
                    _changes_file = spec.parent / (spec.stem + '.changes')
                    _changes_before = manager.read_file_safe(_changes_file) if _changes_file.exists() else None
                else:
                    target_version = UPDATE_VERSION
                    _changes_before = None
                if not target_version:
                    print(f"\n[UPDATE] Researching latest upstream version for {spec.name}...")
                    spec_content = manager.read_file_safe(spec)

                    # Pre-check: try GitHub API directly before involving Ollama
                    _current_v_match = re.search(r'^Version:\s*(\S+)', spec_before_update, re.M)
                    _current_version = _current_v_match.group(1) if _current_v_match else None
                    _source_url = None
                    for _line in spec_content.split('\n'):
                        _m = re.match(r'^Source\d*:\s*(.+)', _line, re.I)
                        if _m:
                            _source_url = _m.group(1).strip()
                            break
                    _github_api_url = None
                    if _source_url and 'github.com' in _source_url:
                        _gh_m = re.search(r'github\.com[/:]([^/]+/[^/]+?)(?:\.git|/|$)', _source_url)
                        if _gh_m:
                            _repo = _gh_m.group(1).rstrip('/')
                            _github_api_url = f'https://api.github.com/repos/{_repo}/releases/latest'
                    if _github_api_url and _current_version:
                        try:
                            _req = urllib.request.Request(_github_api_url, headers={"User-Agent": "pbuild-ai/1.0", "Accept": "application/vnd.github.v3+json"})
                            _resp = urllib.request.urlopen(_req, timeout=10)
                            _data = json.loads(_resp.read())
                            _tag = _data.get('tag_name', '')
                            _latest = _tag.lstrip('v') if _tag else ''
                            if _latest and _latest == _current_version:
                                print(f"[UPDATE] {spec.name} already at latest version {_current_version}. Skipping.")
                                continue
                        except Exception as _e:
                            _status = getattr(_e, 'code', None) or getattr(_e, 'status', None) or type(_e).__name__
                            print(f"[UPDATE] GitHub API check ({_github_api_url}) failed: {_status} — falling back to AI research.")

                    research_system_content = VERSION_RESEARCH_SYSTEM_PROMPT.format(
                        spec=spec,
                        spec_content=spec_content,
                        full_context=full_context,
                        changelog_prompt=CHANGELOG_PROMPT,
                    )
                    research_messages = [
                        {"role": "system", "content": research_system_content},
                    ]
                    _changes_file = spec.parent / (spec.stem + '.changes')
                    _changes_before = manager.read_file_safe(_changes_file) if _changes_file.exists() else None
                    results = ollama.call_with_tools(research_messages, TOOLS, manager, WORKSPACE_DIR, ALLOW_TOOL_SCRIPTS, interactive=INTERACTIVE, max_rounds=ctx.max_rounds)
                    if results:
                        for r in results:
                            if r.startswith("web_fetch: [Fetched ") or r.startswith("read_file: "):
                                display = r.split("\n", 1)[0]
                            elif DEBUG:
                                display = r
                            else:
                                display = r[:500] + "..." if len(r) > 500 else r
                            print(f"[UPDATE] {display}")
                    _check_update_hints(results, research_messages, spec_files, spec_originals, updated_packages, manager)
                    spec_content = manager.read_file_safe(spec)
                    for line in spec_content.split('\n'):
                        m = re.match(r'^Version:\s*(\S+)', line)
                        if m:
                            target_version = m.group(1)
                            print(f"[UPDATE] Updated to version {target_version}")
                            break
                    if not target_version:
                        print("[UPDATE] Could not determine latest version.")
                        target_version = 'latest'
                else:
                    print(f"\n[UPDATE] Updating {spec.name} to {target_version}...")
                    update_prompt = VERSION_UPDATE_PROMPT.format(
                        target_version=target_version,
                        full_context=full_context,
                        changelog_prompt=CHANGELOG_PROMPT,
                    )
                    messages = [
                        {"role": "system", "content": update_prompt},
                        {"role": "user", "content": f"Update this spec file to version {target_version}:\n\n{manager.read_file_safe(spec)}"}
                    ]
                    _changes_file = spec.parent / (spec.stem + '.changes')
                    _changes_before = manager.read_file_safe(_changes_file) if _changes_file.exists() else None
                    results = ollama.call_with_tools(messages, TOOLS, manager, WORKSPACE_DIR, ALLOW_TOOL_SCRIPTS, interactive=INTERACTIVE, max_rounds=ctx.max_rounds)
                    if results:
                        for r in results:
                            if r.startswith("web_fetch: [Fetched ") or r.startswith("read_file: "):
                                display = r.split("\n", 1)[0]
                            elif DEBUG:
                                display = r
                            else:
                                display = r[:500] + "..." if len(r) > 500 else r
                            print(f"[UPDATE] {display}")
                    else:
                        print("[UPDATE] No changes made.")
                    _check_update_hints(results, messages, spec_files, spec_originals, updated_packages, manager)

                # If version didn't change, restore any corrupted files and skip download
                _old_v = re.search(r'^Version:\s*(\S+)', spec_before_update, re.M)
                if _old_v and target_version == _old_v.group(1):
                    _changes_file = spec.parent / (spec.stem + '.changes')
                    if _changes_file.exists() and _changes_before is not None:
                        _current_changes = manager.read_file_safe(_changes_file)
                        if _current_changes != _changes_before:
                            print(f"[UPDATE] Version unchanged, restoring changes file.")
                            _changes_file.write_text(_changes_before)
                    target_version = None  # prevent download and update tracking

                # Post-format fix: repair mangled RemoteAsset/CreateArchive lines
                _spec_current = manager.read_file_safe(spec)
                _fixed = False
                # Case 1: #!RemoteAsset inline on a Source: line — extract to its own line before Source:
                _m_src = re.search(r'^(Source\d*:\s*)(#!RemoteAsset:[^\n]+\s*)(.*)$', _spec_current, re.M)
                if _m_src:
                    _replacement = f'  {_m_src.group(2).strip()}\n{_m_src.group(1)}{_m_src.group(3).strip()}'
                    _spec_current = _spec_current.replace(_m_src.group(0), _replacement)
                    _fixed = True
                # Case 2: merged onto one line — "!#!CreateArchive" or "#!RemoteAsset: ... #!CreateArchive"
                _m_merged = re.search(r'(#!RemoteAsset:[^\n]+)\s+#?!?CreateArchive[^\n]*', _spec_current)
                if _m_merged:
                    _spec_current = _spec_current.replace(_m_merged.group(0), _m_merged.group(1))
                    _fixed = True
                # Case 3: #!CreateArchive on a continuation line or after Source:
                _m_ca = re.search(r'^(\s+.*)?#!CreateArchive[^\n]*', _spec_current, re.M)
                if _m_ca and not re.search(r'^#!CreateArchive$', _m_ca.group(0), re.M):
                    _spec_current = _spec_current.replace(_m_ca.group(0), '')
                    _fixed = True
                # Case 4: Source: renamed to Source0: when RemoteAsset is present
                if '#!RemoteAsset:' in _spec_current and 'Source0:' in _spec_current and 'Source:' not in _spec_current:
                    _spec_current = _spec_current.replace('Source0:', 'Source:')
                    _fixed = True
                # Ensure #!CreateArchive exists after #!RemoteAsset:
                if '#!RemoteAsset:' in _spec_current and '#!CreateArchive' not in _spec_current:
                    _spec_current = re.sub(
                        r'(#!RemoteAsset:[^\n]+)\n',
                        r'\1\n#!CreateArchive\n',
                        _spec_current,
                        count=1
                    )
                    _fixed = True
                if _fixed:
                    spec.write_text(_spec_current)
                    print("[UPDATE] Fixed RemoteAsset/CreateArchive formatting.")

                # Deterministic source tarball download (not relying on Ollama tool calls)
                if target_version and target_version not in ('latest',):
                    _dl_failed = False
                    try:
                        _spec_content = manager.read_file_safe(spec)
                        _skip_dl = False
                        if '#!CreateArchive' in _spec_content:
                            print(f"[UPDATE] #!CreateArchive found — source from git. Skipping download.")
                            _skip_dl = True
                        elif re.search(r'#!RemoteAsset:\s+(?!git\+)', _spec_content):
                            print(f"[UPDATE] RemoteAsset (non-git) handles source. Skipping download.")
                            _skip_dl = True
                        if not _skip_dl and not (spec.parent / "_service").exists():
                            # Resolve Source URL via Build::Rpm (proper macro expansion)
                            _perl_script = Path(__file__).parent / 'query_source_url.pl'
                            _source_url = None
                            try:
                                _r = subprocess.run(['perl', str(_perl_script), str(spec)],
                                    capture_output=True, text=True, timeout=30)
                                if _r.returncode == 0:
                                    for _line in _r.stdout.strip().split('\n'):
                                        if ': ' in _line:
                                            _val = _line.split(': ', 1)[1]
                                            if not _val.startswith('git+') and _source_url is None:
                                                _source_url = _val
                                            elif _val.startswith('git+') and _source_url is None:
                                                _source_url = _val.replace('git+', '', 1)
                            except Exception:
                                pass
                            # Fallback: regex-based Source parsing (macros unexpanded)
                            if not _source_url:
                                for _line in _spec_content.split('\n'):
                                    _m = re.match(r'^Source\d*:\s*(.+)', _line, re.I)
                                    if _m:
                                        _source_url = _m.group(1).strip()
                                        break
                                # Best-effort manual macro expansion for fallback
                                _macros = {}
                                for _kv in re.finditer(r'^(Name|Version):\s*(\S+)', _spec_content, re.M):
                                    _macros[_kv.group(1).lower()] = _kv.group(2)
                                if _macros:
                                    _expanded = _source_url
                                    for _key, _val in _macros.items():
                                        _expanded = _expanded.replace(f'%{{{_key}}}', _val)
                                    _old_v = re.search(r'^Version:\s*(\S+)', spec_before_update, re.M)
                                    if _old_v and _old_v.group(1) != target_version:
                                        _expanded = _expanded.replace(_old_v.group(1), target_version)
                                    _source_url = _expanded
                            if _source_url:
                                from urllib.parse import urlparse
                                _fname = Path(urlparse(_source_url).path).name or Path(_source_url).name
                                _rel = Path(spec).relative_to(Path(WORKSPACE_DIR))
                                if _rel.parent != Path('.'):
                                    _fname = str(_rel.parent / _fname)
                                print(f"[UPDATE] Downloading {_fname}...")
                                _dl_ok = False
                                for _r in execute_tool_calls(
                                    [("download_file", {"url": _source_url, "filename": _fname})],
                                    manager, WORKSPACE_DIR, ALLOW_TOOL_SCRIPTS, interactive=INTERACTIVE, debug=DEBUG
                                ):
                                    _d = _r[:500] + "..." if len(_r) > 500 else _r
                                    print(f"[UPDATE] {_d}")
                                    if _r.startswith("OK"):
                                        _dl_ok = True
                                # Fallback: if GitHub releases URL 404'd, try archive URL pattern
                                if not _dl_ok and 'github.com' in _source_url and '/releases/download/' in _source_url:
                                    _gh_m = re.search(r'github\.com/([^/]+/[^/]+?)(?:/|$)', _source_url)
                                    if _gh_m:
                                        _alt_url = f"https://github.com/{_gh_m.group(1)}/archive/refs/tags/v{target_version}.tar.gz"
                                        _alt_fname = _fname.rsplit('.', 2)[0] + '.tar.gz' if _fname.endswith('.tar.bz2') else _fname
                                        print(f"[UPDATE] GitHub release URL failed, trying archive: {_alt_url}")
                                        for _r in execute_tool_calls(
                                            [("download_file", {"url": _alt_url, "filename": _alt_fname})],
                                            manager, WORKSPACE_DIR, ALLOW_TOOL_SCRIPTS, interactive=INTERACTIVE, debug=DEBUG
                                        ):
                                            _d = _r[:500] + "..." if len(_r) > 500 else _r
                                            print(f"[UPDATE] {_d}")
                                            if _r.startswith("OK"):
                                                _dl_ok = True
                                                _fname = _alt_fname
                                if not _dl_ok:
                                    _dl_failed = True
                                    print(f"[UPDATE] Source download failed — skipping build for {spec.name}.")
                                # Remove old source tarball only if new download succeeded
                                if _dl_ok:
                                    _old_source_url = None
                                    for _oline in spec_before_update.split('\n'):
                                        _om = re.match(r'^Source\d*:\s*(.+)', _oline, re.I)
                                        if _om:
                                            _old_source_url = _om.group(1).strip()
                                            break
                                    if _old_source_url:
                                        _macros = {}
                                        for _kv in re.finditer(r'^(Name|Version):\s*(\S+)', spec_before_update, re.M):
                                            _macros[_kv.group(1).lower()] = _kv.group(2)
                                        _old_expanded = _old_source_url
                                        for _key, _val in _macros.items():
                                            _old_expanded = _old_expanded.replace(f'%{{{_key}}}', _val)
                                        _old_fname = Path(urlparse(_old_expanded).path).name or Path(_old_expanded).name
                                        _old_rel = Path(spec).relative_to(Path(WORKSPACE_DIR))
                                        if _rel.parent != Path('.'):
                                            _old_fname = str(_old_rel.parent / _old_fname)
                                        _old_path = Path(WORKSPACE_DIR) / _old_fname
                                        _new_path = Path(WORKSPACE_DIR) / _fname
                                        if _old_path.exists() and _old_path != _new_path:
                                            _old_path.unlink()
                                            print(f"[UPDATE] Removed old source: {_old_fname}")
                                    # Remove _service file — tarball replaces obs_scm
                                    _svc = spec.parent / "_service"
                                    if _svc.exists():
                                        _svc.unlink()
                                        print(f"[UPDATE] Removed _service file (tarball replaces obs_scm).")
                    except Exception as e:
                        _dl_failed = True
                        print(f"[UPDATE] Source download failed: {e}")

                spec_after = manager.read_file_safe(spec)
                _new_v = re.search(r'^Version:\s*(\S+)', spec_after, re.M)
                _old_v = re.search(r'^Version:\s*(\S+)', spec_before_update, re.M)
                if spec_after != spec_before_update and _new_v and _old_v and _new_v.group(1) != _old_v.group(1):
                    # Deterministic changes file update if Ollama didn't handle it
                    _changes_file = spec.parent / (spec.stem + '.changes')
                    _changes_after = manager.read_file_safe(_changes_file) if _changes_file.exists() else ''
                    if _changes_after == (_changes_before or ''):
                        if write_changelog_entry(_changes_file, _old_v.group(1), _new_v.group(1), email_author):
                            print(f"[UPDATE] Added changelog entry for {_old_v.group(1)} -> {_new_v.group(1)}.")
                    if not _dl_failed:
                        updated_packages.add(spec)
                        print(f"[UPDATE] Updated {spec.name}.")
                elif spec_after != spec_before_update:
                    print(f"[UPDATE] No changes for {spec.name}.")

            if ctx.update_only:
                ollama.print_stats(manager=manager, program_start=ctx.program_start, skill_manager=skill_manager)
                if not updated_packages:
                    print("[UPDATE] No changes found. Exiting (--update-only).")
                else:
                    print(f"[UPDATE] Sources updated for {len(updated_packages)} packages. Exiting (--update-only, no build).")
                sys.exit(0)

            if not updated_packages:
                print("[UPDATE] No packages were updated. Exiting.")
                sys.exit(0)

            print(f"\n[BUILD] All packages updated. Starting build phase...\n")
            full_context = base_full_context

        # Dispatch build strategy
        if PROJECT_MODE and not PACKAGE_FILTER and FIX_MODE:
            # Project-wide abort-on-fail + fix loop (merged --all behavior)
            if not run_project_fix_loop(spec_files, manager, ollama, skill_manager, full_context):
                sys.exit(1)
        else:
            for spec in spec_files:
                ollama.reset_context()
                ollama.reset_stats()

                if UPDATE_VERSION is not None and spec not in updated_packages:
                    continue

                # 1. Determine skills
                skills = skill_manager.get_skills_for(spec.name, manager.read_file_safe(spec), prompt=MODIFY_PROMPT)
                if skills:
                    for s in skills:
                        print(f"[INFO] Using skill profile: {s.__name__}")
                    spec_prompt_parts = []
                    error_prompt_parts = []
                    fix_funcs = []
                    skill_ctx_parts = []
                    for s in skills:
                        sp = getattr(s, 'OLLAMA_SPEC_PROMPT', '')
                        if sp:
                            spec_prompt_parts.append(f"--- Skill: {s.__name__} ---\n{sp}")
                            skill_ctx_parts.append(f"--- Skill: {s.__name__} ---\n{sp}")
                        ep = getattr(s, 'OLLAMA_ERROR_PROMPT', '')
                        if ep:
                            error_prompt_parts.append(ep)
                        ff = getattr(s, 'fix_content', None)
                        if ff:
                            fix_funcs.append(ff)
                    spec_prompt = "\n\n".join(spec_prompt_parts) if spec_prompt_parts else DEFAULT_SPEC_PROMPT
                    error_prompt = "\n\n".join(error_prompt_parts) if error_prompt_parts else DEFAULT_ERROR_PROMPT
                    if fix_funcs:
                        def chained_fix(content):
                            for f in fix_funcs:
                                content = f(content)
                            return content
                        fix_func = chained_fix
                    else:
                        fix_func = default_fix
                    if skill_ctx_parts:
                        full_context = f"{full_context}\n\n" + "\n\n".join(skill_ctx_parts)
                else:
                    print("[INFO] No specific skill found. Using default profile.")
                    spec_prompt = DEFAULT_SPEC_PROMPT
                    error_prompt = DEFAULT_ERROR_PROMPT
                    fix_func = default_fix

                if UPDATE_VERSION is not None:
                    pass  # Update already done in Phase 1 — build directly
                else:
                    # Normal flow: spec analysis and skill-based fix
                    if not ctx.modify_prompt:
                        print(f"[AI] Analyzing Spec-file: {spec.name}...")
                        analysis_context = full_context
                        if PROMPT_HINT:
                            analysis_context = f"{analysis_context}\n\n--- User Hint (prefer this over generic analysis) ---\n{PROMPT_HINT}"
                        spec_analysis = ollama.analyze(spec_prompt, manager.read_file_safe(spec), analysis_context)
                        print(f"-> AI({ollama.model}) says:\n{spec_analysis}\n")
                        if not FIX_MODE or manager.has_prior_failed_build() or PROMPT_HINT:
                            manager.fix_file_content(spec, fix_func)

                # 4. Build guard: only run pbuild when --fix or --update is active
                error_prompt = _run_build_guard(
                    spec, manager, ollama, full_context, error_prompt, ctx,
                    ctx.program_start, run_fix_loop,
                )

        # Check build result on normal exit — last build attempt determines exit code
        if manager and manager.pbuild_calls > 0 and not manager.last_build_successful:
            print("[EXIT] Last build attempt failed. Exiting with code 1.")
            sys.exit(1)

        ollama.print_stats(manager=manager, program_start=ctx.program_start, skill_manager=skill_manager)
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            print(f"Script aborted: {e}", file=sys.stderr)
        except BlockingIOError:
            sys.stderr.write(f"Script aborted: {e}\n")
        sys.exit(2)

def main():
    """Entry point for pbuild-ai CLI."""
    import runpy
    runpy.run_path(__file__, run_name="__main__")
