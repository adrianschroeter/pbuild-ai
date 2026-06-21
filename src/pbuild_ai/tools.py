import json
import os
import subprocess
import urllib.request
from pathlib import Path
from pbuild_ai.diff_utils import show_diff

from pbuild_ai.network import is_safe_url

from pbuild_ai.utils import resolve_path

def build_tools_list():
    """Return the standard Ollama tool definitions."""
    return [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write or overwrite a file in the workspace directory. Only files within the workspace are allowed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to the file (relative to workspace root)"
                        },
                        "content": {
                            "type": "string",
                            "description": "Content to write to the file"
                        }
                    },
                    "required": ["path", "content"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read the content of a file in the workspace directory. Only files within the workspace are allowed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to the file (relative to workspace root)"
                        }
                    },
                    "required": ["path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files and directories in a directory within the workspace. Returns entries with trailing / for subdirectories.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Directory path (relative to workspace root)"
                        }
                    },
                    "required": ["path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "web_fetch",
                "description": "Fetch content from a remote HTTPS URL. Only HTTPS URLs to public servers are allowed. file://, http://, ftp://, and private/local IP addresses are blocked.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The HTTPS URL to fetch"
                        }
                    },
                    "required": ["url"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "git_command",
                "description": "Execute a git command within the workspace directory. Only local git operations are allowed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The git command to execute (e.g., 'git submodule add https://example.com/repo.git path/to/submodule')"
                        }
                    },
                    "required": ["command"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "run_tool_script",
                "description": "Execute a script from the tool-scripts directory within the workspace. The script must exist in <workspace>/tool-scripts/.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "script_name": {
                            "type": "string",
                            "description": "Name of the script file (e.g., 'setup.sh')"
                        },
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional arguments to pass to the script"
                        }
                    },
                    "required": ["script_name"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "ask_user",
                "description": "Ask the user a clarifying question and get their answer. Use this when you need more information to proceed, e.g., choosing between options or confirming details.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The question to ask the user"
                        }
                    },
                    "required": ["question"]
                }
            }
        }
    ]


def _extract_header(content):
    """Extract leading comment block (lines starting with '# ') and the rest of the content.
    Lines starting with '#!' (shebangs) are NOT treated as copyright header lines."""
    lines = content.split('\n')
    header_lines = []
    rest_start = 0
    for i, line in enumerate(lines):
        if line.startswith('# ') or line == '#':
            header_lines.append(line)
        else:
            rest_start = i
            break
    else:
        rest_start = len(lines)
    return '\n'.join(header_lines), '\n'.join(lines[rest_start:])


