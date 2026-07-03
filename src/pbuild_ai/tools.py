import contextlib
import fnmatch
import json
import os
import re
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from pbuild_ai.diff_utils import show_diff
from pbuild_ai.spinner import Spinner, GREEN

from pbuild_ai.network import is_safe_url

from pbuild_ai.utils import resolve_path

_FORMAT_SPEC_FILE_PATH = "/usr/lib/obs/service/format_spec_file"

MAX_ARCHIVE_READ_SIZE = 512 * 1024  # 500 KB
_ARCHIVE_EXTS = ('.tar.gz', '.tgz', '.tar.bz2', '.tar.xz', '.tar', '.zip')


def _is_safe_archive_path(file_path):
    """Reject path-traversal inside an archive (absolute or ../ components)."""
    if os.path.isabs(file_path):
        return False
    for part in Path(file_path).parts:
        if part == '..':
            return False
    return True


def _archive_type(path):
    """Return 'tar' or 'zip' based on file extension, or None if unsupported."""
    name = path.name.lower()
    if name.endswith('.zip'):
        return 'zip'
    if any(name.endswith(e) for e in ('.tar.gz', '.tgz', '.tar.bz2', '.tar.xz', '.tar')):
        return 'tar'
    return None


def _resolve_archive_path(archive, file_path):
    """Resolve file_path within archive, trying common top-level prefix if direct lookup fails.

    GitHub tarballs have a ``{repo}-{version}/`` prefix on all entries —
    this strips that layer so callers can use bare filenames.
    """
    _lookup = (
        (lambda p: archive.getmember(p))
        if hasattr(archive, 'getmember')
        else (lambda p: archive.getinfo(p))
    )
    try:
        _lookup(file_path)
        return file_path
    except (KeyError, LookupError):
        pass
    names = (
        archive.getnames()
        if hasattr(archive, 'getnames')
        else archive.namelist()
    )
    top = None
    for name in names:
        parts = name.split('/')
        if len(parts) >= 2:
            d = parts[0]
            if top is None:
                top = d
            elif top != d:
                return file_path
    if top:
        candidate = f"{top}/{file_path}"
        try:
            _lookup(candidate)
            return candidate
        except (KeyError, LookupError):
            pass
    return file_path


