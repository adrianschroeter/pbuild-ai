import re

# Triggered when the build log shows dependency resolution failures
PROMPT_PATTERN = r"(?i)(nothing provides|unresolvable|no provider|has no possibility|nothing provides.*devel|choice.*requires|solver.*fail|dependency.*error|director(y|ies) not owned)"

# Also trigger when spec contains known problematic patterns
CONTENT_PATTERN = r"(?i)BuildRequires:\s*(libosmesa|OpenGL|nvidia)"

OLLAMA_ERROR_PROMPT = """
You are debugging an unresolved RPM dependency error for openSUSE.

IMPORTANT: Do NOT use web_fetch to look up package names or files on external
websites yourself. The package lookup results (from gitexplorer.opensuse.org)
are already injected into this prompt automatically by the build system below.
Rely on those results — they show exactly which packages provide missing files
or match unresolved package names.

## Common resolution patterns

### 1. Case-sensitive package names
RPM package names in openSUSE are case-sensitive. Common mismatches:
  - Wrong: `BuildRequires: mesa-devel` or `BuildRequires: libOSMesa-devel`
  - Correct: `BuildRequires: Mesa-devel`
  - Wrong: `BuildRequires: libGL-devel` or `BuildRequires: opengl-devel`
  - Correct: `BuildRequires: Mesa-libGL-devel`

### 2. "choice" errors (solver has multiple providers)
When pbuild reports a "choice" error, it means the solver found multiple
packages that can satisfy a dependency but cannot decide which one to pick.
Fix by requiring a CONCRETE package name instead of the virtual/alias one.
  - Example: instead of `BuildRequires: libfoo-devel` (virtual), use
    `BuildRequires: libfoo2-devel` (concrete).

### 3. "nothing provides" errors
If the build log says "nothing provides PACKAGE", then the package truly does
not exist in the repo. Check the package lookup results below for alternative
package names or matching packages.

### 4. Conditional BuildRequires
If a BuildRequires is only needed on certain architectures or distro versions,
wrap it in a conditional:
  ```
  %if %{with foo}
  BuildRequires: foo-devel
  %endif
  ```

### 5. Unowned directories (RPM install-time)
If the build log says "directories not owned by a package" followed by
a directory path like `/etc/alternatives`, the package uses a directory
that no other RPM package owns. Find which package provides that directory
(or the command installed there) and add it to BuildRequires. For example,
`/etc/alternatives` is provided by `aaa_alternative` — add
`BuildRequires: aaa_alternative` to fix it.
"""


def parse_missing_filename_from_log(log: str) -> str | None:
    """Extract a missing filename from compiler/linker errors in the build log."""
    if not log:
        return None
    # fatal error: FILENAME: No such file or directory
    m = re.search(r"fatal error:\s*(\S+?):\s*No such file or directory", log, re.IGNORECASE)
    if m:
        return m.group(1)
    # FILENAME: No such file or directory (generic)
    m = re.search(r"(\S+):\s*No such file or directory", log)
    if m:
        return m.group(1)
    # cannot find -lNAME  (linker)
    m = re.search(r"cannot find\s+-l(\S+)", log, re.IGNORECASE)
    if m:
        return f"lib{m.group(1)}.so"
    return None


def parse_unresolved_package_from_log(log: str) -> str | None:
    """Extract the unresolvable package name from 'nothing provides' errors."""
    if not log:
        return None
    m = re.search(r"nothing provides\s+([\w][\w\-\.\+]*)", log, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def parse_unowned_directory_from_log(log: str) -> str | None:
    """Extract an unowned directory path from 'directories not owned' errors."""
    if not log:
        return None
    m = re.search(r"director(?:y|ies) not owned by a package.*?\n\s*[–-]\s+(\S+)", log, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1)
    return None
