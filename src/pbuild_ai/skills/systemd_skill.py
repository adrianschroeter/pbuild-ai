# systemd_skill.py

SKILL_NAME = "systemd"

TARGET_PATTERN = r"(?i)(?:^|(?<=-))(?:systemd|service|tmpfiles|sysusers)"  # filename substrings
CONTENT_PATTERN = r"(?i)(?:%\{_unitdir\}|%\{_userunitdir\}|%service_add_|%tmpfiles_create|%sysusers_create|BuildRequires:\s*systemd-rpm-macros|systemctl)"
PROMPT_PATTERN = r"(?i)(?:systemd\s+(?:service|unit|packag)|service\s+file|unit\s+file|tmpfiles?\.d|sysusers?\.d)"

OLLAMA_SPEC_PROMPT = """
You are an expert in systemd RPM packaging for openSUSE. Follow the openSUSE systemd packaging guidelines.

## Build Requirements

- To link against systemd **libraries**, use: `BuildRequires: pkgconfig(libsystemd)`
  This picks `systemd-mini-devel` as needed.
- To use systemd **programs** during build, use: `BuildRequires: pkgconfig(systemd)`
  This picks `systemd-mini` as needed.
- `systemd-rpm-macros` is pulled in by `rpm-build` in Leap/Tumbleweed, but it is fine to be explicit:
  `BuildRequires: systemd-rpm-macros`

## Runtime Dependencies

Do NOT use `Requires(post): systemd` or `%systemd_requires`. The `%service_add_*` macros already handle
the absence of systemd gracefully. Only add explicit `Requires:` for systemd if the shipped programs
invoke systemd commands directly and cannot function otherwise. In that case, use:

    Requires: systemd

### Ordering-only dependency (recommended alternative)

If the package only needs systemd for scriptlets (start/stop/enable during install/update),
use the ordering-only macro instead of a hard Requires:

    %systemd_ordering

This ensures systemd is installed before the package without forcing it on users who
opted out of systemd (e.g., with systemd-mini).

## Unit Files

### Installation paths

- System unit files: `%{_unitdir}` → `/usr/lib/systemd/system`
- User unit files: `%{_userunitdir}` → `/usr/lib/systemd/user`
- NEVER install into `/etc/systemd/system` — that is the admin override directory.

Example install lines:

    install -D -m 644 %{SOURCE3} %{buildroot}%{_unitdir}/foo.service
    install -D -m 644 %{SOURCE4} %{buildroot}%{_userunitdir}/foo.service

### Do NOT mark as %config

Unit files should NOT be listed as `%config` in `%files`. Admins override them by placing
replacement files or `.d/` drop-in fragments in `/etc/systemd/system`.

### Preset files

If the service should be enabled by default on new installs, ship a systemd preset file:

    install -D -m 644 %{SOURCE5} %{buildroot}%{_presetdir}/90-foo.preset

with content:

    enable foo.service

### Service file validation

Run `systemd-analyze verify` on unit files during `%check` if the build environment has systemd:

    systemd-analyze verify %{buildroot}%{_unitdir}/foo.service

## Scriptlets

Use the macros from `systemd-rpm-macros` for all scriptlet operations:

### Install/update scripts

    %prep
    ...

    %install
    ...
    %service_add_pre foo.service

    %post
    %service_add_post foo.service

    %preun
    %service_del_preun foo.service

    %postun
    %service_del_postun foo.service

For user services, the same macros work correctly when the unit is in `%{_userunitdir}`.
There are no separate `_user` variants needed — the macros detect the path automatically.

### Restart-on-update pattern

To only restart the service on updates (not on fresh install):

    %post
    %service_add_post foo.service

    %postun
    if [ $1 -ge 1 ]; then
      %service_del_postun foo.service
    fi

### Stop-on-removal

The `%preun` scriptlet with `%service_del_preun` handles stopping the service on removal.
No extra `%stop_on_removal` macro is needed for standard services.

## tmpfiles.d

If the package creates runtime files or directories (e.g., `/run/foo`, `/var/lib/foo`),
ship a tmpfiles.d snippet:

- Install to `%{_tmpfilesdir}` (`/usr/lib/tmpfiles.d`)
- Example install: `install -D -m 644 %{SOURCE6} %{buildroot}%{_tmpfilesdir}/foo.conf`
- In `%post` scriptlet, run:

    %tmpfiles_create foo.conf

- In `%preun` scriptlet, run:

    %tmpfiles_create foo.conf

- Mark the file in `%files` (not as `%config`):

    %{_tmpfilesdir}/foo.conf

## sysusers.d

If the package needs dedicated system users/groups, ship a sysusers.d snippet:

- Install to `%{_sysusersdir}` (`/usr/lib/sysusers.d`)
- In `%post` scriptlet, run:

    %sysusers_create foo.conf

## Summary of paths and macros

| Purpose | Path macro | Default path |
|---|---|---|
| System unit files | `%{_unitdir}` | `/usr/lib/systemd/system` |
| User unit files | `%{_userunitdir}` | `/usr/lib/systemd/user` |
| Preset files | `%{_presetdir}` | `/usr/lib/systemd/system-preset` |
| tmpfiles.d snippets | `%{_tmpfilesdir}` | `/usr/lib/tmpfiles.d` |
| sysusers.d snippets | `%{_sysusersdir}` | `/usr/lib/sysusers.d` |

| Macro | Purpose |
|---|---|
| `%service_add_pre foo.service` | `%pre` — add service before install |
| `%service_add_post foo.service` | `%post` — enable/start after install |
| `%service_del_preun foo.service` | `%preun` — stop/disable before removal |
| `%service_del_postun foo.service` | `%postun` — reload daemon after removal |
| `%tmpfiles_create foo.conf` | `%post` — create tmpfiles entries |
| `%tmpfiles_create foo.conf` | `%preun` — recreate tmpfiles on upgrade |
| `%sysusers_create foo.conf` | `%post` — create system users/groups |
| `%systemd_ordering` | Ordering-only dep on systemd (no hard Requires) |
| `%fillup_only` | Legacy sysv compat — rarely needed |
"""

