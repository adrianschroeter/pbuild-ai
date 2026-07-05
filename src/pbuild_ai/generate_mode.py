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

import json
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

from pbuild_ai.ollama_client import chat_completion
from pbuild_ai.tools import execute_tool_calls
from pbuild_ai.spinner import Spinner, AI_COLOR

_ARCHIVE_EXTS = ('.tar.gz', '.tgz', '.tar.bz2', '.tar.xz', '.tar', '.zip')
_INDICATOR_FILES = (
    'package.json', 'go.mod', 'Cargo.toml', 'Gemfile',
    'setup.py', 'pyproject.toml', 'composer.json', 'pom.xml',
    'Makefile.PL', 'DESCRIPTION', 'rebar.config', 'mix.exs',
)


def _indicator_matches(name):
    for ind in _INDICATOR_FILES:
        if name == ind or name.endswith('/' + ind):
            return True
    return False


def _check_archives_for_skills(ctx, evaluated_archives, injected_skills, messages):
    """Peek into downloaded archives to find indicator files and match skills."""
    for f in Path(ctx.workspace_dir).iterdir():
        if not f.is_file() or not any(f.name.endswith(e) for e in _ARCHIVE_EXTS):
            continue
        arch_str = str(f)
        if arch_str in evaluated_archives:
            continue
        evaluated_archives.add(arch_str)
        try:
            if f.suffix == '.zip':
                with zipfile.ZipFile(f, 'r') as zf:
                    names = zf.namelist()
                    for cand in (n for n in names if _indicator_matches(n)):
                        info = zf.getinfo(cand)
                        if info.file_size < 100000:
                            content = zf.read(cand).decode('utf-8', errors='replace')
                            _apply_matching_skills(ctx, cand, content, injected_skills, messages)
            else:
                with tarfile.open(f, 'r:*') as tar:
                    names = tar.getnames()
                    for cand in (n for n in names if _indicator_matches(n)):
                        try:
                            info = tar.getmember(cand)
                        except KeyError:
                            continue
                        if info.isfile() and info.size < 100000:
                            fh = tar.extractfile(info)
                            if fh:
                                content = fh.read().decode('utf-8', errors='replace')
                                _apply_matching_skills(ctx, cand, content, injected_skills, messages)
        except Exception:
            pass


def _apply_matching_skills(ctx, filename, content, injected_skills, messages):
    matching = ctx.skill_manager.get_skills_for(filename, content, ctx.generate_prompt)
    for skill in matching:
        skill_name = getattr(skill, 'SKILL_NAME', '?')
        if skill_name in injected_skills:
            continue
        injected_skills.add(skill_name)
        prompt = getattr(skill, 'OLLAMA_SPEC_PROMPT', None)
        if prompt:
            print(f"[GENERATE] Applied skill: {skill_name}")
            messages.append({"role": "system", "content": f"[Skill: {skill_name}]\n{prompt}"})


