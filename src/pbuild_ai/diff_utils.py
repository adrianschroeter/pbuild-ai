import difflib


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
            print(f"\033[32m{prefix} {line}\033[0m", end="")
        elif line.startswith("-"):
            print(f"\033[31m{prefix} {line}\033[0m", end="")
        elif line.startswith("@@"):
            print(f"\033[36m{prefix} {line}\033[0m", end="")
        else:
            print(f"{prefix} {line}", end="")
    print(f"{prefix} --- End diff ---")
