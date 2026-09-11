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

import re
from pathlib import Path


def apply_build_order(spec_files, workspace_dir, package_filter, agents_md_content):
    """Parse AGENTS.md for build order hints, reorder spec_files, and skip unwanted packages."""
    if not agents_md_content and not package_filter:
        return
    scripts_dir = Path(workspace_dir) / "tool-scripts"
    if not scripts_dir.is_dir():
        scripts_dir = Path(workspace_dir)
    # When a specific package is requested on CLI, prioritize it
    if package_filter:
        for i, s in enumerate(spec_files):
            if s.stem == package_filter:
                spec_files.insert(0, spec_files.pop(i))
                break
        _, _, skip_pkgs = parse_agents_md_scripts(agents_md_content or "", scripts_dir)
        if skip_pkgs:
            effective_skip = [p for p in skip_pkgs if p != package_filter]
            if effective_skip != skip_pkgs:
                print(f"[INFO] Package '{package_filter}' explicitly requested, ignoring skip rule for it.")
            skipped = [s for s in spec_files if s.stem in effective_skip]
            if skipped:
                print(f"[INFO] Skipping packages (per AGENTS.md): {', '.join(s.stem for s in skipped)}")
            spec_files[:] = [s for s in spec_files if s.stem not in effective_skip]
        return
    _, build_order_hints, skip_pkgs = parse_agents_md_scripts(agents_md_content or "", scripts_dir)
    if skip_pkgs:
        skipped = [s for s in spec_files if s.stem in skip_pkgs]
        if skipped:
            print(f"[INFO] Skipping packages (per AGENTS.md): {', '.join(s.stem for s in skipped)}")
        spec_files[:] = [s for s in spec_files if s.stem not in skip_pkgs]
    if build_order_hints:
        print(f"[INFO] AGENTS.md build order hint: {', '.join(build_order_hints)}")
        hinted = [p for p in build_order_hints if any(s.stem == p for s in spec_files)]
        remaining = [s for s in spec_files if s.stem not in build_order_hints]
        reordered = []
        for pkg_name in hinted:
            for s in list(spec_files):
                if s.stem == pkg_name:
                    reordered.append(s)
                    break
        reordered.extend(remaining)
        spec_files[:] = reordered


def _strip_spec_suffix(name):
    """Normalise a matched package name to its spec stem.

    Strips ``.spec`` / ``.rpm`` suffixes and trailing version numbers
    (``-1.0``) so the result matches ``Path.stem`` of the spec file.
    Only strips ``-MAJOR.MINOR`` patterns to avoid mangling names like
    ``python3-foo`` (where ``-3`` is part of the name).
    """
    for suffix in ('.spec', '.rpm'):
        if name.lower().endswith(suffix):
            name = name[:-len(suffix)]
    name = re.sub(r'-\d+\.\d+[^-]*$', '', name)
    return name


def _pkg_match(line):
    """Return first package-name match in *line* (word chars, dots, dashes, plusses)."""
    m = re.search(r'([\w][\w\-\.\+]*)', line)
    return _strip_spec_suffix(m.group(1)) if m else None


def _skip_prepositions(match_iter):
    """Yield matches from *match_iter* but skip common English prepositions/articles."""
    SKIP = {'for', 'in', 'to', 'of', 'at', 'on', 'by', 'the', 'a', 'an', 'during', 'from'}
    for m in match_iter:
        word = m.group(1).lower()
        if word not in SKIP:
            return _strip_spec_suffix(m.group(1))
    return None


def parse_failed_package(build_out):
    """Extract the failing package name from build output.

    Tries (in order):
    1. Lines containing ``failed`` / ``failure``.
    2. ``unresolvable`` lines – looks for a ``building`` line just before
       the unresolvable line, or for ``needed by`` / ``required by`` in
       the unresolvable message itself (current or next line).
    3. The last ``building`` / ``##`` line (most recently started pkg).
    4. Any ``building`` / ``Processing`` line mentioning a package.
    Returns the package stem or *None*.
    """
    lines = build_out.split('\n')

    for line in lines:
        it = re.finditer(r'(?:failed|failure)\s+(?:\w+\s+){0,3}([\w][\w\-\.\+]*)', line, re.I)
        result = _skip_prepositions(it)
        if result:
            return result

    for idx, line in enumerate(lines):
        if re.search(r'unresolvable', line, re.I):
            pkg = _package_before_line(lines, idx)
            if pkg:
                return pkg
            m = re.search(r'(?:needed by|required by)\s+([\w][\w\-\.\+]*)', line, re.I)
            if m:
                return _strip_spec_suffix(m.group(1))
            if idx + 1 < len(lines):
                m = re.search(r'(?:needed by|required by)\s+([\w][\w\-\.\+]*)', lines[idx + 1], re.I)
                if m:
                    return _strip_spec_suffix(m.group(1))

    for line in reversed(lines):
        for prefix in ('building', '##', 'Processing'):
            m = re.search(rf'{prefix}\s+([\w][\w\-\.\+]*)', line, re.I)
            if m:
                return _strip_spec_suffix(m.group(1))
    return None