def execute_tool_calls(tool_calls, manager, workspace_dir, allow_tool_scripts=False):
    """Execute tool calls returned by Ollama. Returns list of tool results."""
    results = []
    workspace = Path(workspace_dir).resolve()
    for tool_name, tool_input in tool_calls:
        if tool_name == "write_file":
            path = tool_input.get("path")
            if not path:
                results.append("Error: write_file requires a 'path' argument")
                continue
            file_path = resolve_path(path, workspace_dir, for_write=True)
            if file_path is None or not manager._is_safe_path(file_path):
                results.append(f"Error: {tool_input['path']} is outside the workspace directory.")
                continue
            try:
                if file_path.resolve().is_relative_to(workspace / "tool-scripts"):
                    results.append(f"Error: Cannot write to tool-scripts/ directory: {tool_input['path']}")
                    continue
            except ValueError:
                pass
            try:
                old = manager.read_file_safe(file_path) if file_path.exists() else ""
            except Exception:
                old = ""
            # Preserve copyright header in .spec files
            if old and file_path.suffix == '.spec':
                old_header, _ = _extract_header(old)
                new_header, new_rest = _extract_header(tool_input["content"])
                if old_header and new_header and old_header != new_header:
                    tool_input["content"] = old_header + '\n' + new_rest
                    print(f"[TOOL] Preserved copyright header in {tool_input['path']}")
            show_diff(old, tool_input["content"], file_path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(tool_input["content"])
            print(f"[TOOL] write_file: {tool_input['path']}")
            results.append(f"OK: Wrote {tool_input['path']}")
            # Run format_spec_file on .spec files to normalize formatting
            if file_path.suffix == '.spec':
                fmt_cmd = ["/usr/lib/obs/service/format_spec_file", str(file_path.parent)]
                try:
                    fmt_result = subprocess.run(fmt_cmd, capture_output=True, text=True, timeout=30)
                    if fmt_result.returncode == 0:
                        formatted = file_path.read_text(encoding='utf-8')
                        if formatted != tool_input["content"]:
                            show_diff(tool_input["content"], formatted, file_path)
                            print(f"[TOOL] format_spec_file: normalized {tool_input['path']}")
                except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
                    pass
        elif tool_name == "read_file":
            path = tool_input.get("path")
            if not path:
                results.append("Error: read_file requires a 'path' argument")
                continue
            file_path = resolve_path(path, workspace_dir)
            if file_path is None or not manager._is_safe_path(file_path):
                results.append(f"Error: {tool_input['path']} is outside the workspace directory.")
                continue
            try:
                content = manager.read_file_safe(file_path)
                print(f"[TOOL] read_file: {tool_input['path']} -> {file_path}")
                results.append(content)
            except FileNotFoundError:
                results.append(f"Error: File not found: {tool_input['path']}")
            except Exception as e:
                results.append(f"Error reading file: {e}")
        elif tool_name == "list_files":
            path = tool_input.get("path")
            if not path:
                results.append("Error: list_files requires a 'path' argument")
                continue
            dir_path = resolve_path(path, workspace_dir)
            if dir_path is None or not manager._is_safe_path(dir_path):
                results.append(f"Error: {tool_input['path']} is outside the workspace directory.")
                continue
            if not dir_path.is_dir():
                results.append(f"Error: Not a directory: {tool_input['path']}")
                continue
            try:
                entries = []
                for entry in sorted(dir_path.iterdir()):
                    suffix = "/" if entry.is_dir() else ""
                    entries.append(f"{entry.name}{suffix}")
                print(f"[TOOL] list_files: {tool_input['path']} ({len(entries)} entries)")
                results.append("\n".join(entries) if entries else "(empty directory)")
            except Exception as e:
                results.append(f"Error listing directory: {e}")
        elif tool_name == "web_fetch":
            url = tool_input.get("url")
            if not url:
                results.append("Error: web_fetch requires a 'url' argument")
                continue
            safe, result = is_safe_url(url)
            if not safe:
                results.append(f"Error: {result}")
                continue
            try:
                with urllib.request.urlopen(url, timeout=30) as response:
                    content = response.read().decode("utf-8", errors="replace")
                    print(f"[TOOL] web_fetch: {url} ({len(content)} bytes)")
                    results.append(f"[Fetched {len(content)} bytes]\n{content[:100000]}")
            except Exception as e:
                results.append(f"Error fetching {url}: {e}")
        elif tool_name == "git_command":
            command = tool_input.get("command")
            if not command:
                results.append("Error: git_command requires a 'command' argument")
                continue
            if not command.startswith("git "):
                results.append("Error: Command must start with 'git '")
                continue
            blocked_patterns = ["push"]
            cmd_parts = command.split()
            for pattern in blocked_patterns:
                if pattern in cmd_parts:
                    results.append(f"Error: Git operation '{pattern}' is not allowed. Only local operations like submodule add are permitted.")
                    break
            else:
                full_cmd = f"cd {workspace} && {command}"
                try:
                    result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, cwd=workspace)
                    if result.returncode == 0:
                        print(f"[TOOL] git_command: {command}")
                        output = (result.stdout or "") + (result.stderr or "")
                        results.append(output[:10000] if output else "OK")
                    else:
                        results.append(f"Error: Git command failed (exit {result.returncode}):\n{result.stderr or result.stdout or 'No output'}")
                except Exception as e:
                    results.append(f"Error executing git command: {e}")
        elif tool_name == "run_tool_script":
            if not allow_tool_scripts:
                if not (workspace / "tool-scripts").is_dir():
                    continue
                results.append("Warning: --allow-tool-scripts is required to execute tool-scripts")
                continue
            script_name = tool_input["script_name"]
            args = tool_input.get("args", [])
            script_path = workspace / "tool-scripts" / script_name
            if not manager._is_safe_path(script_path):
                results.append(f"Error: Script '{script_name}' is outside the workspace directory.")
                continue
            if not script_path.is_file():
                results.append(f"Error: Script '{script_name}' not found in tool-scripts/ directory.")
                continue
            full_cmd = [str(script_path)] + args
            try:
                result = subprocess.run(full_cmd, capture_output=True, text=True, cwd=workspace)
                if result.returncode == 0:
                    print(f"[TOOL] run_tool_script: {script_name}")
                    output = (result.stdout or "") + (result.stderr or "")
                    results.append(output[:10000] if output else "OK")
                else:
                    results.append(f"Error: Script failed (exit {result.returncode}):\n{result.stderr or result.stdout or 'No output'}")
            except Exception as e:
                results.append(f"Error executing script: {e}")
        elif tool_name == "ask_user":
            question = tool_input.get("question", "")
            print(f"\n[ASK USER] {question}")
            answer = input("[YOUR ANSWER]: ").strip()
            results.append(answer)
        else:
            results.append(f"Error: Unknown tool '{tool_name}'")
    return results
