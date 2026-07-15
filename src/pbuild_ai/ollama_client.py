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

import difflib
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from pbuild_ai.spinner import Spinner, AI_COLOR
from pbuild_ai.utils import resolve_path, ReadCoverageTracker
from pbuild_ai.tools import execute_tool_calls, format_tool_display


def prune_messages(messages, keep_rounds=2):
    """Keep system prompt and the last N assistant rounds (each round = assistant
    message + its tool result messages). Older messages are dropped to limit
    context size and avoid re-tokenizing the full conversation history on
    every round.

    Returns the pruned list. Does NOT modify the input list in place.
    """
    if len(messages) <= 3:
        return messages[:]

    # Find positions of all assistant messages (with or without tool_calls)
    assistant_positions = [
        i for i, m in enumerate(messages) if m.get('role') == 'assistant'
    ]

    if len(assistant_positions) <= keep_rounds:
        return messages[:]

    keep_from = assistant_positions[-keep_rounds]

    # Always keep system message
    preserved = [messages[0]]
    # Keep original user message if present and before the cutoff
    for m in messages[1:keep_from]:
        if m.get('role') == 'user' and not m.get('tool_call_id'):
            preserved.append(m)
            break

    # Add everything from the cutoff point onwards
    preserved.extend(messages[keep_from:])

    return preserved


