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
import time
import urllib.request
from pathlib import Path

from pbuild_ai.tools import execute_tool_calls


class OllamaAnalyzer:
    def __init__(self, host=None, model="gemma4", debug=False):
        self.host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self.model = model
        self.debug = debug
        self.api_url = f"{self.host}/api/generate"
        self.chat_api_url = f"{self.host}/api/chat"
        self._context = None
        self._chat_context = None
        self._opener = urllib.request.build_opener()
        self._opener.addheaders = [('Connection', 'keep-alive')]
        self.reset_stats()

    MAX_PROMPT_CHARS = 80000

    def reset_context(self):
        self._context = None
        self._chat_context = None

    def reset_stats(self):
        self.ai_calls = 0
        self.ai_time = 0.0

    def print_stats(self):
        print(f"\n[STATS] AI calls: {self.ai_calls}  |  AI time: {self.ai_time:.1f}s")

    def _request(self, url, payload):
        if payload.get("context") is None:
            payload.pop("context", None)
        t0 = time.time()
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        with self._opener.open(req) as response:
            raw = response.read().decode('utf-8')
        elapsed = time.time() - t0
        self.ai_calls += 1
        self.ai_time += elapsed
        if self.debug:
            print(f"[DEBUG] Ollama raw response:\n{raw}", flush=True)
        return json.loads(raw)

    def analyze(self, system_prompt, context_data, agents_md=None):
        context_data = (context_data or "")[:self.MAX_PROMPT_CHARS]
        if agents_md:
            agents_md = agents_md[:20000]
        full_prompt = f"{system_prompt}\n\nHere is the context:\n{context_data}"
        if agents_md:
            full_prompt += f"\n\n--- AGENTS.md ---\n{agents_md}"
        full_prompt = full_prompt[:self.MAX_PROMPT_CHARS]
        payload = {"model": self.model, "prompt": full_prompt, "stream": False}
        if self._context is not None:
            payload["context"] = self._context
        try:
            result = self._request(self.api_url, payload)
            self._context = result.get("context")
            return result.get('response', '').strip()
        except Exception as e:
            return f"[OLLAMA ERROR] {e}"

    def call_with_tools(self, messages, tools, manager, workspace_dir=None, allow_tool_scripts=False, max_rounds=5, interactive=False):
        all_results = []
        for round_idx in range(max_rounds):
            payload = {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "stream": False
            }
            if self._chat_context is not None:
                payload["context"] = self._chat_context
            try:
                result = self._request(self.chat_api_url, payload)
                self._chat_context = result.get("context")
            except Exception as e:
                return f"[OLLAMA ERROR] {e}"

            message = result.get('message', {})
            if 'tool_calls' not in message or not message['tool_calls']:
                text = (message.get('content') or '').strip()
                if text:
                    preview = text[:500].replace('\n', ' | ')
                    if self.debug:
                        print(f"[OLLAMA] No tool calls. Text response: {preview}", flush=True)
                if all_results:
                    return all_results
                return []

            round_calls = []
            for tc in message['tool_calls']:
                tool_name = tc['function']['name']
                raw_args = tc['function']['arguments']
                if isinstance(raw_args, dict):
                    tool_input = raw_args
                else:
                    tool_input = json.loads(raw_args)
                round_calls.append((tool_name, tool_input))

            # Interactive mode: let user select which tool calls to execute (only for modification ops)
            MODIFICATION_TOOLS = {"write_file", "run_tool_script"}
            mod_count = sum(1 for name, _ in round_calls if name in MODIFICATION_TOOLS)
            if interactive and mod_count > 1:
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
                if self.debug:
                    print(f"[OLLAMA] Tool call: {name}({args_preview})", flush=True)

            round_results = execute_tool_calls(round_calls, manager, workspace_dir or str(Path.cwd()), allow_tool_scripts, interactive=interactive)
            for r in round_results:
                if r.startswith("[Fetched "):
                    display = r.split("\n", 1)[0]
                else:
                    display = r[:500] + "..." if len(r) > 500 else r
                print(f"[FIX] {display}", flush=True)
            all_results.extend(f"{name}: {r}" for (name, _), r in zip(round_calls, round_results))

            messages.append({"role": "assistant", "content": message.get('content', ''), "tool_calls": message['tool_calls']})
            for (name, _), content in zip(round_calls, round_results):
                messages.append({"role": "tool", "content": str(content), "name": name})

        print(f"[OLLAMA] Reached max rounds ({max_rounds}).", flush=True)
        return all_results
