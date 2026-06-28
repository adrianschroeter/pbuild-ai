import datetime
import re

CHANGELOG_PROMPT = """
## .changes file format (openSUSE policy)

The `.changes` file (same stem as the spec) uses this format:

-------------------------------------------------------------------
<Weekday> <Month> <Day> <Time> UTC <Year> - <Author Name> <email>

- <entry text>
- <more entries if needed>

### Rules:
- Always prepend new entries at the top. Never modify or remove older entries.
- The author header uses `pbuild-ai <EMAIL>` format.
- Entry body uses `- ` bullet lines with blank line before the next entry.
- When updating to a new version, include: `- Updated to version VERSION`
  followed by relevant upstream changelog details.
- When removing a `Patch:` line, the changelog entry MUST name the exact
  patch filename and state why it was removed, for example:
  `- Remove 0001-fix-segfault.patch (upstream applied the fix in this release)`
  This is openSUSE policy for non-git packages.
- When the .changes file does not exist, create it.
- End the entry body with `- Update generated using pbuild-ai`.
"""


def write_changelog_entry(changes_path, old_version, new_version, email_author):
    """Prepend a deterministic changelog entry to a .changes file.
    Returns True if the entry was written, False if the file already had
    a changelog entry for this version (skipped to avoid duplicates).
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    mon = now.strftime('%b')
    day = now.strftime('%a')
    email_match = re.search(r'<([^>]+)>', email_author)
    changelog_author = f"pbuild-ai <{email_match.group(1)}>" if email_match else f"pbuild-ai <{email_author}>"
    entry = (
        '-------------------------------------------------------------------\n'
        f'{day} {mon} {now.day:2d} {now.hour:02d}:{now.minute:02d}:{now.second:02d} UTC {now.year} - {changelog_author}\n'
        '\n'
        f'- Updated to version {new_version}\n'
        '- Update generated using pbuild-ai\n'
        '\n'
    )
    if changes_path.exists():
        content = changes_path.read_text(encoding='utf-8', errors='replace')
        # Skip if the entry for this version already exists
        if f'Updated to version {new_version}' in content:
            return False
        new_content = entry + content
    else:
        new_content = entry
    changes_path.write_text(new_content)
    return True
