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

from pbuild_ai.tools import execute_tool_calls


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

You have these tools:
- edit_file(path, old_string, new_string): targeted search-and-replace (PREFER this for small changes)
- write_file(path, content): write a file (use only for large rewrites or new files)
- read_file(path): read a file
- web_fetch(url): fetch an HTTPS URL to research upstream sources
- git_command(command): run a git command
- ask_user(question): ask the user a clarifying question

Follow these rules:
1. Research the upstream project first using web_fetch if a URL is provided or you can infer one, then create the package. Do NOT fetch the same URL more than once — the result is cached.
2. For GitHub projects, use https://api.github.com/repos/OWNER/REPO/releases/latest and https://api.github.com/repos/OWNER/REPO/tags to find release versions and tarball URLs instead of the main HTML page. For specific tags, use https://github.com/OWNER/REPO/archive/refs/tags/TAG.tar.gz.
2. Only call ask_user if the specification is truly missing critical information (e.g., no project name, no source URL, no license hint, and you cannot determine it from research). Do NOT ask generic questions.
3. Create the package in a subdirectory named after the package (e.g., workspace_root/package-name/).
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
7. When you are done, tell the user what you created.
8. Do NOT use HTML or markdown formatting in your text responses — use plain text only. No <b>, <a>, <pre>, or any other tags.

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
    for round_idx in range(generate_max_rounds):
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
            print(f"[GENERATE ERROR] {e}")
            break

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
                    print(f"[OLLAMA] Tool call: {name}({args_preview})", flush=True)
            try:
                round_results = execute_tool_calls([(n, i) for n, i in round_calls if n != "_skip"], ctx.manager, ctx.workspace_dir, ctx.allow_tool_scripts, interactive=ctx.interactive)
            except Exception as e:
                round_results = [f"Error executing tool: {e}"]
                print(f"[GENERATE TOOL ERROR] {e}")
            final_results = []
            cache_idx = 0
            for name, inp in round_calls:
                if name == "_skip":
                    final_results.append(inp["_cached"])
                else:
                    if name == "web_fetch" and cache_idx < len(round_results) and round_results[cache_idx].startswith("[Fetched "):
                        url = inp["url"]
                        fetch_cache[url] = round_results[cache_idx]
                    final_results.append(round_results[cache_idx])
                    cache_idx += 1
            round_results = final_results
            for (name, inp), r in zip(round_calls, round_results):
                if name == "read_file":
                    line_count = r.count('\n')
                    display = f"read_file: {inp.get('path', '?')} ({line_count} lines)"
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
                messages.append({"role": "tool", "content": str(content), "name": tool_name})
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
