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
import sys
import urllib.request
from pathlib import Path

from pbuild_ai.tools import execute_tool_calls


def run_modify_mode(ctx):
    """Hand sources + prompt to Ollama, apply changes locally, then exit (no build)."""
    for spec in ctx.spec_files:
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
        messages = [
            {"role": "system", "content": f"""You are an RPM packager assistant. The user wants you to modify a spec file based on their request.

The spec file content is ALREADY provided below in the user message. Do NOT call read_file — the content is right here.

You have these tools: edit_file, write_file, read_file, web_fetch, git_command.

To make changes, prefer edit_file for small targeted changes — it replaces only the matching text and preserves all other lines. Use write_file only for large rewrites or new files. IMPORTANT: write_file writes the ENTIRE file — you must include ALL lines. PRESERVE EVERY LINE YOU ARE NOT CHANGING VERBATIM; do not add, remove, or modify anything beyond the specific change. Keep in mind that your changes need to be reviewed. So keep changes minimal unless stated otherwise. If you are unsure or need to choose between options, ask the user by responding with your question — you will get their answer in the next round.

User request: {ctx.modify_prompt}{hint}

Skill instructions (follow these):
{spec_prompt}"""},
            {"role": "user", "content": f"Spec file path: {spec.relative_to(ctx.workspace_dir)}\n\nCurrent content:\n{spec_content[:5000]}\n\nDo NOT explain. Do NOT ask questions. Apply the changes using write_file or edit_file NOW."}
        ]
        modify_max_rounds = 20
        changes_made = False
        for round_idx in range(modify_max_rounds):
            payload = {
                "model": ctx.ollama.model,
                "messages": messages,
                "tools": ctx.tools,
                "stream": False
            }
            try:
                req = urllib.request.Request(
                    ctx.ollama.chat_api_url,
                    data=json.dumps(payload).encode('utf-8'),
                    headers={'Content-Type': 'application/json'}
                )
                with urllib.request.urlopen(req) as resp:
                    raw = resp.read().decode('utf-8')
                    if ctx.debug:
                        print(f"[DEBUG] Ollama raw response:\n{raw}", flush=True)
                    result = json.loads(raw)
            except Exception as e:
                print(f"[MODIFY ERROR] {e}")
                break

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
                        print(f"[OLLAMA] Tool call: {name}({args_preview})", flush=True)
                try:
                    round_results = execute_tool_calls(round_calls, ctx.manager, ctx.workspace_dir, ctx.allow_tool_scripts, interactive=ctx.interactive)
                except Exception as e:
                    round_results = [f"Error executing tool: {e}"]
                    print(f"[MODIFY TOOL ERROR] {e}")
                for r in round_results:
                    if r.startswith("[Fetched "):
                        display = r.split("\n", 1)[0]
                    else:
                        display = r[:500] + "..." if len(r) > 500 else r
                    print(f"[MODIFY] {display}", flush=True)
                    if spec.name in r and (r.startswith("OK: Wrote ") or r.startswith("OK: Edited ") or r.startswith("OK: Removed ") or r.startswith("OK: Renamed ")):
                        changes_made = True
                messages.append({"role": "assistant", "content": message.get('content', ''), "tool_calls": message['tool_calls']})
                for (name, _), content in zip(round_calls, round_results):
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
