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

from pbuild_ai.llm_client import chat_completion, prune_messages, _summarize_round_calls
from pbuild_ai.tools import (execute_tool_calls, format_tool_display, resolve_path,
                             MODIFICATION_TOOLS, ask_tool_selection, instruction_messages)
from pbuild_ai.spinner import Spinner, AI_COLOR
from pbuild_ai.utils import ABORT_RE, ReadCoverageTracker, truncate_tool_result

_ARCHIVE_EXTS = ('.tar.gz', '.tgz', '.tar.bz2', '.tar.xz', '.tar', '.zip')
# Share of the round budget (PbuildContext.max_generate_rounds) after which the
# AI is told to stop researching and write the spec from what it already knows.
_RESEARCH_SHARE = 0.5
# The status block is replaced on every round, so it needs a fixed marker.
_STATUS_TAG = "[WORKSPACE STATUS]"
_STATUS_MAX_ENTRIES = 12
_STATUS_MAX_URLS = 8
_INDICATOR_FILES = (
    'package.json', 'go.mod', 'Cargo.toml', 'Gemfile',
    'setup.py', 'pyproject.toml', 'composer.json', 'pom.xml',
    'Makefile.PL', 'DESCRIPTION', 'rebar.config', 'mix.exs',
)


def _spec_files(workspace_dir):
    """The .spec files in the workspace root — the goal of generate mode."""
    return sorted(Path(workspace_dir).glob("*.spec"))


def _human_size(num):
    if num < 1024:
        return f"{num} bytes"
    if num < 1024 * 1024:
        return f"{num / 1024:.0f} KB"
    return f"{num / 1024 / 1024:.1f} MB"


def _archive_top_dir(path):
    """The single directory an archive extracts to, or None when ambiguous."""
    try:
        if path.name.endswith('.zip'):
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
        else:
            with tarfile.open(path, 'r:*') as tar:
                names = tar.getnames()
    except Exception:
        return None
    tops = {n.split('/', 1)[0] for n in names if n and not n.startswith('/')}
    return tops.pop() if len(tops) == 1 else None


def _budget_directive(rounds_used, max_rounds, has_spec):
    """Push the AI from researching to writing before the budget runs out.

    Returns None while there is no reason to hurry, and never once a spec file
    exists — the AI may then keep refining it.
    """
    if has_spec or max_rounds <= 0:
        return None
    left = max_rounds - rounds_used
    if left <= 1:
        return ("THIS IS THE LAST ROUND and no .spec file exists yet. Reply with a "
                "write_file call creating <name>.spec containing the complete spec — "
                "anything else leaves the task failed.")
    if rounds_used >= max_rounds * 0.8:
        return (f"{rounds_used} of {max_rounds} rounds are used and no .spec file exists "
                f"yet. Do NOT read, list, fetch or download anything else: write the "
                f".spec file now from what you already know. {left} rounds are left.")
    if rounds_used >= max_rounds * _RESEARCH_SHARE:
        return (f"Stop researching: {rounds_used} of {max_rounds} rounds are used and no "
                f".spec file exists yet. Write the .spec file now with the information you "
                f"have and mark unresolved details with '# TODO' comments — a spec with "
                f"open points is worth far more than no spec at all. Refine it and add "
                f"patches or supporting files afterwards.")
    return None


