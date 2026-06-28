from pathlib import Path


def resolve_path(path_str, workspace_dir, for_write=False):
    """Resolve a path from the model relative to workspace_dir.
    Tries: exact path, basename only, and stripped package prefix.
    Returns None if path escapes workspace_dir."""
    if "\x00" in str(path_str):
        return None
    workspace = Path(workspace_dir).resolve()
    candidates = [
        workspace / path_str,
        workspace / Path(path_str).name,
    ]
    parts = Path(path_str).parts
    if len(parts) >= 2:
        cand = workspace / Path(*parts[1:])
        candidates.append(cand)
    for p in candidates:
        try:
            resolved = p.resolve()
            try:
                resolved.relative_to(workspace)
            except ValueError:
                continue
            if resolved.exists() or for_write:
                return resolved
        except (OSError, ValueError):
            continue
    last = (workspace / path_str).resolve()
    try:
        last.relative_to(workspace)
        return last
    except ValueError:
        return None