def _package_before_line(lines, idx):
    """Look backwards from *idx* for a ``building`` / ``Processing`` line."""
    for i in range(idx - 1, -1, -1):
        m = re.search(r'(?:building|Processing)\s+([\w][\w\-\.\+]*)', lines[i], re.I)
        if m:
            return _strip_spec_suffix(m.group(1))
    return None


def parse_agents_md_scripts(agents_text, scripts_dir):
    """Parse AGENTS.md for startup script hints, build order, and skip/ignore hints.
    Returns (startup_scripts, build_order_hints, skip_packages).
    """
    startup = []
    build_order = []
    skip_pkgs = []

    if not agents_text:
        return startup, build_order, skip_pkgs

    lines = agents_text.split("\n")
    in_startup_section = False
    in_build_order_section = False
    in_skip_section = False

    for line in lines:
        stripped = line.strip()

        section_lower = stripped.lower()
        if stripped.startswith("#") and "startup" in section_lower:
            in_startup_section = True
            in_build_order_section = False
            in_skip_section = False
            continue
        if stripped.startswith("#") and ("build order" in section_lower or "build sequence" in section_lower):
            in_build_order_section = True
            in_startup_section = False
            in_skip_section = False
            continue
        if stripped.startswith("#") and any(kw in section_lower for kw in ("skip", "ignore", "exclude")):
            in_skip_section = True
            in_startup_section = False
            in_build_order_section = False
            continue
        if stripped.startswith("#") and not stripped.startswith("##"):
            in_startup_section = False
            in_build_order_section = False
            in_skip_section = False

        m = re.match(r"^startup-script:\s*(\S+)", stripped, re.I)
        if m:
            startup.append(m.group(1))
            continue

        if in_startup_section:
            m = re.match(r"^[-*]\s*(?:`?tool-scripts/)?(\S+)`?", stripped)
            if m:
                startup.append(m.group(1))
                continue

        if in_startup_section and scripts_dir.is_dir():
            for f in scripts_dir.iterdir():
                if f.is_file() and f.name in stripped:
                    if f.name not in startup:
                        startup.append(f.name)

        if in_build_order_section:
            m = re.match(r"^[-*\d+\.]\s*([\w\-\.\+]+)", stripped)
            if m:
                build_order.append(m.group(1))
                continue

        lang_patterns = [
            r"(?:build|package)\s+(?:order|sequence)\s*[:\-]\s*(.+)",
            r"start\s+with\s+([\w\-\.\+]+)",
            r"begin\s+with\s+([\w\-\.\+]+)",
            r"first\s+build\s+([\w\-\.\+]+)",
            r"build\s+([\w\-\.\+]+)\s+first",
            r"start\s+([\w\-\.\+]+)\s+first",
            r"start\s+by\s+building\s+([\w\-\.\+]+)",
        ]
        for pat in lang_patterns:
            m = re.search(pat, stripped, re.I)
            if m:
                pkgs = re.findall(r"[\w\-\.\+]+", m.group(1))
                for pkg in pkgs:
                    if pkg not in build_order:
                        build_order.append(pkg)
                break

        skip_patterns = [
            r"(?:skip|ignore|exclude|do\s+not\s+build)\s*[:\-]\s*(.+)",
            r"(?:skip|ignore)\s+(?:package|packages|building)\s+([\w\-\.\+]+)",
        ]
        if in_skip_section:
            m = re.match(r"^[-*\d+\.]\s*([\w\-\.\+]+)", stripped)
            if m:
                pkgs = re.findall(r"[\w\-\.\+]+", m.group(1))
                skip_pkgs.extend(pkgs)
                continue
        for pat in skip_patterns:
            m = re.search(pat, stripped, re.I)
            if m:
                pkgs = re.findall(r"[\w\-\.\+]+", m.group(1))
                skip_pkgs.extend(pkgs)
                break

    seen = set()
    unique_startup = [s for s in startup if not (s in seen or seen.add(s))]
    seen = set()
    unique_order = [p for p in build_order if not (p in seen or seen.add(p))]
    seen = set()
    unique_skip = [p for p in skip_pkgs if not (p in seen or seen.add(p))]

    return unique_startup, unique_order, unique_skip


