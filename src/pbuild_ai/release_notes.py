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

"""Release notes from the changelog file shipped in a source archive.

Projects without GitHub/GitLab releases often keep their history in
CHANGES.rst, CHANGELOG.md, NEWS, ... inside the tarball. The sections for
the versions in (old, new] are returned as ``## <version>`` blocks, the
same format the releases API path produces.
"""

import re
import tarfile
import zipfile
from pathlib import Path

_CHANGELOG_NAME_RE = re.compile(
    r'^(changes|changelog|news|history|release[-_]?notes|whatsnew)(\.(rst|md|markdown|txt))?$', re.I)
_MAX_FILE_SIZE = 2 * 1024 * 1024
_ARCHIVE_SUFFIXES = ('.tar.gz', '.tgz', '.tar.xz', '.txz', '.tar.bz2', '.tbz2', '.tar.zst', '.tar', '.zip')
# '3.1.0', 'v3.1.0', 'Version 3.1.0', '[3.1.0]', '3.1.0 rc 2', '3.1.0b1',
# each optionally followed by a date or other text after '(' '-' ':'.
_VERSION_HEADING_RE = re.compile(
    r'^(?:#{1,6}\s*)?\[?(?:version\s+|release\s+)?v?(\d+(?:\.\d+)+)'
    r'(\s*[-.]?\s*(?:a|b|c|rc|alpha|beta|pre|preview|dev)\s*\.?\s*\d*)?\]?'
    r'(?:\s*[(\-–:].*)?\s*$', re.I)
_UNDERLINE_RE = re.compile(r'^\s*([=\-~^*#+])\1{2,}\s*$')


def _version_key(v):
    return tuple(int(x) for x in v.split('.'))


def _headings(lines):
    """[(line index, base version, prerelease suffix, heading line count)]."""
    result = []
    for i, line in enumerate(lines):
        m = _VERSION_HEADING_RE.match(line.rstrip())
        if not m or line[:1].isspace():
            continue
        underlined = i + 1 < len(lines) and _UNDERLINE_RE.match(lines[i + 1])
        if line.startswith('#') or underlined or re.match(r'(?i)(version|release)\s', line):
            result.append((i, m.group(1), (m.group(2) or '').strip(), 2 if underlined else 1))
    return result


def changelog_sections(text, old_version, new_version, budget=10000):
    """``## <version>`` blocks for every version in (old_version, new_version].

    Prerelease sections (3.1.0b1, 3.1.0 rc 2) are folded into their release:
    the final section of such a release often only says 'no changes since
    rc 2'. Blocks are oldest first; '' when nothing matches."""
    lines = (text or '').splitlines()
    heads = _headings(lines)
    try:
        old_key = _version_key(re.match(r'\d+(?:\.\d+)*', old_version).group(0)) if old_version else ()
        new_key = _version_key(re.match(r'\d+(?:\.\d+)*', new_version).group(0))
    except AttributeError:
        return ''
    grouped = {}
    for n, (i, version, _pre, size) in enumerate(heads):
        key = _version_key(version)
        if not old_key < key <= new_key:
            continue
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        body = '\n'.join(lines[i + size:end]).strip()
        if body:
            grouped.setdefault(key, (version, []))[1].append(body)
    # Every release gets its share of the budget, like the releases API path.
    share = budget // max(len(grouped), 1)
    blocks = []
    for _key, (version, bodies) in sorted(grouped.items()):
        body = '\n\n'.join(bodies)
        blocks.append(f"## {version}\n\n" + (body if len(body) <= share else body[:share] + ' ...'))
    return '\n\n'.join(blocks)


def _find_changelog(names):
    """The most likely changelog file among archive member *names*."""
    best = None
    for name in names:
        parts = [p for p in name.split('/') if p]
        if not parts or not _CHANGELOG_NAME_RE.match(parts[-1]):
            continue
        # <top>/CHANGES.rst beats <top>/docs/changes.rst
        rank = (len(parts), 0 if parts[-1].isupper() or parts[-1][:1].isupper() else 1)
        if best is None or rank < best[0]:
            best = (rank, name)
    return best[1] if best else None


def _read_changelog(archive):
    archive = Path(archive)
    if archive.name.endswith('.zip'):
        with zipfile.ZipFile(archive) as z:
            name = _find_changelog(z.namelist())
            if not name or z.getinfo(name).file_size > _MAX_FILE_SIZE:
                return None, None
            return name, z.read(name).decode('utf-8', errors='replace')
    with tarfile.open(archive, 'r:*') as t:
        members = {m.name: m for m in t.getmembers() if m.isfile()}
        name = _find_changelog(members)
        if not name or members[name].size > _MAX_FILE_SIZE:
            return None, None
        return name, t.extractfile(members[name]).read().decode('utf-8', errors='replace')


def archive_release_notes(spec_dir, old_version, new_version, log=print):
    """Release notes for (old, new] from the changelog file in the source
    archive of *new_version* next to the spec; '' when none is found."""
    archives = sorted(p for p in Path(spec_dir).iterdir()
                      if p.is_file() and new_version in p.name and p.name.endswith(_ARCHIVE_SUFFIXES))
    for archive in archives:
        try:
            name, text = _read_changelog(archive)
        except (OSError, tarfile.TarError, zipfile.BadZipFile, EOFError) as e:
            log(f"[CHANGELOG] Could not read {archive.name}: {e}")
            continue
        if not name:
            continue
        notes = changelog_sections(text, old_version, new_version)
        if notes:
            log(f"[CHANGELOG] Using release notes from {name} in {archive.name}.")
            return notes
    return ''