def _status_block(ctx, read_tracker, fetched_urls, directive):
    """Summary of what the workspace already holds, re-injected every round.

    Only the last rounds of the conversation survive pruning, so without this
    the model re-downloads and re-reads what it inspected earlier and can
    spend its whole round budget on research.
    """
    ws = Path(ctx.workspace_dir)
    lines = [_STATUS_TAG + " — maintained by pbuild-ai, refreshed every round."]
    try:
        entries = sorted(ws.iterdir(), key=lambda p: p.name)
    except OSError:
        entries = []
    archives, files, dirs = [], [], []
    for entry in entries:
        if entry.name.startswith('.') or entry.name in ('tool-scripts', 'skills'):
            continue
        if entry.is_dir():
            dirs.append(entry.name + "/")
        elif any(entry.name.endswith(ext) for ext in _ARCHIVE_EXTS):
            archives.append(entry)
        else:
            files.append(f"{entry.name} ({_human_size(entry.stat().st_size)})")

    def _show(label, items, limit=_STATUS_MAX_ENTRIES):
        if not items:
            return
        shown = items[:limit]
        more = f" (+{len(items) - len(shown)} more)" if len(items) > len(shown) else ""
        lines.append(f"{label}: {', '.join(shown)}{more}")

    def _archive_line(a):
        top = _archive_top_dir(a)
        note = f", extracts to '{top}/'" if top else ""
        return f"{a.name} ({_human_size(a.stat().st_size)}{note})"

    _show("Source archive(s)", [_archive_line(a) for a in archives])
    _show("Files in the workspace", files)
    _show("Directories", dirs)
    _show("Already fetched (cached — do not fetch again)", sorted(fetched_urls),
          limit=_STATUS_MAX_URLS)
    _show("Already read in full (do not read again)",
          read_tracker.fully_read(ctx.workspace_dir))
    specs = _spec_files(ws)
    if specs:
        lines.append(f"A spec file exists: {', '.join(s.name for s in specs)} "
                     f"— complete it and report what you created.")
    else:
        lines.append("The task is NOT complete while the workspace has no .spec file.")
    if directive:
        lines.append("ACTION REQUIRED: " + directive)
    return "\n".join(lines)


def _inject_status(messages, text):
    """Put the current status block last, replacing the one from an earlier round."""
    messages[:] = [m for m in messages
                   if not (m.get('role') == 'user'
                           and str(m.get('content') or '').startswith(_STATUS_TAG))]
    if text:
        messages.append({"role": "user", "content": text})


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
        prompt = getattr(skill, 'AI_SPEC_PROMPT', None)
        if prompt:
            print(f"[GENERATE] Applied skill: {skill_name}")
            messages.append({"role": "system", "content": f"[Skill: {skill_name}]\n{prompt}"})


