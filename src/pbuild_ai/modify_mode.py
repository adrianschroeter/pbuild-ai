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
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

from pbuild_ai.network import is_safe_url
from pbuild_ai.ollama_client import chat_completion
from pbuild_ai.tools import execute_tool_calls


def _expand_url_macros(url, spec_content):
    """Expand RPM macros like %{name}, %{version} in a URL using spec file values."""
    macros = {}
    for m in re.finditer(r'^(Name|Version):\s*(\S+)', spec_content, re.MULTILINE):
        macros[m.group(1).lower()] = m.group(2)
    if not macros:
        return url
    expanded = url
    for key, val in macros.items():
        expanded = expanded.replace(f'%{{{key}}}', val)
        expanded = expanded.replace(f'%{{upper:{key}}}', val.upper())
    return expanded


def _resolve_url_references(ctx):
    """Scan spec files for Source:/Patch: lines containing remote URLs,
    download the content, and rewrite the lines to reference local files.

    This prevents build failures when the build environment has no network
    access -- all sources and patches must be stored locally in git.
    """
    for spec in ctx.spec_files:
        content = ctx.manager.read_file_safe(spec)
        if not content:
            continue

        lines = content.split('\n')
        modified = 0

        for i, line in enumerate(lines):
            m = re.match(r'^(Source\d*|Patch\d*):\s+(https?://\S+)', line.strip(), re.I)
            if not m:
                continue
            tag = m.group(1)
            url = m.group(2).rstrip()

            safe, _ = is_safe_url(url)
            if not safe:
                print(f"[MODIFY] Skipping unsafe URL in {spec.name}:{i+1}: {url}")
                continue

            from urllib.parse import urlparse
            parsed = urlparse(url)
            fname = Path(parsed.path).name
            if not fname:
                fname = f"from_{tag.lower()}.patch"

            fname_expanded = _expand_url_macros(fname, content)
            local_path = spec.parent / fname_expanded
            if not local_path.exists():
                download_url = _expand_url_macros(url, content)
                print(f"[MODIFY] Downloading {download_url} -> {fname_expanded}")
                try:
                    req = urllib.request.Request(download_url, headers={"User-Agent": "pbuild-ai/1.0"})
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        data = resp.read()
                    local_path.write_bytes(data)
                    print(f"[MODIFY] Downloaded {len(data)} bytes to {fname_expanded}")
                except Exception as e:
                    print(f"[MODIFY] Failed to download {download_url}: {e}")
                    continue
            else:
                print(f"[MODIFY] {fname_expanded} already exists, skipping download.")

            # Preserve inline comment after the URL (including leading space)
            suffix = ""
            comment_pos = line.find('#')
            if comment_pos > 0:
                before_hash = line[comment_pos - 1]
                suffix = (line[comment_pos - 1:] if before_hash == ' '
                          else line[comment_pos:])
            lines[i] = f"{tag}: {fname_expanded}{suffix}"
            modified += 1

        if modified:
            spec.write_text('\n'.join(lines))
            print(f"[MODIFY] Updated {spec.name}: {modified} remote URL(s) resolved to local files.")


