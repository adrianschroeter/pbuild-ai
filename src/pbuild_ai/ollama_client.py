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
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from pbuild_ai.utils import resolve_path

from pbuild_ai.spinner import Spinner, CYAN
from pbuild_ai.tools import execute_tool_calls


class OllamaAnalyzer:
    def __init__(self, host=None, model="default", debug=False, timeout=None):
        self.host = (host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip('/')
        self.model = model
        self.debug = debug
        self.timeout = timeout if timeout is not None else int(os.environ.get("OLLAMA_TIMEOUT", "900"))
        self.api_url = f"{self.host}/api/generate"
        self.chat_api_url = f"{self.host}/api/chat"
        self._context = None
        self._chat_context = None
        self._opener = urllib.request.build_opener()
        self._opener.addheaders = [('Connection', 'keep-alive')]
        self._chat_supported = True
        self.manager = None
        self.reset_stats()

    MAX_PROMPT_CHARS = 80000

    def reset_context(self):
        self._context = None
        self._chat_context = None

    def reset_stats(self):
        self.ai_calls = 0
        self.ai_time = 0.0

    def print_stats(self, manager=None, program_start=None, skill_manager=None):
        if skill_manager is not None and skill_manager.activated_skills:
            print(f"[STATS] Skills used: {', '.join(sorted(skill_manager.activated_skills))}")
        parts = [f"[STATS] AI model: {self.model}  |  AI calls: {self.ai_calls}  |  AI time: {self.ai_time:.1f}s"]
        if manager is not None:
            parts.append(f"pbuild calls: {manager.pbuild_calls}  |  pbuild time: {manager.pbuild_time:.1f}s")
        if program_start is not None:
            total = time.time() - program_start
            parts.append(f"total runtime: {total:.1f}s")
        print("  |  ".join(parts))

    def _chat_to_generate_payload(self, messages):
        """Convert chat-format messages to /api/generate payload (system + prompt, no tools)."""
        system = ''
        prompt_parts = []
        for msg in messages:
            role = msg.get('role', '')
            content = msg.get('content', '')
            if role == 'system' and content:
                system += content + '\n'
            elif content:
                prompt_parts.append(f"{role}: {content}")
        return {
            "model": self.model,
            "system": system.strip() or None,
            "prompt": '\n'.join(prompt_parts) if prompt_parts else '.',
            "stream": False
        }

    def _request(self, url, payload):
        if payload.get("context") is None:
            payload.pop("context", None)
        if self.debug:
            payload_preview = json.dumps(payload)
            print(f"[DEBUG] Ollama request: {url} ({len(payload_preview)} bytes payload, model={payload.get('model', '?')})", flush=True)
        t0 = time.time()
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        try:
            model_name = payload.get('model', self.model)
            with Spinner(prefix=f"[AI] {model_name}", color=CYAN):
                with self._opener.open(req, timeout=self.timeout) as response:
                    raw = response.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')[:2000] if e.fp else ''
            if self.debug:
                print(f"[DEBUG] Ollama HTTP {e.code} response body:\n{body}", flush=True)
            raise RuntimeError(f"HTTP Error {e.code}: {e.reason} — {body}") from e
        except OSError as e:
            if self.debug:
                print(f"[DEBUG] Ollama request failed (will retry once): {e}", flush=True)
            time.sleep(2)
            try:
                with self._opener.open(req, timeout=self.timeout) as response:
                    raw = response.read().decode('utf-8')
            except urllib.error.HTTPError as e2:
                body2 = e2.read().decode('utf-8', errors='replace')[:2000] if e2.fp else ''
                raise RuntimeError(f"HTTP Error {e2.code}: {e2.reason} — {body2}") from e2
            except OSError as e2:
                raise RuntimeError(
                    f"Ollama connection failed after retry ({self.timeout}s timeout): {e2}"
                ) from e2
        elapsed = time.time() - t0
        self.ai_calls += 1
        self.ai_time += elapsed
        if self.debug:
            print(f"[DEBUG] Ollama raw response ({len(raw)} bytes, {elapsed:.1f}s):\n{raw}", flush=True)
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
            response_text = result.get('response', '').strip()
            return response_text
        except Exception as e:
            print(f"[OLLAMA ERROR] {e}")
            sys.exit(2)

    def _write_analysis_file(self, response_text):
        if self.manager and hasattr(self.manager, '_last_log_path') and self.manager._last_log_path:
            analyze_path = Path(str(self.manager._last_log_path) + ".analyze")
            analyze_path.parent.mkdir(parents=True, exist_ok=True)
            analyze_path.write_text(response_text, encoding='utf-8')
            print(f"[BUILD LOG] Wrote {len(response_text)} bytes to {analyze_path}")

    def call_with_tools(self, messages, tools, manager, workspace_dir=None, allow_tool_scripts=False, max_rounds=15, interactive=False):
        max_rounds = max_rounds if max_rounds > 0 else 999999
        all_results = []
        for round_idx in range(max_rounds):
            if self._chat_supported:
                payload = {
                    "model": self.model,
                    "messages": messages,
                    "tools": tools,
                    "stream": False
                }
                if self._chat_context is not None:
                    payload["context"] = self._chat_context
            else:
                payload = self._chat_to_generate_payload(messages)
            try:
                result = self._request(
                    self.chat_api_url if self._chat_supported else self.api_url,
                    payload
                )
                if self._chat_supported:
                    self._chat_context = result.get("context")
            except RuntimeError as e:
                if "HTTP Error 405" in str(e) and self._chat_supported:
                    print(f"[INFO] Chat API not supported at {self.chat_api_url}, falling back to {self.api_url}")
                    self._chat_supported = False
                    payload = self._chat_to_generate_payload(messages)
                    try:
                        result = self._request(self.api_url, payload)
                    except Exception as e2:
                        print(f"[OLLAMA ERROR] {e2}")
                        sys.exit(2)
                else:
                    print(f"[OLLAMA ERROR] {e}")
                    sys.exit(2)
            except Exception as e:
                print(f"[OLLAMA ERROR] {e}")
                sys.exit(2)

            if not self._chat_supported:
                # /api/generate returns text in 'response', no tool calls
                text = result.get('response', '').strip()
                if text:
                    print(f"[FIX] {text[:500]}", flush=True)
                    all_results.append(text)
                return all_results

            message = result.get('message', {})
            if 'tool_calls' not in message or not message['tool_calls']:
                text = (message.get('content') or '').strip()
                if text:
                    preview = text[:500].replace('\n', ' | ')
                    if self.debug:
                        print(f"[AI] No tool calls. Text response: {preview}", flush=True)
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
            MODIFICATION_TOOLS = {"write_file", "edit_file", "remove_file", "rename_file", "run_tool_script"}
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
                    print(f"[AI] Tool call: {name}({args_preview})", flush=True)

            round_results = execute_tool_calls(round_calls, manager, workspace_dir or str(Path.cwd()), allow_tool_scripts, interactive=interactive, debug=self.debug)
            for (name, inp), r in zip(round_calls, round_results):
                if name == "read_file":
                    line_count = r.count('\n')
                    display = f"read_file: {inp.get('path', '?')} ({line_count} lines)"
                elif name in ("list_archive", "list_files"):
                    continue
                elif name == "read_file_from_archive":
                    if not self.debug:
                        continue
                    display = r[:500] + "..." if len(r) > 500 else r
                elif r.startswith("[Fetched "):
                    display = r.split("\n", 1)[0]
                else:
                    display = r[:500] + "..." if len(r) > 500 else r
                print(f"[FIX] {display}", flush=True)
            all_results.extend(f"{name}: {r}" for (name, _), r in zip(round_calls, round_results))

            messages.append({"role": "assistant", "content": message.get('content', ''), "tool_calls": message['tool_calls']})
            _injected_edit_help = False
            for (name, inp), content in zip(round_calls, round_results):
                if name == "read_file" and isinstance(content, str) and len(content) > 2000:
                    content = content[:1000] + "\n... (truncated) ...\n" + content[-900:]
                messages.append({"role": "tool", "content": str(content), "name": name})
                if not _injected_edit_help and name == "edit_file" and ("old_string not found" in str(content) or "old_string found" in str(content)):
                    _path = inp.get("path", "")
                    _resolved = resolve_path(_path, workspace_dir) if workspace_dir else None
                    if _resolved and _resolved.exists():
                        try:
                            _file_content = manager.read_file_safe(_resolved)
                            messages.append({"role": "user", "content": f"The edit_file call for {_path} failed. Here is the current content of {_path}:\n\n```\n{_file_content[:8000]}\n```\nAnalyze the content and retry the edit with the correct old_string."})
                            _injected_edit_help = True
                            print(f"[FIX] Injected file content to help edit_file retry for {_path}", flush=True)
                        except Exception:
                            pass

        print(f"[AI] Reached max rounds ({max_rounds}).", flush=True)
        return all_results


def chat_completion(ollama, messages, tools, debug=False, track_stats=False):
    """Send a non-streaming chat completion request with retry on transient errors
    and empty responses. Returns the parsed result dict.
    On HTTP/protocol errors or after 3 failed attempts, prints diagnostic info
    and calls sys.exit(2)."""
    payload = {"model": ollama.model, "messages": messages, "tools": tools, "stream": False}
    data_bytes = json.dumps(payload).encode('utf-8')
    for attempt in range(3):
        try:
            _t0 = time.time()
            req = urllib.request.Request(
                ollama.chat_api_url,
                data=data_bytes,
                headers={'Content-Type': 'application/json'},
            )
            with urllib.request.urlopen(req, timeout=ollama.timeout) as resp:
                raw = resp.read().decode('utf-8')
            if debug:
                print(f"[DEBUG] Ollama response ({len(raw)} bytes):\n{raw}", flush=True)
            result = json.loads(raw)
            if track_stats:
                ollama.ai_calls += 1
                ollama.ai_time += time.time() - _t0
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')[:2000] if e.fp else ''
            print(f"[OLLAMA ERROR] HTTP {e.code}: {e.reason} - {body}")
            sys.exit(2)
        except OSError as e:
            if attempt < 2:
                print(f"[OLLAMA] Transient error (retry {attempt+2}/3): {e}", flush=True)
                time.sleep(2)
                continue
            print(f"[OLLAMA ERROR] {e}")
            sys.exit(2)
        except Exception as e:
            print(f"[OLLAMA ERROR] {e}")
            sys.exit(2)

        message = result.get('message', {})
        if message.get('content', '').strip() or message.get('tool_calls'):
            return result

        if attempt < 2:
            print(f"[OLLAMA] Empty response (retry {attempt+2}/3, message keys: "
                  f"{list(message.keys())})...")
            time.sleep(2)
        else:
            print(f"[OLLAMA ERROR] Empty response after 3 attempts. "
                  f"Message keys: {list(message.keys())}. "
                  f"Content: {message.get('content')!r}")
            sys.exit(2)