OLLAMA_ERROR_PROMPT = """
You are debugging a systemd service RPM build failure for openSUSE.

## Common build errors

### Missing pkgconfig(libsystemd)
```
fatal error: systemd/sd-daemon.h: No such file or directory
```
→ Add `BuildRequires: pkgconfig(libsystemd)`

### Missing systemd tools
```
/usr/bin/systemctl: not found  (during build)
```
→ Add `BuildRequires: pkgconfig(systemd)` (gives systemd-mini in build root)

### Unpackaged unit file
```
error: Installed (but unpackaged) file(s) found:
   /usr/lib/systemd/system/foo.service
```
→ Add to `%files`: `%{_unitdir}/foo.service`

### Bad exit status in %post/%preun scriptlet
```
error: Bad exit status from /var/tmp/rpm-tmp.XXXXX (%post)
```
→ Check that the service name matches the actual unit filename.
  Verify `%service_add_post foo.service` uses the correct unit name (with `.service` suffix).
  If the service exists in a subpackage, make sure the scriptlets are in the correct subpackage.

### tmpfiles/sysusers errors
```
error: /usr/lib/tmpfiles.d/foo.conf is not owned by any package
```
→ Add `%{_tmpfilesdir}/foo.conf` to `%files`.
  Ensure `%tmpfiles_create foo.conf` or `%sysusers_create foo.conf` is in `%post`.

## Investigating interactively

If you are unsure about the root cause and need to investigate inside the build environment,
include [DEEP_ANALYZE] in your response.

When inside the build environment with --deep-analyze:
- Check unit file syntax: `systemd-analyze verify /usr/lib/systemd/system/foo.service`
- Check unit file contents: `cat /usr/lib/systemd/system/foo.service`
- List all installed unit files: `find /usr/lib/systemd/system -name '*.service' -o -name '*.socket'`
- Check preset files: `ls /usr/lib/systemd/system-preset/`
- Run `journalctl` to see service errors (if systemd is running)
"""

DEEP_ANALYZE_PROMPT = """
## Interactive systemd investigation

You are inside the build environment. Investigate systemd service issues:

1. **Check unit file syntax**: `systemd-analyze verify PATH/TO/SERVICE.service`
2. **View unit file**: `cat PATH/TO/SERVICE.service`
3. **Check for missing files**: `ls -la /usr/lib/systemd/system/`
4. **Check preset configuration**: `cat /usr/lib/systemd/system-preset/*.preset`
5. **Check tmpfiles**: `ls -la /usr/lib/tmpfiles.d/`
6. **Check for build root ownership issues**: `find /usr/lib/systemd -not -user root -not -name '.*'`
7. **If the service fails to start**: `systemctl status SERVICE.service 2>/dev/null || echo "systemd not available in build root"`
"""


def fix_content(content: str) -> str:
    """Pre-build spec content fixes for common systemd packaging issues."""
    lines = content.split('\n')
    changed = False

    # Check if unit files are installed but not listed in %files
    has_unitdir_install = False
    has_unitdir_files = False
    has_service_scripts = False

    for line in lines:
        if '%{_unitdir}' in line and any(cmd in line for cmd in ('install', 'cp ', 'mv ')):
            has_unitdir_install = True
        if '%{_unitdir}' in line and line.strip().startswith('%{_unitdir}'):
            has_unitdir_files = True
        if '%service_add_' in line:
            has_service_scripts = True

    # If unit files are installed but not packaged, add to %files
    if has_unitdir_install and not has_unitdir_files:
        new_lines = []
        in_files = False
        files_indent = ''
        for i, line in enumerate(lines):
            new_lines.append(line)
            stripped = line.strip()
            if stripped == '%files' and not stripped.startswith('%files '):
                in_files = True
                files_indent = line[:len(line) - len(line.lstrip())]
            elif in_files and (stripped.startswith('%') and not stripped.startswith('%{')) or \
                 i == len(lines) - 1:
                if i == len(lines) - 1:
                    new_lines.append(line)
                if not any('%{_unitdir}' in l for l in lines[i-3:i+3]):
                    new_lines.append(f'{files_indent}%{{_unitdir}}/*.service')
                    new_lines.append(f'{files_indent}%{{_unitdir}}/*.socket')
                    new_lines.append(f'{files_indent}%{{_unitdir}}/*.timer')
                    new_lines.append(f'{files_indent}%{{_unitdir}}/*.path')
                    changed = True
                if i < len(lines) - 1:
                    in_files = False
        if changed:
            content = '\n'.join(new_lines)

    return content