_POST_UPDATE_KEYWORDS = ("version", "update", "upgrade", "after", "post", "changed")
_POST_UPDATE_SCRIPT_RE = re.compile(
    r'(?<![A-Za-z0-9_\.\/\-#])((?:\.agents/skills/|tool-scripts/|skills/)?[A-Za-z0-9][A-Za-z0-9._-]*\.sh)'
)


def parse_post_update_scripts(agents_text):
    """Extract script references that AGENTS.md mandates after a package version
    change.  A script is considered post-update when it appears on (or right
    after) a line mentioning a version change, or under an explicit
    ``post-update``/``post-update-script:`` marker.  Returns a deduplicated list
    in document order."""
    if not agents_text:
        return []
    found = []
    seen = set()
    in_post_section = False
    prev_hint = False
    for raw in agents_text.splitlines():
        stripped = raw.strip()
        lower = stripped.lower()
        if stripped.startswith("#") and ("post-update" in lower or "post update" in lower):
            in_post_section = True
            continue
        if stripped.startswith("#"):
            if not stripped.startswith("##"):
                in_post_section = False
        marker_match = re.match(r'^post-update-script:\s*(\S+)', stripped, re.I)
        if marker_match:
            candidates = [marker_match.group(1)]
        else:
            candidates = re.findall(_POST_UPDATE_SCRIPT_RE, stripped)
        hint = not stripped.startswith("#") and any(k in lower for k in _POST_UPDATE_KEYWORDS)
        for ref in candidates:
            if not ref:
                continue
            if in_post_section or hint or prev_hint or marker_match:
                if ref not in seen:
                    seen.add(ref)
                    found.append(ref)
        prev_hint = hint
    return found


def fix_remote_asset_formatting(spec_text):
    """Repair mangled `#!RemoteAsset` / `#!CreateArchive` lines in a spec file.

    Returns (text, changed, notes). A `#!RemoteAsset:` line immediately followed
    by a `#!CreateArchive` line is the correct OBS form and is never touched —
    only same-line merges, glued lines and missing markers are repaired.
    """
    notes = []
    _spec_current = spec_text
    fixed = False

    # Case 1: #!RemoteAsset inline on a Source: line — move it to its own line
    m_src = re.search(r'^(Source\d*:\s*)(#!RemoteAsset:[^\n]+\s*)(.*)$', _spec_current, re.M)
    if m_src:
        replacement = f'  {m_src.group(2).strip()}\n{m_src.group(1)}{m_src.group(3).strip()}'
        _spec_current = _spec_current.replace(m_src.group(0), replacement)
        fixed = True
        notes.append("moved inline #!RemoteAsset onto its own line")

    # Case 2: #!CreateArchive merged onto the #!RemoteAsset: line. Only horizontal
    # whitespace counts here — a #!CreateArchive on the NEXT line is the correct
    # OBS form and must not be treated as a merge.
    m_merged = re.search(r'(#!RemoteAsset:[^\n]+?)[ \t]+#?!?CreateArchive[^\n]*', _spec_current)
    if m_merged:
        _spec_current = _spec_current.replace(m_merged.group(0), m_merged.group(1))
        fixed = True
        notes.append("split #!CreateArchive off the #!RemoteAsset line")

    # Case 3: #!CreateArchive glued to the end of another line. A standalone
    # #!CreateArchive line is always left alone. When the marker belongs to a
    # #!RemoteAsset: above it, only the remainder of the line is kept — the pass
    # below re-inserts #!CreateArchive at the position OBS expects.
    split_lines = []
    seen_asset = False
    for line in _spec_current.split('\n'):
        if line.startswith('#!RemoteAsset:'):
            seen_asset = True
        if '#!CreateArchive' not in line or line.strip() == '#!CreateArchive':
            split_lines.append(line)
            continue
        rest = re.sub(r'#?!{0,2}CreateArchive\s*$', '', line).strip()
        if rest and not seen_asset:
            split_lines.append(rest)
            split_lines.append('#!CreateArchive')
        elif rest:
            split_lines.append(rest)
        fixed = True
        notes.append("split glued #!CreateArchive off the line")
    _spec_current = '\n'.join(split_lines)

    # Case 4: Source: was renamed to Source0: while a RemoteAsset is present
    if ('#!RemoteAsset:' in _spec_current and 'Source0:' in _spec_current
            and 'Source:' not in _spec_current):
        _spec_current = _spec_current.replace('Source0:', 'Source:')
        fixed = True
        notes.append("restored Source0: to Source:")

    # Every #!RemoteAsset: needs its own #!CreateArchive line right below it
    lines = _spec_current.split('\n')
    rebuilt = []
    for idx, line in enumerate(lines):
        rebuilt.append(line)
        if not line.startswith('#!RemoteAsset:'):
            continue
        nxt = lines[idx + 1] if idx + 1 < len(lines) else ''
        if idx + 1 < len(lines) and lines[idx + 1].strip() == '#!CreateArchive':
            continue
        rebuilt.append('#!CreateArchive')
        fixed = True
        notes.append("added missing #!CreateArchive after #!RemoteAsset")
    _spec_current = '\n'.join(rebuilt)

    # Case 6: strip the invalid 'filename::url' syntax from Source lines
    m_dc = re.search(r'^(Source\d*:\s*)(\S+)::(https?://\S+)', _spec_current, re.M)
    if m_dc:
        _spec_current = _spec_current.replace(m_dc.group(0), m_dc.group(1) + m_dc.group(3))
        fixed = True
        notes.append("removed invalid 'filename::url' Source syntax")

    return _spec_current, fixed, notes