class OllamaAnalyzer:
    def __init__(self, host=None, model="default", debug=False, timeout=None, options=None):
        self.host = (host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip('/')
        self.model = model
        self.debug = debug
        self.timeout = timeout if timeout is not None else int(os.environ.get("OLLAMA_TIMEOUT", "900"))
        self.options = options or {}
        self.api_url = f"{self.host}/api/generate"
        self.chat_api_url = f"{self.host}/api/chat"
        self._context = None
        self._chat_context = None
        self._opener = urllib.request.build_opener()
        self._opener.addheaders = [('Connection', 'keep-alive')]
        self._chat_supported = True
        self.manager = None
        self._changed_files: set[str] = set()
        self.reset_stats()

        if self.options.get("num_ctx"):
            self.max_tokens = int(self.options["num_ctx"])
        else:
            self.max_tokens = self._fetch_default_num_ctx() or 32768

    @staticmethod
    def _estimate_tokens(text):
        return max(1, len(text) // 4)

    def _fetch_default_num_ctx(self):
        try:
            payload = json.dumps({"model": self.model}).encode()
            req = urllib.request.Request(
                f"{self.host}/api/show",
                data=payload,
                headers={"Content-Type": "application/json"}
            )
            with self._opener.open(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            for key, value in data.get("model_info", {}).items():
                if key.endswith(".context_length"):
                    return int(value)
        except Exception:
            pass
        return None

    def count_tokens(self, text):
        if not text:
            return 0
        try:
            payload = json.dumps({"model": self.model, "content": text}).encode()
            req = urllib.request.Request(
                f"{self.host}/api/tokenize",
                data=payload,
                headers={"Content-Type": "application/json"}
            )
            with self._opener.open(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            return len(data.get("tokens", []))
        except Exception:
            return self._estimate_tokens(text)

    def reset_context(self):
        self._context = None
        self._chat_context = None

    def _apply_options_and_format(self, payload, tool_calling=False):
        """Inject Ollama options and format into payload.

        By default format=json is sent on every /api/generate request.
        Tool-calling requests (/api/chat with tools) skip format=json
        because it conflicts with the native tool_calls response format.

        Models can opt out entirely by setting format=text in their config
        (e.g. via models.yaml or --ollama-option format=text).
        The 'format' key is removed from options since it is not a model
        option — it is a top-level field.
        """
        opts = self.options.copy() if self.options else {}
        fmt = opts.pop("format", None)
        if fmt == "text":
            pass
        elif not tool_calling:
            payload["format"] = "json"
        if opts:
            payload["options"] = opts
        return payload

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
        payload = {
            "model": self.model,
            "system": system.strip() or None,
            "prompt": '\n'.join(prompt_parts) if prompt_parts else '.',
            "stream": False,
        }
        self._apply_options_and_format(payload)
        return payload

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
            _psize = payload.get("prompt")
            if _psize:
                _tok = self.count_tokens(_psize)
                _ctx_str = f" ({_tok//1024}k/{self.max_tokens//1024}k tok)"
            else:
                _ctx_str = ""
            with Spinner(prefix=f"[AI] {model_name}{_ctx_str}", color=AI_COLOR):
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
        _char_limit = self.max_tokens * 6
        context_data = (context_data or "")[:_char_limit]
        if agents_md:
            agents_md = agents_md[:20000]
        full_prompt = f"{system_prompt}\n\nHere is the context:\n{context_data}"
        if agents_md:
            full_prompt += f"\n\n--- AGENTS.md ---\n{agents_md}"
        full_prompt = full_prompt[:_char_limit]
        payload = {"model": self.model, "prompt": full_prompt, "stream": False}
        self._apply_options_and_format(payload)
        self._context = None
        try:
            result = self._request(self.api_url, payload)
            self._context = result.get("context")
            response_text = result.get('response', '').strip()
            # Some thinking/CoT models (e.g. qwen3.6) put the JSON output
            # in the 'thinking' field when format=json is active.
            if not response_text and result.get('thinking'):
                response_text = result.get('thinking', '').strip()
            # When format=json is sent, the model outputs JSON text.
            # Try to extract meaningful content from the JSON structure.
            if payload.get("format") == "json" and response_text:
                try:
                    parsed = json.loads(response_text)
                    if isinstance(parsed, dict):
                        texts = [v for v in parsed.values() if isinstance(v, str) and len(v) > 20]
                        if texts:
                            response_text = max(texts, key=len)
                except (json.JSONDecodeError, ValueError):
                    pass
            # If the response is empty (or only contains empty JSON), show a clean message
            if not response_text:
                response_text = "(model returned empty response)"
            else:
                try:
                    parsed = json.loads(response_text)
                    if isinstance(parsed, dict) and not any(v for v in parsed.values() if isinstance(v, str) and v.strip()):
                        response_text = "(model returned empty response)"
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
            return response_text
        except Exception as e:
            print(f"[OLLAMA ERROR] {e}")
            sys.exit(2)

    def _write_analysis_file(self, response_text):
        if self.manager and hasattr(self.manager, '_last_log_path') and self.manager._last_log_path:
            base = str(self.manager._last_log_path)
            analyze_path = Path(base + '.analyze')
            analyze_path.parent.mkdir(parents=True, exist_ok=True)
            filtered = self._strip_spec_from_analysis(response_text)
            analyze_path.write_text(filtered, encoding='utf-8')
            print(f"[BUILD LOG] Wrote {len(filtered)} bytes to {analyze_path}")

    def _add_changed_file(self, abs_or_rel_path):
        """Track a file modified outside call_with_tools (e.g. via spec rewrite)."""
        if self.manager and hasattr(self.manager, 'base_dir'):
            try:
                p = Path(abs_or_rel_path)
                if p.is_absolute():
                    p = p.relative_to(self.manager.base_dir)
                self._changed_files.add(str(p))
            except ValueError:
                pass

    def _write_tool_changes(self, before_contents=None):
        """Write .tool_changes file next to the current build log with diffs of
        every source file the LLM modified this round. Only writes when files
        were actually changed (self._changed_files is non-empty).

        before_contents: optional dict[str, str] mapping relative file paths to
        their content BEFORE edits — used to generate proper diffs for untracked
        files (instead of showing the entire file as new via git diff --no-index)."""
        if not (self.manager and hasattr(self.manager, '_last_log_path') and self.manager._last_log_path):
            return
        base = str(self.manager._last_log_path)
        diff_path = Path(base + '.tool_changes')
        try:
            ws = self.manager.base_dir if hasattr(self.manager, 'base_dir') else None
            if not ws:
                return

            def _is_source_file(path: str) -> bool:
                parts = path.split('/')
                if any(p.startswith('_build.') or p == 'results' or p == '.pc'
                       for p in parts):
                    return False
                filename = parts[-1]
                if filename.startswith('results.') or filename == 'benchmark-report.html':
                    return False
                if any(path.endswith(ext) for ext in ('.log', '.analyze', '.pai.context')):
                    return False
                return True

            if not self._changed_files:
                print("[DIFF_DEBUG] no files changed by LLM this round")
                return

            changed = [f for f in self._changed_files if _is_source_file(f)]
            excluded = [f for f in self._changed_files if not _is_source_file(f)]
            if excluded:
                print(f"[DIFF_DEBUG] filtered out (not source): {excluded}")
            if not changed:
                print("[DIFF_DEBUG] all changed files are non-source — no diff")
                return

            before_contents = before_contents or {}

            def _content_based_diff(rel_path):
                """Generate a unified diff from before/after content snapshots."""
                before = before_contents.get(rel_path)
                if before is None:
                    return None
                abs_path = Path(ws) / rel_path
                try:
                    after = abs_path.read_text(encoding='utf-8')
                except Exception:
                    return None
                if before == after:
                    return None
                lines = list(difflib.unified_diff(
                    before.splitlines(),
                    after.splitlines(),
                    fromfile=rel_path,
                    tofile=rel_path,
                ))
                if not lines:
                    return None
                return '\n'.join(line.rstrip('\n') for line in lines) + '\n'

            diff_parts = []
            for f in sorted(changed):
                # 1) Try git diff (works for tracked files)
                r = subprocess.run(
                    ["git", "diff", "--", f],
                    cwd=ws, capture_output=True, text=True, timeout=30
                )
                if r.returncode == 0 and r.stdout.strip():
                    diff_parts.append(r.stdout)
                    continue
                # 2) Try git diff --staged
                r = subprocess.run(
                    ["git", "diff", "--staged", "--", f],
                    cwd=ws, capture_output=True, text=True, timeout=30
                )
                if r.returncode == 0 and r.stdout.strip():
                    diff_parts.append(r.stdout)
                    continue
                # 3) Try content-based diff (for untracked files with before snapshot)
                cd = _content_based_diff(f)
                if cd is not None:
                    diff_parts.append(cd)
                    continue
                # 4) Last resort: show as new file (untracked, no before snapshot)
                r = subprocess.run(
                    ["git", "diff", "--no-index", "/dev/null", f],
                    cwd=ws, capture_output=True, text=True, timeout=30
                )
                if r.returncode in (0, 1) and r.stdout.strip():
                    diff_parts.append(r.stdout)

            if diff_parts:
                diff_text = '\n'.join(p.rstrip('\n') for p in diff_parts) + '\n'
                diff_path.write_text(diff_text, encoding='utf-8')
                print(f"[BUILD LOG] Wrote {len(diff_text)} bytes to {diff_path}")
                self._changed_files.clear()
            else:
                print("[DIFF_DEBUG] no diffs could be generated for changed files")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    @staticmethod
    def _strip_spec_from_analysis(text):
        """Remove embedded spec file content from analysis text, keeping only the analysis."""
        if not text:
            return text
        # Check if the entire response IS a spec file (no analysis at all)
        _first = next((l.strip() for l in text.split('\n') if l.strip()), '')
        _has_analysis_markers = any(m in text for m in (
            '### Error', '### Solution', '### Root Cause', '### Fix',
            '**Error**', '**Fix**', '**Solution**', '**Root Cause**',
            'Error Analysis', 'Error Cause', 'root cause',
        ))
        _looks_like_spec = _first.startswith('#') and 'Name:' in text and 'Version:' in text and (
            '%prep' in text or '%build' in text or '%install' in text or '%files' in text or '%description' in text
        )
        if _looks_like_spec and not _has_analysis_markers:
            return "(LLM returned spec file content instead of error analysis — see terminal output for the actual analysis)"
        # Strip fenced spec blocks
        lines = text.split('\n')
        result_lines = []
        in_spec_block = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('```spec') or (stripped.startswith('```') and any(
                l.strip().startswith(('Name:', 'Summary:', 'Version:', '%', '#'))
                for l in lines[max(0, i):min(len(lines), i + 5)]
            )):
                in_spec_block = True
                continue
            if in_spec_block and stripped == '```':
                in_spec_block = False
                continue
            if in_spec_block:
                continue
            result_lines.append(line)
        filtered = '\n'.join(result_lines).rstrip()
        if not filtered or len(filtered) < 20:
            return text
        return filtered

    def call_with_tools(self, messages, tools, manager, workspace_dir=None, allow_tool_scripts=False, max_rounds=15, interactive=False):
        max_rounds = max_rounds if max_rounds > 0 else 999999
        all_results = []
        _file_versions = {}
        _blocked_files = set()
        _WRITE_TOOLS = {"write_file", "edit_file"}
        _READ_TOOLS = {"read_file", "read_file_from_archive"}
        self._changed_files = set()
        _read_tracker = ReadCoverageTracker()

        # Record initial versions of all spec files so reverts to original are caught
        if workspace_dir:
            try:
                for _sf in Path(workspace_dir).glob("*.spec"):
                    try:
                        _init = manager.read_file_safe(_sf)
                        _init_hash = hashlib.md5(_init.encode()).hexdigest()
                        _file_versions[_sf.name] = [_init_hash]
                    except Exception:
                        pass
            except Exception:
                pass
        for round_idx in range(max_rounds):
            if self._chat_supported:
                payload = {
                    "model": self.model,
                    "messages": messages,
                    "tools": tools,
                    "stream": False,
                }
                self._apply_options_and_format(payload, tool_calling=True)
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

            # Anti-oscillation: pre-filter write/edit calls to detect reverts, no-ops, and blocked files
            _filtered_calls = []
            _filtered_indices = []
            _skipped_results = []
            _skipped_indices = []
            _all_skipped = True

            _filtered_read_calls, _read_skipped = _read_tracker.filter_reads(round_calls, workspace_dir, manager)

            for _ci, (name, tool_input) in enumerate(round_calls):
                if name in _READ_TOOLS:
                    if _ci in _read_skipped:
                        _skipped_results.append(_read_skipped[_ci])
                        _skipped_indices.append(_ci)
                        continue
                    _filtered_calls.append((name, tool_input))
                    _filtered_indices.append(_ci)
                    _all_skipped = False
                    continue
                if name not in _WRITE_TOOLS:
                    _filtered_calls.append((name, tool_input))
                    _filtered_indices.append(_ci)
                    _all_skipped = False
                    continue
                _path = tool_input.get("path", "")
                _resolved = resolve_path(_path, workspace_dir) if workspace_dir else None
                if _path in _blocked_files:
                    _msg = f"SKIP: {_path} is blocked (reverted a previous change). No further edits accepted."
                    _skipped_results.append(_msg)
                    _skipped_indices.append(_ci)
                    print(f"[FIX] {_msg}", flush=True)
                    continue
                if name == "write_file":
                    _new_content = tool_input.get("content", "")
                    _new_hash = hashlib.md5(_new_content.encode()).hexdigest()
                    if _resolved and _resolved.exists():
                        try:
                            _current = manager.read_file_safe(_resolved)
                            if hashlib.md5(_current.encode()).hexdigest() == _new_hash:
                                _msg = f"OK: File unchanged: {_path}"
                                _skipped_results.append(_msg)
                                _skipped_indices.append(_ci)
                                print(f"[FIX] {_msg}", flush=True)
                                continue
                        except Exception:
                            pass
                    _versions = _file_versions.get(_path, [])
                    if _new_hash in _versions:
                        _blocked_files.add(_path)
                        _msg = f"SKIP: write_file to {_path} reverts to a previous version. File is now blocked."
                        _skipped_results.append(_msg)
                        _skipped_indices.append(_ci)
                        print(f"[FIX] {_msg}", flush=True)
                        continue
                    _filtered_calls.append((name, tool_input))
                    _filtered_indices.append(_ci)
                    _all_skipped = False
                elif name == "edit_file":
                    if not _resolved or not _resolved.exists():
                        _filtered_calls.append((name, tool_input))
                        _filtered_indices.append(_ci)
                        _all_skipped = False
                        continue
                    _old_string = tool_input.get("old_string", "")
                    _new_string = tool_input.get("new_string", "")
                    if not _old_string:
                        _filtered_calls.append((name, tool_input))
                        _filtered_indices.append(_ci)
                        _all_skipped = False
                        continue
                    try:
                        _current = manager.read_file_safe(_resolved)
                    except Exception:
                        _filtered_calls.append((name, tool_input))
                        _filtered_indices.append(_ci)
                        _all_skipped = False
                        continue
                    if _current.count(_old_string) != 1:
                        _filtered_calls.append((name, tool_input))
                        _filtered_indices.append(_ci)
                        _all_skipped = False
                        continue
                    _new_content = _current.replace(_old_string, _new_string, 1)
                    _new_hash = hashlib.md5(_new_content.encode()).hexdigest()
                    _versions = _file_versions.get(_path, [])
                    if _new_hash in _versions:
                        _blocked_files.add(_path)
                        _msg = f"SKIP: edit_file to {_path} reverts to a previous version. File is now blocked."
                        _skipped_results.append(_msg)
                        _skipped_indices.append(_ci)
                        print(f"[FIX] {_msg}", flush=True)
                        continue
                    _filtered_calls.append((name, tool_input))
                    _filtered_indices.append(_ci)
                    _all_skipped = False

            if _all_skipped and _filtered_calls == [] and all_results:
                for _si, _ci in enumerate(_skipped_indices):
                    name, inp = round_calls[_ci]
                    _skip_msg = _skipped_results[_si]
                    all_results.append(f"{name}: {_skip_msg}")
                messages.append({"role": "assistant", "content": message.get('content', ''), "tool_calls": message['tool_calls']})
                for _si, _ci in enumerate(_skipped_indices):
                    name, inp = round_calls[_ci]
                    messages.append({"role": "tool", "content": str(_skipped_results[_si]), "name": name})
                print(f"[AI] All tool calls skipped (reverts/no-ops). Stopping tool loop.", flush=True)
                break

            round_results = execute_tool_calls(_filtered_calls, manager, workspace_dir or str(Path.cwd()), allow_tool_scripts, interactive=interactive, debug=self.debug)
            # Merge executed and skipped results in original round_calls order by index
            _merged_results = []
            for _ci, (name, inp) in enumerate(round_calls):
                if _ci in _filtered_indices:
                    _fi = _filtered_indices.index(_ci)
                    if _fi < len(round_results):
                        _merged_results.append((name, inp, round_results[_fi]))
                    else:
                        _path = inp.get("path", "?") if isinstance(inp, dict) else "?"
                        _merged_results.append((name, inp, f"Error: {name} for {_path} produced no result"))
                elif _ci in _skipped_indices:
                    _si = _skipped_indices.index(_ci)
                    _merged_results.append((name, inp, _skipped_results[_si]))
                else:
                    _path = inp.get("path", "?") if isinstance(inp, dict) else "?"
                    _merged_results.append((name, inp, f"Error: {name} for {_path} was not executed (skipped by anti-oscillation filter)"))
            for name, inp, r in _merged_results:
                display = format_tool_display(name, inp, r, self.debug)
                if display is None:
                    continue
                print(f"[FIX] {display}", flush=True)
            all_results.extend(f"{name}: {r}" for name, _, r in _merged_results if r)

            # Record file versions after successful write/edit
            for name, inp, r in _merged_results:
                if name in _WRITE_TOOLS and r.startswith("OK:"):
                    _path = inp.get("path", "")
                    if _path:
                        self._changed_files.add(_path)
                        _resolved = resolve_path(_path, workspace_dir) if workspace_dir else None
                        if _resolved and _resolved.exists():
                            try:
                                _content = manager.read_file_safe(_resolved)
                                _hash = hashlib.md5(_content.encode()).hexdigest()
                                if _path not in _file_versions:
                                    _file_versions[_path] = []
                                _file_versions[_path].append(_hash)
                            except Exception:
                                pass

            # Update read coverage from merged results
            _read_tracker.update_from_results(
                [(name, inp) for name, inp, _ in _merged_results],
                [r for _, _, r in _merged_results],
                workspace_dir, manager
            )

            messages.append({"role": "assistant", "content": message.get('content', ''), "tool_calls": message['tool_calls']})
            _injected_edit_help = False
            for name, inp, content in _merged_results:
                content = str(content)
                if len(content) > 2000:
                    if self.debug:
                        print(f"[DEBUG] Truncating {name} result: {len(content)} chars -> 2000 chars", flush=True)
                    content = content[:1000] + "\n... (truncated) ...\n" + content[-900:]
                messages.append({"role": "tool", "content": content, "name": name})
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

            # Prune old messages to keep context manageable — keep system
            # prompt (index 0) and the last 2 assistant rounds.
            messages[:] = prune_messages(messages, keep_rounds=2)

        print(f"[AI] Reached max rounds ({max_rounds}).", flush=True)
        return all_results


def chat_completion(ollama, messages, tools, debug=False, track_stats=False):
    """Send a non-streaming chat completion request with retry on transient errors
    and empty responses. Returns the parsed result dict.
    On HTTP/protocol errors or after 3 failed attempts, prints diagnostic info
    and calls sys.exit(2)."""
    payload = {"model": ollama.model, "messages": messages, "tools": tools, "stream": False}
    _opts = (ollama.options or {}).copy()
    fmt = _opts.pop("format", None)
    if fmt == "text":
        pass
    elif not tools:
        payload["format"] = "json"
    if _opts:
        payload["options"] = _opts
    _payload_str = None
    data_bytes = b''
    try:
        _payload_str = json.dumps(payload)
        data_bytes = _payload_str.encode('utf-8')
    except (TypeError, ValueError) as e:
        # Identify problematic messages
        for mi, msg in enumerate(messages):
            for field in ("content", "tool_call_id", "name"):
                val = msg.get(field)
                if val is not None and not isinstance(val, (str, type(None))):
                    print(f"[OLLAMA ERROR] Message {mi} field '{field}' is {type(val).__name__}, not str: {val!r}", flush=True)
        print(f"[OLLAMA ERROR] Failed to serialize payload: {e}", flush=True)
        # Dump first few messages for debugging
        import pprint
        for mi, msg in enumerate(messages[:3]):
            pprint.pprint(msg, depth=3)
        sys.exit(2)

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
            _payload_len = len(data_bytes) if data_bytes else 0
            print(f"[OLLAMA DEBUG] Payload size: {_payload_len} bytes. Messages: {len(messages)}.")
            if debug and _payload_str:
                # Show tool calls + results in last few messages
                for mi in range(max(0, len(messages)-4), len(messages)):
                    msg = messages[mi]
                    role = msg.get("role", "?")
                    if role == "assistant" and "tool_calls" in msg:
                        tcs = msg["tool_calls"]
                        print(f"[DEBUG] msg[{mi}] tool_calls={len(tcs)}: "
                              f"{[{t['function']['name']: t['function']['arguments'][:100]} for t in tcs]}", flush=True)
                    elif role == "tool":
                        tc_id = msg.get("tool_call_id", "?")
                        name = msg.get("name", "?")
                        content_len = len(msg.get("content", ""))
                        print(f"[DEBUG] msg[{mi}] tool_result id={tc_id} name={name} content_len={content_len}", flush=True)
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

        # Collect model-level diagnostics for the error message
        _model_name = result.get('model', '?')
        _done_reason = result.get('done_reason', '?')
        _eval_count = result.get('eval_count', '?')

        if attempt < 2:
            print(f"[OLLAMA] Empty response (retry {attempt+2}/3, "
                  f"model={_model_name}, eval_count={_eval_count}, "
                  f"done_reason={_done_reason}, message keys: "
                  f"{list(message.keys())})...")
            if debug:
                print(f"[DEBUG] Response ({len(raw)} bytes): {raw[:1000]}", flush=True)
            time.sleep(2)
        else:
            hint = ""
            if message.get('content') == '' and not message.get('tool_calls'):
                hint = " (model returned empty content with no tool calls)"
                if isinstance(_eval_count, int) and _eval_count <= 1:
                    hint += (" -- the model produced no meaningful tokens. "
                             "This model may not support tool/function calling. "
                             "Try a model that supports tools (e.g. qwen2.5, llama3, mistral).")
            print(f"[OLLAMA ERROR] Empty response after 3 attempts."
                  f"{hint} Model: {_model_name}, eval_count: {_eval_count}, "
                  f"done_reason: {_done_reason}.")
            if debug:
                print(f"[DEBUG] Full response ({len(raw)} bytes):\n{raw}", flush=True)
            sys.exit(2)
