import datetime
import re

CHANGELOG_PROMPT = """
## .changes file format (openSUSE policy)

The `.changes` file uses entries separated by `---` lines. There must be a
blank line BEFORE each `---` line (except at the very top of the file).

Correct example:

-------------------------------------------------------------------
Mon Jun 29 12:00:00 UTC 2026 - pbuild-ai <email@suse.de>

- Updated to version 1.2.3
  * upstream changelog details here
- Updated to version 1.2.2
  * upstream changelog details here
- Remove old-patch.patch (upstream applied the fix)
- Update generated using pbuild-ai

-------------------------------------------------------------------
Tue Nov  8 10:21:12 UTC 2022 - Previous Author <email>

- Earlier entry

### Rules:
- Always prepend new entries at the top. Never modify or remove older entries.
- Replace `<email@suse.de>` with the actual email from the spec. Do NOT leave
  `<EMAIL>` as a literal placeholder — substitute it with a real address.
- Entry body uses `- ` bullet lines.
- Keep every line of an entry at most 70 characters wide (the format is
  column-based and diffs stay readable). Wrap long bullets onto continuation
  lines indented with `  ` rather than exceeding 70 chars.
- Do NOT end your new entry with a `---` separator line: exactly one `---`
  line separates two entries, and the older entry below already begins with
  its own. Two consecutive `---` lines are invalid.
- When removing a `Patch:` line, name the exact patch filename and state why.
- End each entry body with `- Update generated using pbuild-ai`.
- When the .changes file does not exist, create it.
- When the version jump spans former upstream releases — i.e. the current
  package source is older than some releases that upstream shipped in between —
  cover those former releases in the entry as well: name the skipped
  intermediate versions and their notable highlights, because the package
  previously did not ship them. For each version an own entry, e.g.
  `- Updated to version 1.3 with sub-bullets noting highlights.
   - Updated to version 1.2 with sub-bullets noting highlights.`
"""


def split_release_notes(notes):
    """Split combined release notes into ``[(version, scalar_text), ...]``.

    Accepts both plain notes (no ``## <version>`` headings → a single entry
    with version ``None``) and multi-version blocks produced by the GitHub or
    GitLab prefetch (each ``## <version>`` heading starts a section).
    """
    if not notes:
        return []
    lines = notes.splitlines()
    sections = []
    current_header = None
    current_lines = []
    for line in lines:
        m = _RELEASE_NOTES_VERSION_HEADER_RE.match(line)
        if m:
            if current_header is not None or current_lines:
                sections.append((current_header, '\n'.join(current_lines)))
            current_header = m.group(1)
            current_lines = []
        else:
            current_lines.append(line)
    if current_header is not None or (current_lines and not sections):
        sections.append((current_header, '\n'.join(current_lines)))
    if not sections and notes.strip():
        sections.append((None, notes))
    return sections


def sanitize_release_notes(notes, max_bullets=4, max_chars_per_line=120):
    """Convert upstream release notes into a few compact .changes bullet lines.

    When ``notes`` contains ``## <version>`` headings, each version section is
    converted into ``  * <version>: ...`` sub-bullets (capped at max two per
    version, and ``max_bullets`` overall).  Plain notes keep the existing
    behaviour of returning ``  * ...`` sub-bullets.

    Deterministic transformer (no AI): strips HTML/markdown markup and turns
    the leading meaningful lines into sub-bullets suitable for a .changes
    entry body.
    """

    def _sanitize_section(text, limit):
        if not text:
            return []
        text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)
        text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
        text = re.sub(r'`([^`]*)`', r'\1', text)
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'\1', text)
        text = re.sub(r'^#{1,6}\s*', '', text, flags=re.M)
        text = re.sub(r'^\s*>\s*', '', text, flags=re.M)
        bullets = []
        for line in text.splitlines():
            line = re.sub(r'^\s*[-*+]\s*', '', line).strip()
            if len(line) < 2:
                continue
            low = line.lower()
            if low.startswith(('http://', 'https://', '#')):
                continue
            if low.startswith(("what's changed", "what's new", "release notes", "changelog", "highlight")):
                continue
            line = line[:max_chars_per_line].rstrip()
            bullets.append(line)
            if len(bullets) >= limit:
                break
        return bullets

    sections = split_release_notes(notes)
    if not sections:
        return []
    if len(sections) == 1 and sections[0][0] is None:
        return [f'  * {b}' for b in _sanitize_section(sections[0][1], max_bullets)]
    bullets = []
    for version, text in sections:
        for b in _sanitize_section(text, 2):
            label = f'{version}: ' if version else ''
            bullets.append(f'  * {label}{b}')
            if len(bullets) >= max_bullets:
                return bullets
    return bullets


def versioned_release_versions(notes):
    """Return the ordered list of upstream versions named in release notes."""
    return [v for v, _ in split_release_notes(notes) if v]


# Matches a .changes version-header bullet in its canonical and the various
# native styles, e.g. '- Updated to version 0.34.0', '- Update to 0.34.0',
# '- Updated to v0.34.0'. The captured version must look like a version
# (digit, optionally preceded by 'v') so prose lines like '- Update to latest'
# never match.
_CHANGELOG_VERSION_RE = re.compile(
    r'^-\s*Update(?:d)?\s+(?:to\s+)?(?:version\s+)?(v?\d[\w.+-]*)', re.M)