def extract_spec(text):
    t = text.strip()
    m = re.search(r"```(?:spec)?\s*\n(.*?)```", t, re.DOTALL)
    if m:
        return m.group(1).strip()
    prefixes = ("%", "Name:", "Summary:", "Version:", "Release:", "License:", "Group:",
                "BuildRequires:", "Requires:", "Source:", "Patch:", "Url:", "Prefix:",
                "Epoch:", "Vendor:", "Packager:", "ExclusiveArch:", "ExcludeArch:", "#")
    lines = t.split("\n")
    start = next((i for i, l in enumerate(lines) if l.strip() and any(l.strip().startswith(p) for p in prefixes)), None)
    if start is not None:
        return "\n".join(lines[start:]).strip()
    return t


def find_rpm_tags(text):
    tags = set()
    for m in re.finditer(r'^(BuildRequires|Requires|Recommends|Suggests|Supplements|Conflicts|Obsoletes|Provides)\s*:.+$', text, re.MULTILINE):
        tags.add(m.group(0).strip())
    for fence in re.findall(r'```(?:spec)?\s*\n(.*?)```', text, re.DOTALL):
        for m in re.finditer(r'^(BuildRequires|Requires|Recommends|Suggests|Supplements|Conflicts|Obsoletes|Provides)\s*:.+$', fence, re.MULTILINE):
            tags.add(m.group(0).strip())
    return sorted(tags)


def apply_spec_insertions(spec_lines, lines_to_add):
    modified = False
    for line_to_add in lines_to_add:
        if line_to_add.strip() in [l.strip() for l in spec_lines]:
            print(f"[FIX] Already present, skipping: {line_to_add}", flush=True)
            continue
        insert_pos = None
        for i in range(len(spec_lines) - 1, -1, -1):
            if spec_lines[i].strip().startswith("BuildRequires:"):
                j = i + 1
                while j < len(spec_lines) and spec_lines[j].strip().startswith("%"):
                    if spec_lines[j].strip().startswith("%endif"):
                        insert_pos = j + 1
                    j += 1
                if insert_pos is None:
                    insert_pos = i + 1
                break
        if insert_pos is None:
            for i, l in enumerate(spec_lines):
                if l.strip().startswith(("Name:", "Summary:", "Version:")):
                    insert_pos = i + 1
            if insert_pos is None:
                insert_pos = len(spec_lines)
        spec_lines.insert(insert_pos, line_to_add)
        modified = True
        print(f"[FIX] Inserted: {line_to_add}", flush=True)
    return spec_lines, modified
