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


def parse_failed_package(build_out):
    for line in build_out.split('\n'):
        m = re.search(r'(?:failed|failure).*?([\w][\w\-\.\+]*)', line, re.I)
        if m:
            return m.group(1)
    for line in reversed(build_out.split('\n')):
        m = re.search(r'(?:building|##)\s+([\w][\w\-\.\+]*)', line, re.I)
        if m:
            return m.group(1)
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

        if scripts_dir.is_dir():
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
