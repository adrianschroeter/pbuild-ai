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
from pathlib import Path

if __name__ == "__main__" and not __package__:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pbuild_ai.manifest import list_packages
from pbuild_ai.diff_utils import show_diff
from pbuild_ai.tools import execute_tool_calls, build_tools_list
from pbuild_ai.skill_manager import SkillManager
from pbuild_ai.ollama_client import OllamaAnalyzer
from pbuild_ai.workspace import RpmSourceManager
from pbuild_ai.parsing import parse_agents_md_scripts, parse_failed_package, extract_spec, find_rpm_tags, apply_spec_insertions
from pbuild_ai.context import PbuildContext
from pbuild_ai.generate_mode import run_generate_mode
from pbuild_ai.modify_mode import run_modify_mode

# ==========================================
# Main Application Logic
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RPM packager helper")
    parser.add_argument("workspace_dir", help="Path to the workspace directory")
    parser.add_argument("package_name", nargs="?", default=None, help="Package name to focus on (only in project mode)")
    parser.add_argument("--fix", "-f", action="store_true", help="Apply suggested changes and run a test build to verify")
    parser.add_argument("--root", default=None, help="Root directory for pbuild (passed as --root to pbuild)")
    parser.add_argument("--show-buildlog", "-L", action="store_true", help="Show the pbuild build log output")
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

    parser.add_argument("--update", "-u", action="store_true", help="Update to the latest upstream version (also enables --fix). Use --update=VERSION for a specific version.")
    parser.add_argument("--update-only", action="store_true", help="Update sources to the latest upstream version, then exit (no test build). Use --update-only=VERSION for a specific version.")
    parser.add_argument("--update-version", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--preset", default=None, help="Preset name to pass to pbuild")
    parser.add_argument("--allow-tool-scripts", action="store_true", help="Allow execution of scripts from <workspace>/tool-scripts/")
    parser.add_argument("--debug", "-D", action="store_true", help="Print raw JSON responses from Ollama")
    parser.add_argument("--fix-attempts", type=int, default=10, help="Max fix retry attempts per package (default: 10, resets for each package)")
    parser.add_argument("--all", "-a", action="store_true", help="Build all packages in project mode (runs pbuild --abort-on-fail, fixes failures, and restarts)")
    parser.add_argument("--deep-analyze", "-d", action="store_true", help="On build failure, open an interactive shell in the build environment instead of auto-fixing")
    parser.add_argument("--prompt", "-p", default=None, help="Additional hint to include in all analysis prompts sent to Ollama")
    parser.add_argument("--modify", "-m", default=None, help="Modify package sources: send prompt + sources to Ollama, apply changes locally, then quit (no build)")
    parser.add_argument("--generate", default=None, help="Generate a new package from scratch in workspace_dir based on the given prompt. The tool will research upstream, ask clarifying questions, and create spec files.")
    parser.add_argument("-i", "--interactive", action="store_true", help="Ask the user to select which changes to apply when Ollama proposes multiple tool calls")
    parser.add_argument("--openai-server", default=None, help="OpenAI-compatible server URL (overrides OLLAMA_HOST env var, default http://localhost:11434)")
    parser.add_argument("--model", default=None, help="Ollama model name (overrides OLLAMA_MODEL env var, default gemma4)")
    parser.add_argument("--email", default=None, help="Email address for PACKAGE.changes entries (e.g., 'adrian@suse.de' or 'Adrian Schröter <adrian@suse.de>'). Falls back to EMAIL env var.")
    clean_group = parser.add_mutually_exclusive_group()
    clean_group.add_argument("--clean", action="store_true", default=False, help="Clean build artifacts before building")
    clean_group.add_argument("--no-clean", action="store_true", default=True, help="Do not clean build artifacts (default)")
    args = parser.parse_args()

    ctx = PbuildContext(
        workspace_dir=args.workspace_dir,
        root_dir=args.root,
        package_filter=args.package_name,
        fix_mode=args.fix or args.update or args.modify is not None,
        show_buildlog=args.show_buildlog,
        do_clean=args.clean,
        vm_type=args.vm_type,
        vm_memory=args.vm_memory,
        preset=args.preset,
        allow_tool_scripts=args.allow_tool_scripts,
        debug=args.debug,
        deep_analyze=args.deep_analyze,
        fix_attempts=args.fix_attempts,
        all_mode=args.all,
        prompt_hint=args.prompt,
        update_version=args.update_version or "" if (args.update or args.update_only) else None,
        update_only=args.update_only,
        modify_prompt=args.modify,
        generate_prompt=args.generate,
        ollama_server=args.openai_server,
        ollama_model_arg=args.model,
        shell_after_build=args.shell_after_build,
        interactive=args.interactive,
        email=args.email or os.environ.get("EMAIL", ""),
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
    FIX_ATTEMPTS = ctx.fix_attempts
    ALL_MODE = ctx.all_mode
    PROMPT_HINT = ctx.prompt_hint
    UPDATE_VERSION = ctx.update_version
    INTERACTIVE = ctx.interactive
    MODIFY_PROMPT = ctx.modify_prompt
    GENERATE_PROMPT = ctx.generate_prompt
    OPENAI_SERVER = ctx.ollama_server
    OLLAMA_MODEL_ARG = ctx.ollama_model_arg
    MAX_ALL_ATTEMPTS = 50
    ROOT_DIR = ctx.root_dir
    SKILLS_DIR = Path(__file__).parent / "skills"

    Path(WORKSPACE_DIR).mkdir(exist_ok=True)
    ctx.project_mode = PROJECT_MODE

    if ALL_MODE and not PROJECT_MODE:
        print("[ERROR] --all requires a project directory with _manifest")
        sys.exit(1)

    manager = RpmSourceManager(WORKSPACE_DIR, do_clean=DO_CLEAN, vm_type=ctx.vm_type, vm_memory=ctx.vm_memory, shell_after_build=ctx.shell_after_build, preset=PRESET, root_dir=ROOT_DIR)
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
    
    ollama = OllamaAnalyzer(host=OPENAI_SERVER, model=OLLAMA_MODEL_ARG or os.environ.get("OLLAMA_MODEL", "gemma4"), debug=DEBUG)
    ctx.ollama = ollama
    ctx.full_context = full_context

    # Default prompts as fallback
    DEFAULT_SPEC_PROMPT = ctx.default_spec_prompt
    DEFAULT_ERROR_PROMPT = ctx.default_error_prompt
    
    def default_fix(content):
        return content # Does nothing if no skill matches

    # Define allowed tools (file operations within workspace + remote web fetching)
    TOOLS = build_tools_list()
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
            if not result.startswith("OK: Wrote ") or not result.endswith(".patch"):
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
                    dep_skill = skill_manager.get_skill_for(dep_spec.name, manager.read_file_safe(dep_spec))
                    dep_prompt = getattr(dep_skill, 'OLLAMA_SPEC_PROMPT', DEFAULT_SPEC_PROMPT) if dep_skill else DEFAULT_SPEC_PROMPT
                    dep_spec_analysis = ollama.analyze(dep_prompt, manager.read_file_safe(dep_spec), full_context)
                    print(f"-> Ollama says about {dep_spec.name}:\n{dep_spec_analysis}\n")
                    dep_success, dep_out = manager.run_project_build(suggested, stream_output=SHOW_BUILDLOG)
                    if dep_success:
                        print(f"[DEP] '{suggested}' built successfully. Continuing with current package.")
                        return True
                    else:
                        print(f"[DEP WARN] '{suggested}' build also failed. Trying to continue with current package anyway.")
                        return False
        return False

    def run_fix_loop(spec, package_name, initial_build_out, error_prompt, rebuild_func, exit_on_no_changes=False, exit_on_exhaustion=False):
        """Shared fix loop: diagnose → apply tool changes → rebuild → repeat."""
        MAX_ATTEMPTS = FIX_ATTEMPTS if FIX_ATTEMPTS > 0 else 999999
        unlimited = FIX_ATTEMPTS == 0
        fix_attempt = 0
        current_build_out = initial_build_out
        fix_messages = None
        build_success2 = False
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
                    error_context = f"Build output:\n{current_build_out[:2000]}\n\nBuild log:\n{log_content}"
                else:
                    print(f"[DIAG] Warning: {log_content}")
                    # No log found — check unresolvable in build output, otherwise abort (single-mode only)
                    if not PROJECT_MODE and not ("unresolvable" in build_out_lower or "nothing provides" in build_out_lower):
                        print("[BUG] No build log and no unresolvable deps detected. This is likely a bug in pbuild-ai or pbuild. Aborting.")
                        sys.exit(1)
                    error_context = current_build_out
            error_analysis = ollama.analyze(error_prompt, error_context, full_context)
            print(f"\n--- OLLAMA ERROR ANALYSIS ---\n{error_analysis}\n-----------------------------\n")
            # Auto-trigger deep-analyze if Ollama requests it and we aren't already in that mode
            if "[DEEP_ANALYZE]" in error_analysis and not DEEP_ANALYZE:
                print("\n[DEEP ANALYZE] Ollama requested interactive investigation. Opening shell...")
                manager.run_deep_analyze_shell(package_name=spec.stem, ollama=ollama, full_context=full_context, project_mode=PROJECT_MODE, debug=DEBUG, deep_analyze_prompt=skill_manager.get_deep_analyze_prompt())
                time.sleep(3)
                print("[DEEP ANALYZE] Shell exited. Re-analyzing with collected data...")
                deep_context = f"{full_context}\n\n--- Deep investigation data ---\n{manager.deep_exploration[-20000:]}"
                error_analysis = ollama.analyze(error_prompt, error_context, deep_context)
                error_analysis = error_analysis.replace("[DEEP_ANALYZE]", "").strip()
                print(f"\n--- OLLAMA ERROR ANALYSIS (after deep investigation) ---\n{error_analysis}\n-----------------------------\n")
            if spec_files:
                build_suggested_dependency(error_analysis, spec_files, manager, ollama, full_context)
            print("[FIX MODE] Applying suggested changes via tool calls...")
            spec_content = manager.read_file_safe(spec)
            fix_context = full_context or 'No AGENTS.md'
            if PROMPT_HINT:
                fix_context = f"{fix_context}\n\n--- User Hint (prefer this over generic analysis) ---\n{PROMPT_HINT}"
            if fix_messages is None:
                fix_messages = [
                    {"role": "system", "content": f"""You are an RPM packager assistant. Fix build failures by using tools.

You MUST call one or more of these tools NOW to make changes:
- edit_file(path, old_string, new_string): targeted search-and-replace (PREFER this for small changes)
- write_file(path, content): write a file (use only for large rewrites or new files)
- read_file(path): read a file
- web_fetch(url): fetch an HTTPS URL
- git_command(command): run a git command
- run_tool_script(script_name, args): run a script from tool-scripts/

Call the tools to make changes. You may need to read files first, then call edit_file or write_file.
Prefer edit_file for small targeted changes — it replaces only the matching text and preserves all other lines.
IMPORTANT: write_file writes the ENTIRE file. You must include ALL lines.
PRESERVE EVERY LINE YOU ARE NOT CHANGING VERBATIM — do not add, remove, or modify anything beyond the specific fix.
Keep in mind that your changes need to be reviewed. So keep changes minimal unless stated otherwise.
Make all necessary changes now, then stop.

AGENTS.md instructions (follow these):
{fix_context}"""},
                    {"role": "user", "content": f"""The build for {spec.name} failed.
Package: {package_name}
Spec file path: {spec.relative_to(WORKSPACE_DIR)}

Error context:
{error_context[:5000]}

Current spec content:
{spec_content[:5000]}

Call the tools to fix the build failure NOW."""}
                ]
            else:
                fix_messages.append({"role": "assistant", "content": (error_analysis or "")[:2000]})
                fix_messages.append({"role": "user", "content": f"""The previous fix attempt did not resolve the build for {package_name}. Here is the new error context:

{error_context[:3000]}

Consult the skill rules (OPENSUSE.md / Build & Packaging Rules) in the system prompt for the exact fix pattern — the solution is almost certainly described there. Apply the specific fix using write_file now."""})
                MAX_HISTORY = 40
                if len(fix_messages) > MAX_HISTORY:
                    fix_messages = [fix_messages[0]] + fix_messages[-(MAX_HISTORY - 1):]
            tool_results = ollama.call_with_tools(fix_messages, TOOLS, manager, WORKSPACE_DIR, ALLOW_TOOL_SCRIPTS, interactive=INTERACTIVE)
            if isinstance(tool_results, str):
                print(f"[FIX ERROR] {tool_results}")
            elif tool_results:
                for r in tool_results:
                    display = r.split("\n", 1)[0] if r.startswith("[Fetched ") else (r[:500] + "..." if len(r) > 500 else r)
                    print(f"[FIX] {display}")
                relocate_patches(tool_results, spec)
            else:
                print("[FIX] No tool calls. Retrying with forceful tool demand...")
                fix_messages.append({"role": "assistant", "content": error_analysis})
                fix_messages.append({"role": "user", "content": f"""Your analysis above is correct. Now apply these changes to the spec file.

Do NOT explain again. Do NOT summarize. Do NOT ask questions.
Prefer edit_file for targeted changes — it preserves all other lines.
IMPORTANT: write_file writes the ENTIRE file. Include EVERY line verbatim — preserve all lines except the specific fix.
Keep in mind that your changes need to be reviewed. So keep changes minimal unless stated otherwise.
Apply the corrected spec content NOW."""})
                tool_results = ollama.call_with_tools(fix_messages, TOOLS, manager, WORKSPACE_DIR, ALLOW_TOOL_SCRIPTS, interactive=INTERACTIVE)
                if isinstance(tool_results, str):
                    print(f"[FIX ERROR] {tool_results}")
                elif tool_results:
                    for r in tool_results:
                        display = r.split("\n", 1)[0] if r.startswith("[Fetched ") else (r[:500] + "..." if len(r) > 500 else r)
                        print(f"[FIX] {display}")
                    relocate_patches(tool_results, spec)
                else:
                    print("[FIX] No tool calls received. Asking Ollama to rewrite the spec file...")

                    def try_rewrite():
                        prompt = f"""The build for {spec.name} failed.

Error (from analysis):
{error_analysis[:2000]}

Current spec:
{spec_content[:5000]}

Fix the spec file. Your output must be ONLY the complete raw spec file content.
- No markdown formatting
- No code fences
- No explanations, no introductory text, no summary
- Start with the first line of the spec (typically Name: or # or %)
- Output the COMPLETE spec, not just the changed parts
- Just raw spec content and nothing else"""
                        result = ollama.analyze("You are an RPM spec expert.", prompt, full_context)
                        if not result or result.startswith("[OLLAMA ERROR"):
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
                            print(f"[FIX] Rewrote {spec.name} with Ollama's fix.", flush=True)
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
            if not changed and not tool_results:
                print("[FIX ERROR] No source changes were made. Aborting rebuild.", flush=True)
                if exit_on_no_changes:
                    sys.exit(1)
                break

            print("[FIX MODE] Re-building to verify...", flush=True)
            build_success2, build_out2 = rebuild_func(package_name)

            if build_success2:
                print(f"\n[OK] Fix verified: Build for {spec.name} succeeded after applying changes.")
                if DEEP_ANALYZE:
                    export_deep_fix_patch(WORKSPACE_DIR, spec, spec.stem)
                break
            else:
                print(f"\n[WARN] Fix attempt {fix_attempt} still failing.")
                error_analysis2 = ollama.analyze(error_prompt, build_out2, full_context)
                print(f"\n--- OLLAMA ERROR ANALYSIS (attempt {fix_attempt}) ---\n{error_analysis2}\n------------------------------------------\n")
                current_build_out = build_out2

        if not build_success2:
            label = MAX_ATTEMPTS if not unlimited else "unlimited"
            print(f"[FIX ERROR] All {label} fix attempts exhausted. Build still failing.")
            if exit_on_exhaustion:
                sys.exit(1)

        ollama.print_stats()
        return build_success2

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

        # --generate mode: create a new package from scratch
        if ctx.generate_prompt:
            run_generate_mode(ctx)
            sys.exit(0)

        # --modify mode: hand sources + prompt to Ollama, apply changes locally
        if ctx.modify_prompt:
            run_modify_mode(ctx)
            if not FIX_MODE:
                sys.exit(0)  # --modify without --fix: only modifies sources, does not build

        # --all mode: build all packages, detect failure, fix, restart
        if ALL_MODE:
            spec_map = {s.stem: s for s in spec_files}
            all_attempt = 0
            while all_attempt < MAX_ALL_ATTEMPTS:
                all_attempt += 1
                print(f"\n[ALL MODE] Full project build (attempt {all_attempt}/{MAX_ALL_ATTEMPTS})...")
                all_success, all_out = manager.run_full_project_build(stream_output=SHOW_BUILDLOG)
                if all_success:
                    print("\n[OK] All packages built successfully.")
                    break
                if all_attempt >= MAX_ALL_ATTEMPTS:
                    print(f"[ALL ERROR] All {MAX_ALL_ATTEMPTS} full build attempts exhausted.")
                    break
                failed_pkg = parse_failed_package(all_out)
                if not failed_pkg or failed_pkg not in spec_map:
                    print(f"[ALL ERROR] Could not identify failing package. Falling back to individual builds.")
                    sys.exit(1)
                spec = spec_map[failed_pkg]
                print(f"\n[ALL MODE] Package '{failed_pkg}' failed. Running fix loop...")
                ollama.reset_context()
                ollama.reset_stats()
                skill = skill_manager.get_skill_for(spec.name, manager.read_file_safe(spec), prompt=MODIFY_PROMPT)
                if skill:
                    error_prompt = getattr(skill, 'OLLAMA_ERROR_PROMPT', DEFAULT_ERROR_PROMPT)
                    fix_func = getattr(skill, 'fix_content', default_fix)
                    skill_ctx = getattr(skill, 'OLLAMA_SPEC_PROMPT', '')
                    if skill_ctx:
                        full_context = f"{full_context}\n\n--- Skill: {skill.__name__} ---\n{skill_ctx}"
                else:
                    error_prompt = DEFAULT_ERROR_PROMPT
                    fix_func = default_fix
                spec_content = manager.read_file_safe(spec)
                current_build_out = all_out
                # Before entering fix loop: if build never reached build phase, retry with --clean
                if not manager.build_phase_reached(package_name=failed_pkg):
                    print(f"[ALL MODE] Build did not reach build phase. Retrying {failed_pkg} with --clean...")
                    build_ok, build_out2 = manager.run_project_build(failed_pkg, stream_output=SHOW_BUILDLOG, force_clean=True)
                    if build_ok:
                        print(f"\n[OK] {failed_pkg} succeeded after --clean retry.")
                        continue
                    else:
                        all_out = build_out2
                        current_build_out = build_out2
                        print(f"[ALL MODE] Clean build also failed. Proceeding with fix loop.")
                if not run_fix_loop(spec, failed_pkg, current_build_out, error_prompt,
                    lambda p: manager.run_project_build(p, stream_output=SHOW_BUILDLOG),
                    exit_on_exhaustion=True):
                    sys.exit(1)
            sys.exit(0)

        # Phase 1: Update pass — update all packages first without building
        updated_packages = set()
        if UPDATE_VERSION is not None:
            base_full_context = full_context
            email_author = EMAIL if EMAIL else "<Your Name> <your@email>"
            for spec in spec_files:
                ollama.reset_context()
                ollama.reset_stats()

                skill = skill_manager.get_skill_for(spec.name, manager.read_file_safe(spec), prompt=MODIFY_PROMPT)
                if skill:
                    print(f"[INFO] Using skill profile: {skill.__name__}")
                    skill_ctx = getattr(skill, 'OLLAMA_SPEC_PROMPT', '')
                    if skill_ctx:
                        full_context = f"{base_full_context}\n\n--- Skill: {skill.__name__} ---\n{skill_ctx}"
                else:
                    print("[INFO] No specific skill found. Using default profile.")

                spec_before_update = manager.read_file_safe(spec)
                target_version = UPDATE_VERSION
                if not target_version:
                    print(f"\n[UPDATE] Researching latest upstream version for {spec.name}...")
                    spec_content = manager.read_file_safe(spec)
                    research_messages = [
                        {"role": "system", "content": f"""You are an RPM packager assistant. Find the latest upstream version for the spec file below.

You MUST complete ALL steps before stopping.

Steps (do them in order, never skip any):
1. Examine the Source URLs in the spec to identify the upstream project
2. Use web_fetch to find the latest stable version:
   - For GitHub projects, try the API first (https://api.github.com/repos/OWNER/REPO/releases/latest) — it returns JSON with the 'tag_name' field
   - For GitLab, try https://gitlab.com/api/v4/projects/OWNER%2FREPO/releases/permalink/latest
   - For PyPI, try https://pypi.org/pypi/PACKAGE/json
   - Fall back to fetching the releases page if no API is available
3. Fetch the release notes / changelog from the upstream release page using web_fetch:
   - For GitHub, fetch the release page (e.g., https://github.com/OWNER/REPO/releases/tag/vVERSION) or use the API tag endpoint
   - For GitLab, fetch the repository release page
   - For PyPI, fetch https://pypi.org/pypi/PACKAGE/VERSION/json and look for the description or release_url field
   - Extract the changelog entries for this version from the fetched content
4. Update the spec — make ONLY these changes and nothing else:
   - Prefer edit_file for targeted changes (it preserves all other lines)
   - Change the Version tag to the new version number
   - Update Source URLs ONLY if they contain the OLD version number literally (e.g., "1.0.19" in the URL); do NOT replace the %{{version}} macro
   - PRESERVE ALL OTHER LINES VERBATIM — do not add, remove, or modify anything else
5. Update the .changes file (same name as the .spec but with .changes extension):
   - Use list_files to find the .changes file if unsure of its name
    - Prepend a new changelog entry in openSUSE format using edit_file:
      * <Day> <Month> <Date> <Year> {email_author} - NEWVERSION
     - Updated to version NEWVERSION
     - <changelog details from the upstream release notes>
     - Update generated using pbuild-ai
   - If the .changes file does not exist, create it with write_file
6. Check for a _service file next to the spec (use list_files). If present, read it with read_file and update all <revision> tags to match the new version using edit_file.
7. Avoid using files with .obscpio suffix. eg from "obs_scm" service calls. Try to convert these to remote assets instead.
8. Download the new source tarball using download_file — this is MANDATORY when the package is using a tar ball, do not skip it. Include the package subdirectory in the filename argument (e.g., "libopenshot/libopenshot-0.4.0.tar.xz" not just "libopenshot-0.4.0.tar.xz") — use list_files output to find the correct relative path from the workspace root. Look at the Source URL in the spec file to determine the correct download URL pattern, then substitute %{{version}} and any old version literals with the new version number. Do NOT pick download URLs from the release page assets — those are often precompiled binaries. The correct source tarball URL is the one defined in the spec's Source tag, reconstructed with the new version.

Also consult the AGENTS.md / skill rules below for project-specific update steps (e.g., tarball updates, _service file changes, additional files to update).

Spec file ({spec}):
{spec_content}

Additional context (AGENTS.md + skill rules):
{full_context}"""},
                    ]
                    results = ollama.call_with_tools(research_messages, TOOLS, manager, WORKSPACE_DIR, ALLOW_TOOL_SCRIPTS, interactive=INTERACTIVE, max_rounds=15)
                    if isinstance(results, str):
                        print(f"[UPDATE ERROR] {results}")
                    elif results:
                        for r in results:
                            if DEBUG:
                                print(f"[UPDATE] {r}")
                            elif r.startswith("web_fetch: [Fetched "):
                                display = r.split("\n", 1)[0]
                                print(f"[UPDATE] {display}")
                            else:
                                display = r[:500] + "..." if len(r) > 500 else r
                                print(f"[UPDATE] {display}")
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
                    update_prompt = f"""Update the spec file to version {target_version}:
- Use web_fetch to get the release notes for version {target_version} from the upstream project page (GitHub releases, GitLab releases, PyPI, etc.)
- Update the Version tag
- Update any Source and Patch URLs that include version numbers
- PRESERVE ALL OTHER LINES VERBATIM — do not add, remove, or modify anything else
- Then update the .changes file (same stem as the spec, e.g., PACKAGE.changes) with a new entry based on the release notes — use list_files to find it if needed. Use "{email_author}" as the author in the entry header. Append "  - Update generated using pbuild-ai" as the last line of the entry
- Check for a _service file next to the spec (use list_files). If present, update all <revision> tags to match the new version.
- Then download the new source tarball using download_file — include the package subdirectory in the filename (check list_files output for the correct relative path from workspace root). Construct the URL from the spec's Source tag (substituting %{{version}} and the old version), not from the release page assets which are often precompiled binaries

Also consult the AGENTS.md / skill rules below for version specific update steps (e.g., tarball updates, service file changes, additional files to update).

Additional context (AGENTS.md + skill rules):
{full_context}"""
                    messages = [
                        {"role": "system", "content": update_prompt},
                        {"role": "user", "content": f"Update this spec file to version {target_version}:\n\n{manager.read_file_safe(spec)}"}
                    ]
                    results = ollama.call_with_tools(messages, TOOLS, manager, WORKSPACE_DIR, ALLOW_TOOL_SCRIPTS, interactive=INTERACTIVE, max_rounds=15)
                    if isinstance(results, str):
                        print(f"[UPDATE ERROR] {results}")
                    elif results:
                        for r in results:
                            if DEBUG:
                                print(f"[UPDATE] {r}")
                            elif r.startswith("web_fetch: [Fetched "):
                                display = r.split("\n", 1)[0]
                                print(f"[UPDATE] {display}")
                            else:
                                display = r[:500] + "..." if len(r) > 500 else r
                                print(f"[UPDATE] {display}")
                    else:
                        print("[UPDATE] No changes made.")

                spec_after = manager.read_file_safe(spec)
                if spec_after != spec_before_update:
                    updated_packages.add(spec)
                    print(f"[UPDATE] Updated {spec.name}.")
                else:
                    print(f"[UPDATE] No changes for {spec.name}.")

            if ctx.update_only:
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

        for spec in spec_files:
            ollama.reset_context()
            ollama.reset_stats()

            if UPDATE_VERSION is not None and spec not in updated_packages:
                continue

            # 1. Determine skill
            skill = skill_manager.get_skill_for(spec.name, manager.read_file_safe(spec), prompt=MODIFY_PROMPT)
            if skill:
                print(f"[INFO] Using skill profile: {skill.__name__}")
                spec_prompt = getattr(skill, 'OLLAMA_SPEC_PROMPT', DEFAULT_SPEC_PROMPT)
                error_prompt = getattr(skill, 'OLLAMA_ERROR_PROMPT', DEFAULT_ERROR_PROMPT)
                fix_func = getattr(skill, 'fix_content', default_fix)
                skill_ctx = getattr(skill, 'OLLAMA_SPEC_PROMPT', '')
                if skill_ctx:
                    full_context = f"{full_context}\n\n--- Skill: {skill.__name__} ---\n{skill_ctx}"
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
                    print(f"[OLLAMA] Analyzing Spec-file: {spec.name}...")
                    analysis_context = full_context
                    if PROMPT_HINT:
                        analysis_context = f"{analysis_context}\n\n--- User Hint (prefer this over generic analysis) ---\n{PROMPT_HINT}"
                    spec_analysis = ollama.analyze(spec_prompt, manager.read_file_safe(spec), analysis_context)
                    print(f"-> Ollama says:\n{spec_analysis}\n")
                    if not FIX_MODE or manager.has_prior_failed_build():
                        manager.fix_file_content(spec, fix_func)

            # 4. Determine build mode and execute (always from WORKSPACE_DIR, never cd)
            if PACKAGE_FILTER:
                package_name = spec.stem
                print(f"[INFO] Building single package: {package_name}...")

                build_success, build_out = manager.run_project_build(package_name, preset=PRESET, stream_output=SHOW_BUILDLOG)
            elif PROJECT_MODE:
                package_name = spec.stem
                print(f"[INFO] Building {package_name} from project directory...")

                # FIXME: add flavor support
                build_success, build_out = manager.run_project_build(package_name, preset=PRESET, stream_output=SHOW_BUILDLOG)
            else:
                # Single package mode - WORKSPACE_DIR IS a single package, no iteration needed
                print("[INFO] Single package mode (no _manifest found). Running orphan build...")
                build_success, build_out = manager.run_orphan_build(stream_output=SHOW_BUILDLOG)

            # 4b. Retry with --clean if incomplete setup detected
            INCOMPLETE_SETUP_MSG = "It seems that there was an incomplete setup of /"
            if not build_success and INCOMPLETE_SETUP_MSG in build_out:
                print(f"[RETRY] Incomplete setup detected. Retrying with --clean...")
                if PACKAGE_FILTER or PROJECT_MODE:
                    build_success, build_out = manager.run_project_build(package_name, preset=PRESET, stream_output=SHOW_BUILDLOG, force_clean=True)
                else:
                    build_success, build_out = manager.run_orphan_build(stream_output=SHOW_BUILDLOG, force_clean=True)
            
            # 4c. If build failed without reaching build phase, retry with --clean instead of modifying sources
            #     (runs before consulting Ollama to avoid unnecessary analysis)
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

            # 5. Ollama Error Analysis
            if build_success:
                print(f"\n[OK] Build for {spec.name} succeeded.")
                ollama.print_stats()
            else:
                print(f"\n[ERROR] Build for {spec.name} failed. Consulting Ollama...")
                error_analysis = ollama.analyze(error_prompt, build_out, full_context)
                print(f"\n--- OLLAMA ERROR ANALYSIS ---\n{error_analysis}\n-----------------------------\n")

            # 6. Fix mode: diagnose, apply changes via tool calling, and re-build (with retries)
            if FIX_MODE and not build_success:
                pkg_name = package_name if 'package_name' in dir() else spec.stem
                if PROJECT_MODE:
                    rebuild_func = lambda p: manager.run_project_build(p, stream_output=SHOW_BUILDLOG)
                else:
                    rebuild_func = lambda p: manager.run_orphan_build(stream_output=SHOW_BUILDLOG)
                run_fix_loop(spec, pkg_name, build_out, error_prompt, rebuild_func, exit_on_no_changes=True)
                
    except Exception as e:
        try:
            print(f"Script aborted: {e}")
        except BlockingIOError:
            sys.stderr.write(f"Script aborted: {e}\n")

def main():
    """Entry point for pbuild-ai CLI."""
    import runpy
    runpy.run_path(__file__, run_name="__main__")