def _strip_html(text):
    """Remove HTML tags, decode common entities, and collapse whitespace."""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    text = re.sub(r'&[#a-zA-Z0-9]+;', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _auth_headers(url):
    """Return auth headers for known APIs. Reads tokens from environment."""
    headers = {}
    if "api.github.com" in url:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers

def build_tools_list(interactive=False):
    """Return the standard Ollama tool definitions.
    When interactive=False, the ask_user tool is excluded so Ollama
    cannot waste a round on a question nobody will answer."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write or overwrite a file in the workspace directory. Use ONLY for large/wholesale rewrites or new files. For targeted changes, prefer edit_file to avoid accidentally dropping lines.",
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
                "name": "edit_file",
                "description": "Apply a targeted search-and-replace edit to an existing file in the workspace. PREFER this over write_file for small, specific changes — it preserves all lines not being modified. IMPORTANT: Include enough surrounding lines (the full target line plus 1-2 lines of context before/after) so that old_string matches EXACTLY ONE location.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to the file (relative to workspace root)"
                        },
                        "old_string": {
                            "type": "string",
                            "description": "Exact text to search for. Include adjacent surrounding lines (full target line + 1-2 lines before/after) so that this string appears ONLY ONCE in the file."
                        },
                        "new_string": {
                            "type": "string",
                            "description": "Replacement text"
                        }
                    },
                    "required": ["path", "old_string", "new_string"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read the content of a file in the workspace directory. Optionally read a portion of the file by specifying offset and/or limit (both in characters). Only files within the workspace are allowed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to the file (relative to workspace root)"
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Character offset to start reading from (0-based, inclusive). If omitted, reads from the beginning."
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of characters to read. If omitted, reads to end of file."
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
                "description": "Fetch content from a remote HTTPS URL into the AI context (read-only). For release notes, API responses, or web pages. Do NOT use this to download source tarballs — use download_file instead, which saves the file to disk permanently. Only HTTPS URLs to public servers are allowed. file://, http://, ftp://, and private/local IP addresses are blocked.",
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
                "name": "download_file",
                "description": "Download a file from a remote HTTPS URL and save it to the workspace directory. Use this for source tarballs — unlike web_fetch, this saves the file permanently to disk. Only safe HTTPS URLs are allowed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The HTTPS URL to download"
                        },
                        "filename": {
                            "type": "string",
                            "description": "Filename to save as (relative to workspace root)"
                        }
                    },
                    "required": ["url", "filename"]
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
                "name": "remove_file",
                "description": "Remove/delete a file from the workspace directory. Only files within the workspace are allowed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to the file to remove (relative to workspace root)"
                        }
                    },
                    "required": ["path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "rename_file",
                "description": "Rename or move a file within the workspace directory. Both source and destination must be within the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": "Current path of the file (relative to workspace root)"
                        },
                        "destination": {
                            "type": "string",
                            "description": "New path for the file (relative to workspace root)"
                        }
                    },
                    "required": ["source", "destination"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_archive",
                "description": "List files inside a compressed archive (tar.gz, tar.bz2, tar.xz, tar, zip) in the workspace. Returns paths relative to archive root. Optionally filter with a glob pattern.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "archive_path": {
                            "type": "string",
                            "description": "Path to the archive file (relative to workspace root)"
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Optional glob filter (e.g. '*.md', '**/*.py'). Only matching entries are returned."
                        }
                    },
                    "required": ["archive_path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_file_from_archive",
                "description": "Read a specific file from inside a compressed archive (tar.gz, tar.bz2, tar.xz, tar, zip) in the workspace. Optionally read a portion by specifying offset and/or limit (both in characters). Symlinks, hardlinks, files over 500 KB, and paths with ../ are rejected for security.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "archive_path": {
                            "type": "string",
                            "description": "Path to the archive file (relative to workspace root)"
                        },
                        "file_path": {
                            "type": "string",
                            "description": "Path to the file inside the archive"
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Character offset to start reading from (0-based, inclusive). If omitted, reads from the beginning."
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of characters to read. If omitted, reads to end of file."
                        }
                    },
                    "required": ["archive_path", "file_path"]
                }
            }
        }
    ]
    if interactive:
        tools.append({
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
        })
    return tools


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


def execute_tool_calls(tool_calls, manager, workspace_dir, allow_tool_scripts=False, interactive=False, debug=False):
    """Execute tool calls returned by Ollama. Returns list of tool results."""
    results = []
    workspace = Path(workspace_dir).resolve()
    _spec_dir = workspace  # tracks spec directory for subsequent lookups
    for tool_name, tool_input in tool_calls:
        if tool_name == "write_file":
            path = tool_input.get("path")
            content = tool_input.get("content", "")
            if not path:
                # If content looks like a spec file, try to infer path
                inferred = None
                content_start = content.strip()[:200].lower()
                if "spec file for package" in content_start or content_start.startswith("name:"):
                    spec_files = sorted(_spec_dir.glob("*.spec"))
                    if len(spec_files) == 1:
                        inferred = spec_files[0].name
                    elif spec_files:
                        m = re.search(r'spec file for package\s+(\S+)', content) or re.search(r'^Name:\s*(\S+)', content, re.MULTILINE)
                        if m:
                            pkg = m.group(1)
                            for sf in spec_files:
                                if sf.stem == pkg or sf.stem == f"python-{pkg}":
                                    inferred = sf.name
                                    break
                        if not inferred:
                            inferred = spec_files[0].name if spec_files else None
                if inferred:
                    path = inferred
                    tool_input["path"] = inferred
                    print(f"[TOOL] write_file: inferred path='{inferred}' from content")
            if not path:
                keys = list(tool_input.keys())
                results.append(f"Error: write_file: missing 'path'. Got keys: {keys}. Must have 'path' (relative path) and 'content'.")
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
            show_diff(old, tool_input["content"], file_path, prefix="[TOOL]")
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(tool_input["content"])
            print(f"[TOOL] write_file: {tool_input['path']}")
            results.append(f"OK: Wrote {tool_input['path']}")
            # Track spec directory for subsequent tool call lookups
            if file_path.suffix == '.spec':
                _spec_dir = file_path.parent
                fmt_cmd = [_FORMAT_SPEC_FILE_PATH, str(file_path.parent)]
                try:
                    fmt_result = subprocess.run(fmt_cmd, capture_output=True, text=True, timeout=30)
                    if fmt_result.returncode == 0:
                        formatted = file_path.read_text(encoding='utf-8')
                        if formatted != tool_input["content"]:
                            show_diff(tool_input["content"], formatted, file_path, prefix="[TOOL]")
                            print(f"[TOOL] format_spec_file: normalized {tool_input['path']}")
                except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
                    pass
        elif tool_name == "edit_file":
            path = tool_input.get("path")
            old_string = tool_input.get("old_string")
            new_string = tool_input.get("new_string", "")
            if not path:
                results.append(f"Error: edit_file: missing 'path'. Got keys: {list(tool_input.keys())}.")
                continue
            if not old_string:
                results.append(f"Error: edit_file: missing 'old_string'. Got keys: {list(tool_input.keys())}.")
                continue
            file_path = resolve_path(path, workspace_dir)
            if file_path is None or not manager._is_safe_path(file_path):
                results.append(f"Error: {path} is outside the workspace directory.")
                continue
            try:
                if file_path.resolve().is_relative_to(workspace / "tool-scripts"):
                    results.append(f"Error: Cannot edit files in tool-scripts/ directory: {path}")
                    continue
            except ValueError:
                pass
            if not file_path.exists():
                results.append(f"Error: File not found: {path}")
                continue
            try:
                content = manager.read_file_safe(file_path)
            except Exception as e:
                results.append(f"Error reading file: {e}")
                continue
            count = content.count(old_string)
            if count == 0:
                results.append(f"Error: edit_file: old_string not found in {path}")
                continue
            if count > 1:
                results.append(f"Error: edit_file: old_string found {count} times in {path}. Provide more context to make the match unique.")
                continue
            new_content = content.replace(old_string, new_string, 1)
            if content == new_content:
                results.append(f"OK: No change (old_string == new_string in {path})")
                continue
            show_diff(content, new_content, file_path, prefix="[TOOL]")
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"[TOOL] edit_file: {path} (1 match replaced)")
            results.append(f"OK: Edited {path}")
            # Track spec directory for subsequent tool call lookups
            if file_path.suffix == '.spec':
                _spec_dir = file_path.parent
                fmt_cmd = ["/usr/lib/obs/service/format_spec_file", str(file_path.parent)]
                try:
                    fmt_result = subprocess.run(fmt_cmd, capture_output=True, text=True, timeout=30)
                    if fmt_result.returncode == 0:
                        formatted = file_path.read_text(encoding='utf-8')
                        if formatted != new_content:
                            show_diff(new_content, formatted, file_path, prefix="[TOOL]")
                            print(f"[TOOL] format_spec_file: normalized {path}")
                except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
                    pass
        elif tool_name == "read_file":
            path = tool_input.get("path")
            if not path:
                results.append(f"Error: read_file: missing 'path'. Got keys: {list(tool_input.keys())}.")
                continue
            file_path = resolve_path(path, workspace_dir)
            if file_path is None or not manager._is_safe_path(file_path):
                results.append(f"Error: {tool_input['path']} is outside the workspace directory.")
                continue
            try:
                content = manager.read_file_safe(file_path)
                offset = tool_input.get("offset")
                limit = tool_input.get("limit")
                if offset is not None or limit is not None:
                    start = offset if offset is not None else 0
                    end = start + limit if limit is not None else None
                    content = content[start:end]
                print(f"[TOOL] read_file: {tool_input['path']}" +
                      (f" (offset={offset}, limit={limit})" if offset is not None or limit is not None else ""))
                results.append(content)
            except FileNotFoundError:
                results.append(f"Error: File not found: {tool_input['path']}")
            except Exception as e:
                results.append(f"Error reading file: {e}")
        elif tool_name == "list_files":
            path = tool_input.get("path")
            if not path:
                results.append(f"Error: list_files: missing 'path'. Got keys: {list(tool_input.keys())}.")
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
                req = urllib.request.Request(url, headers=_auth_headers(url))
                with Spinner(prefix="[TOOL] web_fetch", color=GREEN):
                    with urllib.request.urlopen(req, timeout=30) as response:
                        content = response.read().decode("utf-8", errors="replace")
                    stripped = _strip_html(content)
                    display = stripped[:100000] if len(stripped) < len(content) else content[:100000]
                    print(f"[TOOL] web_fetch: {url} ({len(content)} bytes -> {len(stripped)} stripped)")
                    results.append(f"[Fetched {len(content)} bytes]\n{display}")
            except Exception as e:
                msg = f"Error fetching {url}: {e}"
                if "api.github.com" in url and hasattr(e, 'code') and e.code == 403:
                    if not os.environ.get("GITHUB_TOKEN") and not os.environ.get("GH_TOKEN"):
                        msg += " [HINT] Set GITHUB_TOKEN env var to increase GitHub API rate limits."
                results.append(msg)
        elif tool_name == "download_file":
            url = tool_input.get("url")
            filename = tool_input.get("filename")
            if not url:
                results.append("Error: download_file requires a 'url' argument")
                continue
            if not filename:
                results.append("Error: download_file requires a 'filename' argument")
                continue

            # Expand RPM macros (%{name}, %{version}, etc.) using spec files in the tracked directory
            if "%{" in filename:
                macros = {}
                for spec_file in sorted(_spec_dir.glob("*.spec")):
                    spec_text = spec_file.read_text(encoding="utf-8", errors="replace")
                    for m in re.finditer(r'^(Name|Version):\s*(\S+)', spec_text, re.MULTILINE):
                        macros[m.group(1).lower()] = m.group(2)
                expanded = filename
                for key, val in macros.items():
                    expanded = expanded.replace(f"%{{{key}}}", val)
                expanded = expanded.replace(f"%{{version}}", macros.get("version", ""))
                if expanded != filename:
                    print(f"[TOOL] download_file: expanded RPM macros: '{filename}' -> '{expanded}'")
                    filename = expanded
                    tool_input["filename"] = expanded
            safe, result = is_safe_url(url)
            if not safe:
                results.append(f"Error: {result}")
                continue
            file_path = resolve_path(filename, workspace_dir, for_write=True)
            if file_path is None or not manager._is_safe_path(file_path):
                results.append(f"Error: {filename} is outside the workspace directory.")
                continue
            try:
                if file_path.resolve().is_relative_to(workspace / "tool-scripts"):
                    results.append(f"Error: Cannot download to tool-scripts/ directory: {filename}")
                    continue
            except ValueError:
                pass
            try:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                if file_path.exists():
                    size = file_path.stat().st_size
                    print(f"[TOOL] download_file: {filename} already exists ({size} bytes), skipping download. "
                          f"Use remove_file first if the file is broken or needs to be re-fetched from a different URL.")
                    results.append(f"OK: Already have {size} bytes at {filename}. "
                                   f"Use remove_file first if the file is broken or needs to be re-downloaded from a different URL.")
                    continue
                req = urllib.request.Request(url, headers=_auth_headers(url))
                with Spinner(prefix="[TOOL] download", color=GREEN):
                    with urllib.request.urlopen(req, timeout=120) as response:
                        data = response.read()
                with open(file_path, 'wb') as f:
                    f.write(data)
                size = file_path.stat().st_size
                print(f"[TOOL] download_file: {url} -> {filename} ({size} bytes)")
                results.append(f"OK: Downloaded {size} bytes to {filename}")
                # Remove old archives matching the same pattern (different version)
                saved_name = file_path.name
                for ext in ('.tar.gz', '.tar.bz2', '.tar.xz', '.tar', '.zip', '.tgz', '.tar.Z'):
                    if saved_name.endswith(ext):
                        prefix = saved_name[:-len(ext)]
                        m = re.search(r'[\d.]+$', prefix)
                        if m:
                            base = prefix[:m.start()]
                            for old_file in sorted(file_path.parent.glob(f"{base}*{ext}")):
                                if old_file.name != saved_name and old_file.is_file():
                                    old_file.unlink()
                                    print(f"[TOOL] download_file: removed old archive {old_file.name}")
                        break
            except Exception as e:
                msg = f"Error downloading {url}: {e}"
                if "api.github.com" in url and hasattr(e, 'code') and e.code == 403:
                    if not os.environ.get("GITHUB_TOKEN") and not os.environ.get("GH_TOKEN"):
                        msg += " [HINT] Set GITHUB_TOKEN env var to increase GitHub API rate limits."
                results.append(msg)
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
                    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
                    result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, cwd=workspace, env=env)
                    if result.returncode == 0:
                        print(f"[TOOL] git_command: {command}")
                        output = (result.stdout or "") + (result.stderr or "")
                        results.append(output[:10000] if output else "OK")
                    else:
                        results.append(f"Error: Git command failed (exit {result.returncode}):\n{result.stderr or result.stdout or 'No output'}")
                except Exception as e:
                    results.append(f"Error executing git command: {e}")
        elif tool_name == "run_tool_script":
            script_name = tool_input.get("script_name", "")
            args = tool_input.get("args", [])
            # format_spec_file is a well-known OBS service, not a tool-scripts entry
            if script_name == "format_spec_file":
                fmt_cwd = _spec_dir
                if args:
                    _arg = args[0]
                    _arg_path = Path(_arg)
                    if _arg_path.suffix == '.spec':
                        fmt_cwd = (workspace / _arg_path).resolve().parent
                    else:
                        fmt_cwd = Path(_arg).resolve()
                fmt_cmd = [_FORMAT_SPEC_FILE_PATH]
                try:
                    fmt_result = subprocess.run(fmt_cmd, capture_output=True, text=True, timeout=30, cwd=fmt_cwd)
                    if fmt_result.returncode == 0:
                        spec_files = list(fmt_cwd.glob("*.spec"))
                        if spec_files:
                            print(f"[TOOL] format_spec_file: normalized {len(spec_files)} spec(s)")
                        results.append("OK")
                    else:
                        err = fmt_result.stderr or fmt_result.stdout or "unknown error"
                        results.append(f"Warning: format_spec_file returned {fmt_result.returncode}: {err.strip()}")
                except FileNotFoundError:
                    results.append("Warning: format_spec_file not found (install obs-service-format_spec_file)")
                except (subprocess.TimeoutExpired, PermissionError) as e:
                    results.append(f"Warning: format_spec_file error: {e}")
                continue
            if not allow_tool_scripts:
                if (workspace / "tool-scripts").is_dir():
                    results.append("Warning: run_tool_script requires --allow-tool-scripts")
                continue
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
            if not interactive:
                results.append("Error: Interactive questions disabled. Re-run with --interactive to enable.")
                continue
            question = tool_input.get("question", "")
            print(f"\n[ASK USER] {question}")
            answer = input("[YOUR ANSWER]: ").strip()
            results.append(answer)
        elif tool_name == "remove_file":
            path = tool_input.get("path")
            if not path:
                results.append(f"Error: remove_file: missing 'path'. Got keys: {list(tool_input.keys())}.")
                continue
            file_path = resolve_path(path, workspace_dir)
            if file_path is None or not manager._is_safe_path(file_path):
                results.append(f"Error: {path} is outside the workspace directory.")
                continue
            try:
                if file_path.resolve().is_relative_to(workspace / "tool-scripts"):
                    results.append(f"Error: Cannot remove files from tool-scripts/ directory: {path}")
                    continue
            except ValueError:
                pass
            if not file_path.exists():
                results.append(f"Error: File not found: {path}")
                continue
            if file_path.is_dir():
                results.append(f"Error: Cannot remove directory: {path} (remove_file only removes files)")
                continue
            try:
                file_path.unlink()
                print(f"[TOOL] remove_file: {path}")
                results.append(f"OK: Removed {path}")
            except Exception as e:
                results.append(f"Error removing file: {e}")
        elif tool_name == "rename_file":
            source = tool_input.get("source")
            destination = tool_input.get("destination")
            if not source:
                results.append(f"Error: rename_file: missing 'source'. Got keys: {list(tool_input.keys())}.")
                continue
            if not destination:
                results.append(f"Error: rename_file: missing 'destination'. Got keys: {list(tool_input.keys())}.")
                continue
            src_path = resolve_path(source, workspace_dir)
            dst_path = resolve_path(destination, workspace_dir, for_write=True)
            if src_path is None or not manager._is_safe_path(src_path):
                results.append(f"Error: {source} is outside the workspace directory.")
                continue
            if dst_path is None or not manager._is_safe_path(dst_path):
                results.append(f"Error: {destination} is outside the workspace directory.")
                continue
            try:
                if src_path.resolve().is_relative_to(workspace / "tool-scripts"):
                    results.append(f"Error: Cannot rename files in tool-scripts/ directory: {source}")
                    continue
            except ValueError:
                pass
            try:
                if dst_path.resolve().is_relative_to(workspace / "tool-scripts"):
                    results.append(f"Error: Cannot rename files into tool-scripts/ directory: {destination}")
                    continue
            except ValueError:
                pass
            if not src_path.exists():
                results.append(f"Error: Source not found: {source}")
                continue
            if not src_path.is_file():
                results.append(f"Error: Cannot rename non-file: {source}")
                continue
            if dst_path.exists():
                results.append(f"Error: Destination already exists: {destination}")
                continue
            try:
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                src_path.rename(dst_path)
                print(f"[TOOL] rename_file: {source} -> {destination}")
                results.append(f"OK: Renamed {source} to {destination}")
            except Exception as e:
                results.append(f"Error renaming file: {e}")
        elif tool_name == "list_archive":
            archive_path = tool_input.get("archive_path")
            if not archive_path:
                results.append("Error: list_archive requires an 'archive_path' argument")
                continue
            arch_resolved = resolve_path(archive_path, workspace_dir)
            if arch_resolved is None or not manager._is_safe_path(arch_resolved):
                results.append(f"Error: {archive_path} is outside the workspace directory.")
                continue
            if not arch_resolved.is_file():
                results.append(f"Error: Archive not found: {archive_path}")
                continue
            arch_type = _archive_type(arch_resolved)
            pattern = tool_input.get("pattern")
            if arch_type == 'tar':
                try:
                    with (Spinner(prefix=f"[TOOL] list_archive", color=GREEN) if debug else contextlib.nullcontext()):
                        with tarfile.open(arch_resolved, 'r:*') as tar:
                            names = tar.getnames()
                    if pattern:
                        names = [n for n in names if fnmatch.fnmatch(n, pattern)]
                    count = len(names)
                    MAX_LIST = 100
                    if count > MAX_LIST:
                        display = "\n".join(names[:MAX_LIST]) + f"\n... and {count - MAX_LIST} more entries"
                    else:
                        display = "\n".join(names) if names else "(empty archive)"
                    if debug:
                        print(f"[TOOL] list_archive: {archive_path} ({count} entries)")
                    results.append(display)
                except Exception as e:
                    results.append(f"Error reading archive: {e}")
            elif arch_type == 'zip':
                try:
                    with (Spinner(prefix=f"[TOOL] list_archive", color=GREEN) if debug else contextlib.nullcontext()):
                        with zipfile.ZipFile(arch_resolved, 'r') as zf:
                            names = zf.namelist()
                    if pattern:
                        names = [n for n in names if fnmatch.fnmatch(n, pattern)]
                    count = len(names)
                    MAX_LIST = 100
                    if count > MAX_LIST:
                        display = "\n".join(names[:MAX_LIST]) + f"\n... and {count - MAX_LIST} more entries"
                    else:
                        display = "\n".join(names) if names else "(empty archive)"
                    if debug:
                        print(f"[TOOL] list_archive: {archive_path} ({count} entries)")
                    results.append(display)
                except Exception as e:
                    results.append(f"Error reading archive: {e}")
            else:
                results.append(f"Error: Unsupported archive format: {archive_path}")
        elif tool_name == "read_file_from_archive":
            archive_path = tool_input.get("archive_path")
            file_path = tool_input.get("file_path")
            if not archive_path:
                results.append("Error: read_file_from_archive requires 'archive_path' and 'file_path' arguments")
                continue
            if not file_path:
                results.append("Error: read_file_from_archive requires a 'file_path' argument")
                continue
            if not _is_safe_archive_path(file_path):
                results.append(f"Error: Invalid path inside archive: {file_path} (absolute or ../ is not allowed)")
                continue
            arch_resolved = resolve_path(archive_path, workspace_dir)
            if arch_resolved is None or not manager._is_safe_path(arch_resolved):
                results.append(f"Error: {archive_path} is outside the workspace directory.")
                continue
            if not arch_resolved.is_file():
                results.append(f"Error: Archive not found: {archive_path}")
                continue
            arch_type = _archive_type(arch_resolved)
            if arch_type == 'tar':
                try:
                    with (Spinner(prefix=f"[TOOL] read_archive", color=GREEN) if debug else contextlib.nullcontext()):
                        with tarfile.open(arch_resolved, 'r:*') as tar:
                            try:
                                info = tar.getmember(file_path)
                            except KeyError:
                                resolved = _resolve_archive_path(tar, file_path)
                                if resolved == file_path:
                                    results.append(f"Error: File not found in archive: {file_path}")
                                    continue
                                file_path = resolved
                                info = tar.getmember(file_path)
                            if info.issym() or info.islnk():
                                results.append(f"Error: Refusing to read symlink/hardlink: {file_path}")
                                continue
                            if info.size > MAX_ARCHIVE_READ_SIZE:
                                results.append(f"Error: File too large ({info.size} bytes, limit {MAX_ARCHIVE_READ_SIZE})")
                                continue
                            f = tar.extractfile(info)
                            if f is None:
                                results.append(f"Error: Cannot read {file_path} (directory or special file)")
                                continue
                            content = f.read().decode('utf-8', errors='replace')
                            _offset = tool_input.get("offset")
                            _limit = tool_input.get("limit")
                            if _offset is not None or _limit is not None:
                                content = content[_offset or 0:(_offset or 0) + _limit if _limit is not None else None]
                        print(f"[TOOL] read_file_from_archive: {archive_path}/{file_path} ({len(content)} chars)" +
                              (f" (offset={_offset}, limit={_limit})" if any(k in tool_input for k in ("offset", "limit")) else ""))
                        results.append(content)
                except Exception as e:
                    results.append(f"Error reading from archive: {e}")
            elif arch_type == 'zip':
                try:
                    with (Spinner(prefix=f"[TOOL] read_archive", color=GREEN) if debug else contextlib.nullcontext()):
                        with zipfile.ZipFile(arch_resolved, 'r') as zf:
                            try:
                                info = zf.getinfo(file_path)
                            except KeyError:
                                resolved = _resolve_archive_path(zf, file_path)
                                if resolved == file_path:
                                    results.append(f"Error: File not found in archive: {file_path}")
                                    continue
                                file_path = resolved
                                info = zf.getinfo(file_path)
                            if info.external_attr >> 16 & 0o120000 == 0o120000:
                                results.append(f"Error: Refusing to read symlink/hardlink: {file_path}")
                                continue
                            if info.file_size > MAX_ARCHIVE_READ_SIZE:
                                results.append(f"Error: File too large ({info.file_size} bytes, limit {MAX_ARCHIVE_READ_SIZE})")
                                continue
                            content = zf.read(file_path).decode('utf-8', errors='replace')
                            _offset = tool_input.get("offset")
                            _limit = tool_input.get("limit")
                            if _offset is not None or _limit is not None:
                                content = content[_offset or 0:(_offset or 0) + _limit if _limit is not None else None]
                        print(f"[TOOL] read_file_from_archive: {archive_path}/{file_path} ({len(content)} chars)" +
                              (f" (offset={_offset}, limit={_limit})" if any(k in tool_input for k in ("offset", "limit")) else ""))
                        results.append(content)
                except Exception as e:
                    results.append(f"Error reading from archive: {e}")
            else:
                results.append(f"Error: Unsupported archive format: {archive_path}")
        else:
            results.append(f"Error: Unknown tool '{tool_name}'")
    return results
