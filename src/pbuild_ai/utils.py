import hashlib
from pathlib import Path


def ranges_covered(ranges, start, end):
    for rs, re in ranges:
        if rs <= start and (re is None or (end is not None and re >= end)):
            return True
    return False


def ranges_merge(ranges, start, end):
    if not ranges:
        return [(start, end)]
    for rs, re in ranges:
        if rs <= start and (re is None or (end is not None and re >= end)):
            return ranges
    new_ranges = [r for r in ranges if not (start <= r[0] and (end is None or (r[1] is not None and end >= r[1])))]
    new_ranges.append((start, end))
    new_ranges.sort()
    merged = []
    for rs, re in new_ranges:
        if merged:
            lrs, lre = merged[-1]
            if lre is None or (rs is not None and rs <= (lre if lre is not None else rs)):
                if re is None:
                    merged[-1] = (lrs, None)
                elif lre is not None and re > lre:
                    merged[-1] = (lrs, re)
                continue
        merged.append((rs, re))
    return merged


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


class ReadCoverageTracker:
    """Tracks which file ranges have been read to avoid redundant read tool calls.

    Maintains coverage for read_file (with content hash invalidation) and
    read_file_from_archive (immutable content, no hash check).
    """

    def __init__(self):
        self._file_coverage = {}
        self._archive_coverage = {}

    def filter_reads(self, round_calls, workspace_dir, manager=None):
        """Check read_file/read_file_from_archive calls against current coverage.

        Returns (filtered_calls, skipped_lookup) where filtered_calls contains
        only calls not covered, and skipped_lookup maps original index to skip message.
        """
        skipped = {}
        for ci, (name, inp) in enumerate(round_calls):
            if name == "read_file":
                path = inp.get("path", "")
                resolved = resolve_path(path, workspace_dir) if workspace_dir else None
                if resolved and resolved.exists():
                    offset = inp.get("offset")
                    limit = inp.get("limit")
                    start = offset if offset is not None else 0
                    end = start + limit if limit is not None else None
                    prev = self._file_coverage.get(str(resolved))
                    if prev and manager:
                        current_hash = hashlib.md5(manager.read_file_safe(resolved).encode()).hexdigest()
                        if prev["hash"] == current_hash and ranges_covered(prev["ranges"], start, end):
                            skipped[ci] = f"READ SKIP: {path} already read \u2014 see earlier tool result"
            elif name == "read_file_from_archive":
                archive_path = inp.get("archive_path", "")
                file_path = inp.get("file_path", "")
                arch_resolved = resolve_path(archive_path, workspace_dir) if workspace_dir else None
                if arch_resolved:
                    offset = inp.get("offset")
                    limit = inp.get("limit")
                    start = offset if offset is not None else 0
                    end = start + limit if limit is not None else None
                    cache_key = (str(arch_resolved), file_path)
                    if ranges_covered(self._archive_coverage.get(cache_key, []), start, end):
                        skipped[ci] = f"READ SKIP: {archive_path}/{file_path} already read \u2014 see earlier tool result"

        filtered = [c for ci, c in enumerate(round_calls) if ci not in skipped]
        return filtered, skipped

    def update_from_results(self, round_calls, round_results, workspace_dir, manager=None):
        """Update coverage from executed read results.

        round_calls: list of (name, inp) tuples
        round_results: list of result strings (same length as round_calls)
        """
        for (name, inp), r in zip(round_calls, round_results):
            if name == "read_file" and r and not r.startswith(("Error", "READ SKIP", "OK:")):
                path = inp.get("path", "")
                resolved = resolve_path(path, workspace_dir) if workspace_dir else None
                if resolved and resolved.exists():
                    current_hash = ""
                    if manager:
                        current_hash = hashlib.md5(manager.read_file_safe(resolved).encode()).hexdigest()
                    offset = inp.get("offset")
                    limit = inp.get("limit")
                    start = offset if offset is not None else 0
                    end = start + limit if limit is not None else None
                    prev = self._file_coverage.get(str(resolved), {"hash": "", "ranges": []})
                    self._file_coverage[str(resolved)] = {
                        "hash": current_hash,
                        "ranges": ranges_merge(prev["ranges"], start, end)
                    }
            elif name == "read_file_from_archive" and r and not r.startswith(("Error", "READ SKIP", "OK:")):
                archive_path = inp.get("archive_path", "")
                file_path = inp.get("file_path", "")
                arch_resolved = resolve_path(archive_path, workspace_dir) if workspace_dir else None
                if arch_resolved:
                    cache_key = (str(arch_resolved), file_path)
                    offset = inp.get("offset")
                    limit = inp.get("limit")
                    start = offset if offset is not None else 0
                    end = start + limit if limit is not None else None
                    prev_ranges = self._archive_coverage.get(cache_key, [])
                    self._archive_coverage[cache_key] = ranges_merge(prev_ranges, start, end)

    @staticmethod
    def merge_results(round_calls, filtered_results, skipped_lookup):
        """Reconstruct results list in original round_calls order.

        filtered_results: results from executing filtered_calls
        skipped_lookup: dict from filter_reads()
        Returns list of result strings matching len(round_calls).
        """
        results = []
        fi = 0
        for ci in range(len(round_calls)):
            if ci in skipped_lookup:
                results.append(skipped_lookup[ci])
            else:
                results.append(filtered_results[fi])
                fi += 1
        return results
