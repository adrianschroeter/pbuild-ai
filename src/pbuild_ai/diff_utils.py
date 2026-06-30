import difflib
import os


_COLOR = os.environ.get("NO_COLOR", "").lower() not in ("1", "true", "yes")


def _green(text):
    return f"\033[32m{text}\033[0m" if _COLOR else text


def _red(text):
    return f"\033[31m{text}\033[0m" if _COLOR else text


def _cyan(text):
    return f"\033[36m{text}\033[0m" if _COLOR else text


def show_diff(old_content, new_content, file_path, prefix="[FIX]"):
    """Print a colorized diff between old and new file content."""
    if old_content == new_content:
        return
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = list(difflib.unified_diff(old_lines, new_lines, fromfile=str(file_path), tofile=str(file_path), n=3))
    if not diff:
        return
    print(f"{prefix} --- Diff for {file_path} ---")
    for line in diff:
        if line.startswith("+"):
            print(f"{_green(prefix)} {_green(line)}", end="")
        elif line.startswith("-"):
            print(f"{_red(prefix)} {_red(line)}", end="")
        elif line.startswith("@@"):
            print(f"{_cyan(prefix)} {_cyan(line)}", end="")
        else:
            print(f"{prefix} {line}", end="")
    print(f"{prefix} --- End diff ---")
