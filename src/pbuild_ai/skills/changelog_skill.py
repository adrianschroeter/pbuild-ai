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
- Replace `pbuild-ai <email@suse.de>` with the author you are given. Do NOT
  leave `<EMAIL>` as a literal placeholder — substitute a real address.
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
  cover those former releases in the entry as well, because the package
  previously did not ship them. Give every release its own
  `- Updated to version X` line with its highlights as `  * ` sub-bullets
  below it, newest release first (see the example above). Do NOT repeat the
  version inside the sub-bullets (no `  * 1.2.3: ...` lines) and do NOT list
  the skipped releases in a parenthesis on the first line.
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


def _sanitize_notes_text(text, limit, max_chars_per_line=120):
    """Turn one block of upstream release notes into at most *limit* plain
    lines: HTML/markdown markup, links, headings and boilerplate are
    stripped."""
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
    lines = []
    for line in text.splitlines():
        line = re.sub(r'^\s*[-*+]\s*', '', line).strip()
        if len(line) < 2:
            continue
        low = line.lower()
        if low.startswith(('http://', 'https://', '#')):
            continue
        if low.startswith(("what's changed", "what's new", "release notes", "changelog", "highlight")):
            continue
        lines.append(line[:max_chars_per_line].rstrip())
        if len(lines) >= limit:
            break
    return lines


def sanitize_release_notes(notes, max_bullets=4, max_chars_per_line=120):
    """Convert upstream release notes into a few compact .changes bullet lines.

    Deterministic transformer (no AI): strips HTML/markdown markup and turns
    the leading meaningful lines into ``  * ...`` sub-bullets suitable for a
    .changes entry body.  ``## <version>`` headings are dropped; use
    :func:`release_notes_changelog_body` to get one group per version.
    """
    bullets = []
    for _version, text in split_release_notes(notes):
        for b in _sanitize_notes_text(text, max_bullets - len(bullets), max_chars_per_line):
            bullets.append(f'  * {b}')
        if len(bullets) >= max_bullets:
            break
    return bullets


def release_notes_changelog_body(new_version, old_version, notes,
                                 existing_content='', max_bullets=4,
                                 max_bullets_per_release=2):
    """Build the ``- Updated to version X`` groups of a .changes entry body.

    The first group is always *new_version*.  When *notes* carry
    ``## <version>`` sections for releases upstream shipped between
    *old_version* and *new_version*, each of those gets its own group below,
    newest first, so the entry records the whole jump.  Releases already
    recorded in *existing_content* (an earlier, unfinished update) and
    *old_version* itself are skipped.
    """
    sections = split_release_notes(notes)
    new_clean = _clean_version(new_version)
    old_clean = _clean_version(old_version) if old_version else ''
    recorded = set(changelog_versions(existing_content))
    intermediate = []
    new_texts = []
    for version, text in sections:
        clean = _clean_version(version) if version else None
        if clean is None or clean == new_clean:
            new_texts.append(text)
        elif clean != old_clean and clean not in recorded:
            intermediate.append((version, text))
    per_release = max_bullets if not intermediate else max_bullets_per_release
    body = [f'- Updated to version {new_version}']
    body.extend(f'  * {b}' for b in
                _sanitize_notes_text('\n'.join(new_texts), per_release))
    for version, text in reversed(intermediate):
        body.append(f'- Updated to version {_clean_version(version)}')
        body.extend(f'  * {b}' for b in _sanitize_notes_text(text, per_release))
    return body


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
    headings) get one ``- Updated to version X`` group per release, newest
    first (see release_notes_changelog_body).
    Returns True if the entry was written, False if the file already had a
    changelog entry for this version (skipped to avoid duplicates).
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    mon = now.strftime('%b')
    day = now.strftime('%a')
    # 'Name <email>' is used as given; a bare address keeps the historic
    # 'pbuild-ai <email>' form.
    author_match = re.match(r'^\s*(.*?)\s*<([^<>]+)>\s*$', email_author or '')
    if author_match and author_match.group(1):
        changelog_author = f"{author_match.group(1)} <{author_match.group(2)}>"
    elif author_match:
        changelog_author = f"pbuild-ai <{author_match.group(2)}>"
    else:
        changelog_author = f"pbuild-ai <{email_author}>"
    _existing = ''
    if changes_path.exists():
        _existing = changes_path.read_text(encoding='utf-8', errors='replace')
    body = release_notes_changelog_body(new_version, old_version, release_notes,
                                        existing_content=_existing)
    body.append('- Update generated using pbuild-ai')
    entry = (
        '-------------------------------------------------------------------\n'
        f'{day} {mon} {now.day:2d} {now.hour:02d}:{now.minute:02d}:{now.second:02d} UTC {now.year} - {changelog_author}\n'
        '\n'
        + '\n'.join(body)
        + '\n\n'
    )
    if changes_path.exists():
        # Skip if an entry for this version already exists (in any style)
        if has_changelog_version(_existing, new_version):
            return False
        new_content = entry + _existing
    else:
        new_content = entry
    changes_path.write_text(new_content)
    return True