def run_generate_mode(ctx):
    """Create a new openSUSE RPM package from scratch via AI + tools.

    Returns True when the run left a .spec file in the workspace, False when
    the AI ran out of rounds or aborted without producing one.
    """
    print(f"[GENERATE] Creating new package from prompt: {ctx.generate_prompt}")
    generate_skill = ctx.skill_manager.get_skill_by_name("generate_mode")
    if generate_skill:
        system_content = generate_skill.GENERATE_SYSTEM_PROMPT.format(
            full_context=ctx.full_context or 'No AGENTS.md',
        )
        user_content = generate_skill.GENERATE_USER_PROMPT.format(
            workspace_dir=ctx.workspace_dir,
            generate_prompt=ctx.generate_prompt,
        )
    else:
        print("[INFO] generate_mode skill not found, using inline fallback.")
        system_content = f"""You are an RPM packager assistant. Your task is to create a new openSUSE RPM package from scratch based on the user's specification in the user message.

IMPORTANT: The specification in the user message IS the request, not a conversation starter. Do NOT ask the user "what would you like to package?" or otherwise request information they already provided. Start working immediately based on the specification given.

Follow these rules:
1. Research the upstream project first using web_fetch if a URL is provided or you can infer one, then create the package. Do NOT fetch the same URL more than once — the result is cached.
2. For GitHub projects, use https://api.github.com/repos/OWNER/REPO/releases/latest and https://api.github.com/repos/OWNER/REPO/tags to find release versions and tarball URLs instead of the main HTML page. For specific tags, use https://github.com/OWNER/REPO/archive/refs/tags/TAG.tar.gz.
2. Only call ask_user if the specification is truly missing critical information (e.g., no project name, no source URL, no license hint, and you cannot determine it from research). Do NOT ask generic questions.
3. Create the .spec file directly in the workspace root, next to the downloaded source tarball — do NOT use a subdirectory.
4. Create a complete .spec file following openSUSE packaging conventions from OPENSUSE.md:
   - Keep the copyright header
   - If you create a PACKAGE_NAME.changes file, always keep an empty %%changelog section in the spec — rpm merges the .changes entries into it at build time
   - Do NOT use ?dist macro — use ~ in version format
   - Use standard SUSE RPM macros: %%fdupes, %%set_permissions
   - Single dependency per BuildRequires: line
   - Omit empty %%clean, %%post, %%pre, %%preun, %%postun sections (an empty %%changelog stays when a PACKAGE_NAME.changes file exists)
   - Never recommend rpmbuild
   - Build environment has NO network access — patch out any code that tries to reach external hosts at build time
5. If the upstream provides source archives, set Source0 to the download URL and Source1..N for additional files.
6. You MAY also create supporting files (patches, .desktop, sysconfig, tmpfiles.d, etc.) as needed.
7. When you are done creating files, call run_tool_script("format_spec_file", []) on the spec directory as your final step to normalize spec formatting.
8. Note: only the last 2 tool-calling rounds are kept in context — older messages are pruned each round. Do not rely on history beyond the most recent exchange. If you need information you provided earlier, include it again in your current output.
9. When you are done, tell the user what you created.
9. Do NOT use HTML or markdown formatting in your text responses — use plain text only. No <b>, <a>, <pre>, or any other tags.
10. pbuild-ai adds a "[WORKSPACE STATUS]" message before every round listing the files, source archives and already-read sources of the workspace — trust it, do not download or read what it already lists.
11. The run only succeeds once a .spec file exists in the workspace root. When a status message tells you to write the spec, write it immediately with what you know and mark the open points with '# TODO' comments.

AGENTS.md instructions (follow these):
{ctx.full_context or 'No AGENTS.md'}"""
        user_content = f"""THE USER'S SPECIFICATION (this is the complete request, not a conversation starter):
{ctx.generate_prompt}

Workspace directory: {ctx.workspace_dir}

Start researching and building — do NOT ask me what to package, I already told you."""

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]
    fetch_cache = {}
    _read_tracker = ReadCoverageTracker()
    _evaluated_specs = set()
    _evaluated_archives = set()
    _injected_skills = set()
    # A round budget of 0 means unlimited, as with --max-ai-rounds.
    max_rounds = ctx.max_generate_rounds
    _round_note = f"/{max_rounds}" if max_rounds > 0 else ""
    for round_idx in range(max_rounds if max_rounds > 0 else sys.maxsize):
        _spec_present = bool(_spec_files(ctx.workspace_dir))
        _inject_status(messages, _status_block(
            ctx, _read_tracker, fetch_cache,
            _budget_directive(round_idx, max_rounds, _spec_present)))
        _all_text = "\n".join(msg.get('content', '') or '' for msg in messages)
        _tok = ctx.ai.count_tokens(_all_text)
        _ctx_str = f" ({_tok//1024}k/{ctx.ai.max_tokens//1024}k tok)"
        with Spinner(prefix=f"[AI] {ctx.ai.model}{_ctx_str}", suffix=f"Analyzing spec ({round_idx+1}{_round_note})", color=AI_COLOR):
            result = chat_completion(ctx.ai, messages, ctx.tools, debug=ctx.debug, track_stats=True)

        message = result.get('message', {})
        if 'tool_calls' in message and message['tool_calls']:
            round_calls = []
            for tc in message['tool_calls']:
                tool_name = tc['function']['name']
                raw_args = tc['function']['arguments']
                try:
                    tool_input = raw_args if isinstance(raw_args, dict) else (json.loads(raw_args) if raw_args else {})
                except (json.JSONDecodeError, TypeError):
                    tool_input = {}
                if not isinstance(tool_input, dict):
                    tool_input = {}
                if tool_name == "web_fetch" and tool_input.get("url") in fetch_cache:
                    cached = fetch_cache[tool_input["url"]]
                    print(f"[GENERATE] Cached: web_fetch({tool_input['url']}) ({len(cached)} bytes)", flush=True)
                    round_calls.append(("_skip", {"_cached": cached}))
                    continue
                round_calls.append((tool_name, tool_input))

            if ctx.interactive and sum(1 for c in round_calls if c[0] in MODIFICATION_TOOLS) > 1:
                round_calls, _instruction = ask_tool_selection(round_calls)
                if _instruction:
                    print("[INTERACTIVE] Passing your instruction to the AI.")
                    messages.extend(instruction_messages(message, [tc['function']['name'] for tc in message['tool_calls']], _instruction))
                    continue
                if not round_calls:
                    print("No tool calls selected.")
                    continue

            for name, tool_input in round_calls:
                if name == "_skip":
                    continue
                args_preview = json.dumps(tool_input)[:300]
                if ctx.debug:
                    print(f"[AI] Tool call: {name}({args_preview})", flush=True)

            # Read dedup: skip reads already covered by prior rounds
            _filtered_read_calls, _read_skipped = _read_tracker.filter_reads(round_calls, ctx.workspace_dir, ctx.manager)

            _skip_entries = {}
            _exec_calls = []
            for _ci, (name, inp) in enumerate(_filtered_read_calls):
                if name == "_skip":
                    _skip_entries[_ci] = inp["_cached"]
                else:
                    _exec_calls.append((name, inp))
            if _read_skipped:
                for _ci in _read_skipped:
                    _name, _inp = round_calls[_ci]
                    print(f"[GENERATE] READ SKIP: {_inp.get('path', _inp.get('file_path', ''))} already read")

            try:
                exec_results = execute_tool_calls(_exec_calls, ctx.manager, ctx.workspace_dir, ctx.allow_tool_scripts, interactive=ctx.interactive, debug=ctx.debug)
            except Exception as e:
                exec_results = [f"Error executing tool: {e}"] * len(_exec_calls)
                print(f"[GENERATE TOOL ERROR] {e}")

            # Reconstruct round_results in original round_calls order
            # Merge: _skip_entries, _read_skipped, and exec_results
            round_results = []
            _ei = 0
            for _ci, (name, inp) in enumerate(round_calls):
                if name == "_skip":
                    round_results.append(inp["_cached"])
                elif _ci in _read_skipped:
                    round_results.append(_read_skipped[_ci])
                else:
                    _result = exec_results[_ei] if _ei < len(exec_results) else f"Error: Missing result for tool call #{_ci} ({name})"
                    if name == "web_fetch" and _result.startswith("[Fetched "):
                        fetch_cache[inp["url"]] = _result
                    round_results.append(_result)
                    _ei += 1

            # Update read coverage from results
            _read_tracker.update_from_results(round_calls, round_results, ctx.workspace_dir, ctx.manager)

            for (name, inp), r in zip(round_calls, round_results):
                display = format_tool_display(name, inp, r, ctx.debug)
                if display is None:
                    continue
                print(f"[GENERATE] {display}", flush=True)
            _summary_items = [
                ("web_fetch (cached)", {}, r) if name == "_skip" else (name, inp, r)
                for (name, inp), r in zip(round_calls, round_results)
            ]
            round_summary = _summarize_round_calls(_summary_items)
            print(f"[GENERATE] round {round_idx+1}{_round_note}: "
                  f"{round_summary or 'no tool calls'}", flush=True)
            response_content = message.get('content', '') or ''
            tc_arg = dict(tool_calls=message['tool_calls'])
            messages.append({"role": "assistant", "content": response_content, **tc_arg})
            for (name, _), content in zip(round_calls, round_results):
                tool_name = "web_fetch" if name == "_skip" else name
                content = truncate_tool_result(name, content, ctx.debug)
                messages.append({"role": "tool", "content": content, "name": tool_name})
            messages[:] = prune_messages(messages, keep_rounds=2)
            spec_files = _spec_files(ctx.workspace_dir)
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
            print(f"\n[GENERATE] AI:\n{text_clean}\n")
            _abort = ABORT_RE.search(text)
            if _abort:
                print(f"[ABORT] {_abort.group(1).strip() or 'aborted by AI'}")
                return False
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
            print("[GENERATE] No response from AI.")
            break
    else:
        _limit = f"of {max_rounds}" if max_rounds > 0 else "without limit"
        print(f"[GENERATE] Stopped after the round budget ({_limit}) — the AI never "
              f"finished the package.")

    for spec_file in _spec_files(ctx.workspace_dir):
        try:
            fmt_cmd = ["/usr/lib/obs/service/format_spec_file", str(spec_file.parent)]
            subprocess.run(fmt_cmd, capture_output=True, text=True, timeout=30)
            print(f"[GENERATE] format_spec_file: normalized {spec_file.name}")
        except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
            pass

    if _spec_files(ctx.workspace_dir):
        return True
    print("[GENERATE] Error: no .spec file was created — the package was NOT generated.")
    return False
