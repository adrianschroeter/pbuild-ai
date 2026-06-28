# Triggered when the build log shows dependency resolution failures
PROMPT_PATTERN = r"(?i)(nothing provides|unresolvable|no provider|has no possibility|nothing provides.*devel|choice.*requires|solver.*fail|dependency.*error)"

# Also trigger when spec contains known problematic patterns
CONTENT_PATTERN = r"(?i)BuildRequires:\s*(libosmesa|OpenGL|nvidia)"

OLLAMA_ERROR_PROMPT = """
You are debugging an unresolved RPM dependency error for openSUSE.

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
not exist in the repo. Alternatives:
  - Search for the correct name via factory lists:
    https://download.opensuse.org/tumbleweed/repo/oss/x86_64/
    https://download.opensuse.org/tumbleweed/repo/oss/noarch/
  - Alterntive, use via the gitexplorer for a package providing a concrete file:
    https://gitexplorer.opensuse.org/api/products/files?q=FILENAME
    The filename can include an absolute path optional, but it needs to match exact.

### 4. Conditional BuildRequires
If a BuildRequires is only needed on certain architectures or distro versions,
wrap it in a conditional:
  ```
  %if %{with foo}
  BuildRequires: foo-devel
  %endif
  ```
"""
