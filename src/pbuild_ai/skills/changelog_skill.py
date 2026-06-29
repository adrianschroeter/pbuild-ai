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
