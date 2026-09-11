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
- When removing a `Patch:` line, name the exact patch filename and state why.
- End each entry body with `- Update generated using pbuild-ai`.
- When the .changes file does not exist, create it.
- When the version jump spans former upstream releases — i.e. the current
  package source is older than some releases that upstream shipped in between —
  cover those former releases in the entry as well: name the skipped
  intermediate versions and their notable highlights, because the package
  previously did not ship them. One entry may record the whole jump, e.g.
  `- Updated to version 1.3` with sub-bullets noting the 1.1 and 1.2 highlights.
"""


def sanitize_release_notes(notes, max_bullets=4, max_chars_per_line=120):
    """Convert upstream release notes into a few compact .changes bullet lines.

    Deterministic transformer (no AI): strips HTML/markdown markup and turns
    the leading meaningful lines into '  * ...' sub-bullets suitable for a
    .changes entry body. Never returns more than max_bullets bullets.
    """
    if not notes:
        return []
    text = re.sub(r'<!--.*?-->', '', notes, flags=re.S)
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
        bullets.append(f'  * {line}')
        if len(bullets) >= max_bullets:
            break
    return bullets


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
    under the version line. Returns True if the entry was written, False if the
    file already had a changelog entry for this version (skipped to avoid
    duplicates).
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    mon = now.strftime('%b')
    day = now.strftime('%a')
    email_match = re.search(r'<([^>]+)>', email_author)
    changelog_author = f"pbuild-ai <{email_match.group(1)}>" if email_match else f"pbuild-ai <{email_author}>"
    body = [f'- Updated to version {new_version}']
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