def _clean_version(version):
    """Normalize a captured version for comparison (strip a leading 'v')."""
    return version[1:] if str(version)[:1] == 'v' else str(version)


def changelog_versions(content):
    """Return every upstream version named in the .changes content."""
    return [_clean_version(v) for v in _CHANGELOG_VERSION_RE.findall(content or '')]


def has_changelog_version(content, version):
    """Return True when the .changes content already records *version*."""
    return _clean_version(version) in changelog_versions(content)


_CHANGELOG_ENTRY_SEPARATOR_RE = re.compile(r'^-{10,}\s*$', re.M)
_SEPARATOR_LINE_RE = re.compile(r'-{10,}\s*$')


def collapse_repeated_separators(content):
    """Collapse runs of 2+ consecutive ``---`` separator lines into one.

    The AI changelog round sometimes ends its new entry with a trailing
    separator while the following older entry already begins with its own,
    producing two separator lines back to back.  This is invalid for
    openSUSE `.changes` (entries are separated by exactly one ``---`` line).
    """
    if not content:
        return content
    lines = content.splitlines(keepends=True)
    out = []
    prev_was_separator = False
    for line in lines:
        is_separator = bool(_SEPARATOR_LINE_RE.match(line))
        if is_separator and prev_was_separator:
            continue
        out.append(line)
        prev_was_separator = is_separator
    return ''.join(out)


# Matches a `## <version>` heading used to label release-note sections for a
# specific upstream version (e.g. ``## 0.33.2``).
_RELEASE_NOTES_VERSION_HEADER_RE = re.compile(r'^#{1,6}\s*(v?\d[\w.+-]*)\s*$')


def _split_first_entry(content):
    """Split ``content`` into the newest (first) entry and the rest of the file.

    Every .changes entry starts with a ``---`` separator line.  The split
    therefore happens at the *second* such line (the first separator is
    part of the newest entry's own header).
    """
    first_sep = _CHANGELOG_ENTRY_SEPARATOR_RE.search(content or '')
    if not first_sep:
        return content or '', ''
    second_sep = _CHANGELOG_ENTRY_SEPARATOR_RE.search(content, first_sep.end())
    if not second_sep:
        return content, ''
    return content[:second_sep.start()], content[second_sep.end():]


def would_duplicate_changelog_entry(content):
    """Return True when the new (first) entry names a version that already
    appears in an older entry, or repeats its own version internally.

    Historical duplicate version strings in older .changes entries do *not*
    count — ollama.changes, for example, has "0.3.6" and "0.1.48" mentioned
    more than once in legacy entries and those must never block a fresh edit.
    """
    first, rest = _split_first_entry(content)
    first_versions = changelog_versions(first)
    if len(first_versions) != len(set(first_versions)):
        return True
    if not first_versions:
        return False
    existing_versions = set(changelog_versions(rest))
    return any(v in existing_versions for v in first_versions)


def write_changelog_entry(changes_path, old_version, new_version, email_author, release_notes=None):
    """Prepend a deterministic changelog entry to a .changes file.
    When release_notes is provided, its leading content is added as sub-bullets
    under the version line.  Multi-version notes (with ``## <version>``
    headings) are rendered as ``  * <version>: ...`` sub-bullets and the
    previously-shipped intermediate versions are named on the version line.
    Returns True if the entry was written, False if the file already had a
    changelog entry for this version (skipped to avoid duplicates).
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    mon = now.strftime('%b')
    day = now.strftime('%a')
    email_match = re.search(r'<([^>]+)>', email_author)
    changelog_author = f"pbuild-ai <{email_match.group(1)}>" if email_match else f"pbuild-ai <{email_author}>"
    body = [f'- Updated to version {new_version}']
    # When the jump spans formerly-shipped intermediate releases whose release
    # notes we collected, name them so the entry records the whole jump.
    _versions = versioned_release_versions(release_notes)
    _intermediate = [
        v for v in _versions
        if v != new_version and _clean_version(v) != _clean_version(old_version)
    ]
    if _intermediate:
        if len(_intermediate) == 1:
            _covered = _intermediate[0]
        else:
            _covered = ", ".join(_intermediate[:-1]) + f" and {_intermediate[-1]}"
        body = [f'- Updated to version {new_version} (also covers intermediate '
                f'releases {_covered})']
    body.extend(sanitize_release_notes(release_notes))
    body.append('- Update generated using pbuild-ai')
    entry = (
        '-------------------------------------------------------------------\n'
        f'{day} {mon} {now.day:2d} {now.hour:02d}:{now.minute:02d}:{now.second:02d} UTC {now.year} - {changelog_author}\n'
        '\n'
        + '\n'.join(body)
        + '\n\n'
    )
    if changes_path.exists():
        content = changes_path.read_text(encoding='utf-8', errors='replace')
        # Skip if an entry for this version already exists (in any style)
        if has_changelog_version(content, new_version):
            return False
        new_content = entry + content
    else:
        new_content = entry
    changes_path.write_text(new_content)
    return True