def run_generate_mode(ctx):
    """Create a new openSUSE RPM package from scratch via Ollama + tools (up to 50 rounds)."""
    print(f"[GENERATE] Creating new package from prompt: {ctx.generate_prompt}")
    generate_skill = ctx.skill_manager.get_skill_by_name("generate_mode")
    if generate_skill:
        system_content = generate_skill.GENERATE_SYSTEM_PROMPT.format(
            generate_prompt=ctx.generate_prompt,
            full_context=ctx.full_context or 'No AGENTS.md',
        )
        user_content = generate_skill.GENERATE_USER_PROMPT.format(
            workspace_dir=ctx.workspace_dir,
        )
    else:
        print("[INFO] generate_mode skill not found, using inline fallback.")
        system_content = f"""You are an RPM packager assistant. Your task is to create a new openSUSE RPM package from scratch based on the user's specification below.

THE USER'S SPECIFICATION (this is the complete request, not a conversation starter):
{ctx.generate_prompt}

IMPORTANT: The specification above IS the request. Do NOT ask the user "what would you like to package?" or otherwise request information they already provided. Start working immediately based on the specification given.

Follow these rules:
1. Research the upstream project first using web_fetch if a URL is provided or you can infer one, then create the package. Do NOT fetch the same URL more than once — the result is cached.
2. For GitHub projects, use https://api.github.com/repos/OWNER/REPO/releases/latest and https://api.github.com/repos/OWNER/REPO/tags to find release versions and tarball URLs instead of the main HTML page. For specific tags, use https://github.com/OWNER/REPO/archive/refs/tags/TAG.tar.gz.
2. Only call ask_user if the specification is truly missing critical information (e.g., no project name, no source URL, no license hint, and you cannot determine it from research). Do NOT ask generic questions.
3. Create the .spec file directly in the workspace root, next to the downloaded source tarball — do NOT use a subdirectory.
4. Create a complete .spec file following openSUSE packaging conventions from OPENSUSE.md:
   - Keep the copyright header
   - Empty %%changelog is acceptable
   - Do NOT use ?dist macro — use ~ in version format
   - Use standard SUSE RPM macros: %%fdupes, %%set_permissions
   - Single dependency per BuildRequires: line
   - Omit empty %%clean, %%changelog, %%post, %%pre, %%preun, %%postun sections
   - Never recommend rpmbuild
   - Build environment has NO network access — patch out any code that tries to reach external hosts at build time
5. If the upstream provides source archives, set Source0 to the download URL and Source1..N for additional files.
6. You MAY also create supporting files (patches, .desktop, sysconfig, tmpfiles.d, etc.) as needed.
7. When you are done creating files, call run_tool_script("format_spec_file", []) on the spec directory as your final step to normalize spec formatting.
8. When you are done, tell the user what you created.
9. Do NOT use HTML or markdown formatting in your text responses — use plain text only. No <b>, <a>, <pre>, or any other tags.

AGENTS.md instructions (follow these):
{ctx.full_context or 'No AGENTS.md'}"""
        user_content = f"""Workspace directory: {ctx.workspace_dir}

The specification for the package to create is in the system prompt above. Start researching and building — do NOT ask me what to package, I already told you."""

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]
    generate_max_rounds = 50
    fetch_cache = {}
    _evaluated_specs = set()
    _evaluated_archives = set()
    _injected_skills = set()
    for round_idx in range(generate_max_rounds):
        with Spinner(prefix=f"[AI] {ctx.ollama.model}", color=AI_COLOR):
            result = chat_completion(ctx.ollama, messages, ctx.tools, debug=ctx.debug, track_stats=True)

        message = result.get('message', {})
        if 'tool_calls' in message and message['tool_calls']:
            round_calls = []
            for tc in message['tool_calls']:
                tool_name = tc['function']['name']
                raw_args = tc['function']['arguments']
                tool_input = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
                if tool_name == "web_fetch" and tool_input.get("url") in fetch_cache:
                    cached = fetch_cache[tool_input["url"]]
                    print(f"[GENERATE] Cached: web_fetch({tool_input['url']}) ({len(cached)} bytes)", flush=True)
                    round_calls.append(("_skip", {"_cached": cached}))
                    continue
                round_calls.append((tool_name, tool_input))

            if ctx.interactive and sum(1 for c in round_calls if c[0] in ("write_file", "edit_file", "remove_file", "rename_file", "run_tool_script")) > 1:
                print(f"\n--- Ollama proposes {len(round_calls)} tool calls ---")
                for idx, (name, inp) in enumerate(round_calls, 1):
                    args_preview = json.dumps(inp)[:300]
                    print(f"  [{idx}] {name}({args_preview})")
                print(f"  [a] Execute all")
                print(f"  [n] Execute none")
                selection = input("Select tool calls to execute (e.g. '1,3' or 'a'): ").strip().lower()
                if selection == 'n':
                    print("Skipping all tool calls.")
                    continue
                if selection != 'a':
                    selected = set()
                    for part in selection.split(','):
                        part = part.strip()
                        if part.isdigit():
                            idx = int(part)
                            if 1 <= idx <= len(round_calls):
                                selected.add(idx - 1)
                    round_calls = [c for i, c in enumerate(round_calls) if i in selected]
                    if not round_calls:
                        print("No tool calls selected.")
                        continue

            for name, tool_input in round_calls:
                if name == "_skip":
                    continue
                args_preview = json.dumps(tool_input)[:300]
                if ctx.debug:
                    print(f"[AI] Tool call: {name}({args_preview})", flush=True)
            try:
                round_results = execute_tool_calls([(n, i) for n, i in round_calls if n != "_skip"], ctx.manager, ctx.workspace_dir, ctx.allow_tool_scripts, interactive=ctx.interactive, debug=ctx.debug)
            except Exception as e:
                non_skip_count = sum(1 for n, _ in round_calls if n != "_skip")
                round_results = [f"Error executing tool: {e}"] * non_skip_count
                print(f"[GENERATE TOOL ERROR] {e}")
            final_results = []
            cache_idx = 0
            for name, inp in round_calls:
                if name == "_skip":
                    final_results.append(inp["_cached"])
                else:
                    if cache_idx < len(round_results):
                        result = round_results[cache_idx]
                        if name == "web_fetch" and result.startswith("[Fetched "):
                            url = inp["url"]
                            fetch_cache[url] = result
                    else:
                        result = f"Error: Missing result for tool call #{cache_idx} ({name})"
                    final_results.append(result)
                    cache_idx += 1
            round_results = final_results
            for (name, inp), r in zip(round_calls, round_results):
                if name == "read_file":
                    line_count = r.count('\n')
                    display = f"read_file: {inp.get('path', '?')} ({line_count} lines)"
                elif name in ("list_archive", "list_files"):
                    continue
                elif name == "read_file_from_archive":
                    if not ctx.debug:
                        continue
                    display = r[:500] + "..." if len(r) > 500 else r
                elif r.startswith("[Fetched "):
                    display = r.split("\n", 1)[0]
                else:
                    display = r[:500] + "..." if len(r) > 500 else r
                print(f"[GENERATE] {display}", flush=True)
            response_content = message.get('content', '') or ''
            tc_arg = dict(tool_calls=message['tool_calls'])
            messages.append({"role": "assistant", "content": response_content, **tc_arg})
            for (name, _), content in zip(round_calls, round_results):
                tool_name = "web_fetch" if name == "_skip" else name
                if name == "read_file" and isinstance(content, str) and len(content) > 2000:
                    content = content[:1000] + "\n... (truncated) ...\n" + content[-900:]
                messages.append({"role": "tool", "content": str(content), "name": tool_name})
            spec_files = sorted(Path(ctx.workspace_dir).rglob("*.spec"))
            for spec_path in spec_files:
                spec_str = str(spec_path)
                if spec_str in _evaluated_specs:
                    continue
                _evaluated_specs.add(spec_str)
                try:
                    spec_content = spec_path.read_text(encoding='utf-8')
                except Exception:
                    continue
                _apply_matching_skills(ctx, spec_path.name, spec_content, _injected_skills, messages)
            _check_archives_for_skills(ctx, _evaluated_archives, _injected_skills, messages)
            continue

        text = (message.get('content') or '').strip()
        if text:
            text_clean = re.sub(r'<[^>]+>', '', text)
            print(f"\n[GENERATE] Ollama:\n{text_clean}\n")
            if ctx.interactive and ('?' in text or re.search(r'(?:option\s*\d|choice|choose|which|either|alternative|instead|\b or \b)', text, re.I)):
                user_input = input("[GENERATE] Your response (or 'done' to finish, 'abort' to cancel): ").strip()
                if user_input.lower() == 'abort':
                    print("[GENERATE] Aborted by user.")
                    sys.exit(1)
                if user_input.lower() == 'done':
                    print("[GENERATE] Complete.")
                    break
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": user_input})
                continue
            else:
                print("[GENERATE] No pending questions or tool calls. Assuming complete.")
                break
        else:
            print("[GENERATE] No response from Ollama.")
            break

    for spec_file in sorted(Path(ctx.workspace_dir).rglob("*.spec")):
        try:
            fmt_cmd = ["/usr/lib/obs/service/format_spec_file", str(spec_file.parent)]
            subprocess.run(fmt_cmd, capture_output=True, text=True, timeout=30)
            print(f"[GENERATE] format_spec_file: normalized {spec_file.name}")
        except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
            pass