def run_modify_mode(ctx):
    """Hand sources + prompt to Ollama, apply changes locally, then exit (no build)."""
    for spec in ctx.spec_files:
        _ctx_file = Path(ctx.workspace_dir) / ".pai.context"

        # Load saved context for the same spec
        saved_messages = None
        if _ctx_file.exists():
            try:
                _saved = json.loads(_ctx_file.read_text())
                if _saved.get("spec_path") == str(spec.relative_to(ctx.workspace_dir)):
                    print(f"[MODIFY] Loaded saved context from {_ctx_file.name}")
                    saved_messages = _saved.get("messages", [])
                else:
                    print(f"[MODIFY] Stale context (for {_saved.get('spec_path')}), discarding.")
                    _ctx_file.unlink()
            except Exception as e:
                print(f"[MODIFY] Corrupt context file: {e}")
                _ctx_file.unlink()

        skills = ctx.skill_manager.get_skills_for(spec.name, ctx.manager.read_file_safe(spec), prompt=ctx.modify_prompt)
        if skills:
            for s in skills:
                print(f"[INFO] Using skill profile: {s.__name__}")
            prompt_parts = [getattr(s, 'OLLAMA_SPEC_PROMPT', '') for s in skills if getattr(s, 'OLLAMA_SPEC_PROMPT', '')]
            spec_prompt = "\n\n".join(prompt_parts) if prompt_parts else ctx.default_spec_prompt
        else:
            spec_prompt = ctx.default_spec_prompt
        print(f"\n[MODIFY] Sending {spec.name} sources to {ctx.ollama.model}...")
        spec_content = ctx.manager.read_file_safe(spec)
        hint = f"\n\n--- User Hint (prefer this over generic analysis) ---\n{ctx.prompt_hint}" if ctx.prompt_hint else ""
        system_content = f"""You are an RPM packager assistant. The user wants you to modify a spec file based on their request.

The spec file content is ALREADY provided below in the user message. Do NOT call read_file — the content is right here.

To make changes, prefer edit_file for small targeted changes — it replaces only the matching text and preserves all other lines. IMPORTANT: when using edit_file, include enough surrounding lines (full target line + 1-2 lines before/after) so old_string matches EXACTLY ONE location. Use write_file only for large rewrites or new files. IMPORTANT: write_file writes the ENTIRE file — you must include ALL lines. PRESERVE EVERY LINE YOU ARE NOT CHANGING VERBATIM; do not add, remove, or modify anything beyond the specific change. Keep in mind that your changes need to be reviewed. So keep changes minimal unless stated otherwise. If you are unsure or need to choose between options, ask the user by responding with your question — you will get their answer in the next round.

User request: {ctx.modify_prompt}{hint}

Skill instructions (follow these):
{spec_prompt}"""

        if saved_messages:
            messages = [{"role": "system", "content": system_content}] + (saved_messages[1:] if len(saved_messages) > 1 else [])
            messages.append({"role": "user", "content": f"Continuing from previous session. Current spec content (full file — do NOT call read_file for the spec):\n{spec_content}\n\nApply remaining changes."})
        else:
            messages = [
                {"role": "system", "content": system_content},
                {"role": "user", "content": f"Spec file path: {spec.relative_to(ctx.workspace_dir)}\n\nCurrent content (full file — do NOT call read_file for the spec):\n{spec_content}\n\nDo NOT explain. Do NOT ask questions. Apply the changes using write_file or edit_file NOW."}
            ]
        modify_max_rounds = 20
        changes_made = False
        for round_idx in range(modify_max_rounds):
            result = chat_completion(ctx.ollama, messages, ctx.tools, debug=ctx.debug, track_stats=True)

            message = result.get('message', {})
            if 'tool_calls' in message and message['tool_calls']:
                round_calls = []
                for tc in message['tool_calls']:
                    tool_name = tc['function']['name']
                    raw_args = tc['function']['arguments']
                    tool_input = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
                    if tool_name == 'write_file':
                        wf_path = Path(tool_input.get('path', ''))
                        if wf_path.name == spec.name and spec.resolve() != (Path(ctx.workspace_dir) / wf_path).resolve():
                            correct = str(spec.relative_to(ctx.workspace_dir))
                            print(f"[MODIFY] Remapped write_file path: {wf_path} \u2192 {correct}")
                            tool_input['path'] = correct
                    round_calls.append((tool_name, tool_input))

                if ctx.interactive and sum(1 for name, _ in round_calls if name in ("write_file", "edit_file", "remove_file", "rename_file", "run_tool_script")) > 1:
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
                    args_preview = json.dumps(tool_input)[:300]
                    if ctx.debug:
                        print(f"[AI] Tool call: {name}({args_preview})", flush=True)
                try:
                    round_results = execute_tool_calls(round_calls, ctx.manager, ctx.workspace_dir, ctx.allow_tool_scripts, interactive=ctx.interactive, debug=ctx.debug)
                except Exception as e:
                    round_results = [f"Error executing tool: {e}"]
                    print(f"[MODIFY TOOL ERROR] {e}")
                for (name, inp), r in zip(round_calls, round_results):
                    if name == "read_file":
                        line_count = r.count('\n')
                        display = f"read_file: {inp.get('path', '?')} ({line_count} lines)"
                    elif r.startswith("[Fetched "):
                        display = r.split("\n", 1)[0]
                    else:
                        display = r[:500] + "..." if len(r) > 500 else r
                    print(f"[MODIFY] {display}", flush=True)
                    if spec.name in r and (r.startswith("OK: Wrote ") or r.startswith("OK: Edited ") or r.startswith("OK: Removed ") or r.startswith("OK: Renamed ")):
                        changes_made = True
                messages.append({"role": "assistant", "content": message.get('content', ''), "tool_calls": message['tool_calls']})
                for (name, _), content in zip(round_calls, round_results):
                    if name == "read_file" and isinstance(content, str) and len(content) > 2000:
                        content = content[:1000] + "\n... (truncated) ...\n" + content[-900:]
                    messages.append({"role": "tool", "content": str(content), "name": name})
                continue

            text = (message.get('content') or '').strip()
            if text:
                text_clean = re.sub(r'<[^>]+>', '', text)
                print(f"\n[MODIFY] Ollama:\n{text_clean}\n")
                if ctx.interactive and ('?' in text or re.search(r'(?:option\s*\d|choice|choose|which|either|alternative|instead|\b or \b)', text, re.I)):
                    user_input = input("[MODIFY] Your response (or 'done' to accept, 'abort' to cancel): ").strip()
                    if user_input.lower() == 'abort':
                        print("[MODIFY] Aborted by user.")
                        sys.exit(1)
                    if user_input.lower() == 'done':
                        print("[MODIFY] Changes applied.")
                        break
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": user_input})
                    continue
                else:
                    if changes_made:
                        print("[MODIFY] Changes confirmed.")
                        break
                    print("[MODIFY] No tool calls. Changes not applied.")
                    break
            else:
                print("[MODIFY] No response from Ollama.")
                break

        # Save context on exhaustion, delete on success
        if changes_made:
            _resolve_url_references(ctx)
            if _ctx_file.exists():
                _ctx_file.unlink()
                print(f"[MODIFY] Removed saved context ({_ctx_file.name}) after successful changes.")
        elif len(messages) > 1:
            save_data = {
                "version": 1,
                "mode": "modify",
                "spec_path": str(spec.relative_to(ctx.workspace_dir)),
                "messages": messages,
                "spec_content": spec_content,
                "modify_prompt": ctx.modify_prompt,
                "timestamp": time.time(),
            }
            _ctx_file.write_text(json.dumps(save_data, indent=2))
            print(f"[MODIFY] Saved conversation context to {_ctx_file.name} for restart.")


