import re
from pathlib import Path

# Central skill for build-dependency problems: it parses the failure out of
# the build log, applies deterministic fixes for known mistake patterns
# (file-path BuildRequires) and guides the AI with lookup results from
# gitexplorer.opensuse.org.

# Triggered when the build log shows dependency resolution failures
PROMPT_PATTERN = r"(?i)(nothing provides|unresolvable|no provider|has no possibility|nothing provides.*devel|choice.*requires|solver.*fail|dependency.*error|director(y|ies) not owned)"

# Also trigger when spec contains known problematic patterns — a BuildRequires
# whose value is a file path ("BuildRequires: /usr/bin/protoc") is one
CONTENT_PATTERN = r"(?i)BuildRequires:\s*((libosmesa|OpenGL|nvidia)|/)"

AI_ERROR_PROMPT = """
You are debugging an unresolved RPM dependency error for openSUSE.

IMPORTANT: Do NOT use web_fetch to look up package names or files on external
websites yourself. The package lookup results (from gitexplorer.opensuse.org)
are already injected into this prompt automatically by the build system below,
and you can run further lookups yourself with the search_package tool
(search_by='name' for a package name, search_by='file' for a file path).
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

In project mode, prefer to fix a missing dependency by adding a further
package to the project that provides the missing dependency, rather than
dropping the BuildRequires line.

### 4. File-path dependencies (BuildRequires: /usr/bin/something)
A dependency written as a file path — e.g. `BuildRequires: /usr/bin/protoc` —
is wrong: file requires only resolve if a binary package in the build repos
happens to own that exact path, so they are fragile and often unresolvable.
NEVER add or keep a file path as a dependency name. Look up which package
provides the file (search_package with search_by='file', or the "Packages
providing" results injected below) and require that PACKAGE name instead:
  - Wrong: `BuildRequires: /usr/bin/protoc`
  - Correct: `BuildRequires: protobuf21-devel` (whatever the lookup returned)

### 5. Conditional BuildRequires
If a BuildRequires is only needed on certain architectures or distro versions,
wrap it in a conditional:
  ```
  %if %{with foo}
  BuildRequires: foo-devel
  %endif
  ```

### 6. Unowned directories (RPM install-time)
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
    """Extract the unresolvable package name from 'nothing provides' errors.

    Also matches file-path dependencies (e.g. 'nothing provides
    /usr/bin/protoc') — check for a leading '/' and look those up with the
    gitexplorer files endpoint instead of the packages endpoint.
    """
    if not log:
        return None
    m = re.search(r"nothing provides\s+([/\w][\w\-\.\+/]*)", log, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def parse_unresolved_package_with_constraint(log: str) -> tuple[str, str | None, str | None] | None:
    """Extract package name, operator, and version from 'nothing provides' errors.

    Returns (pkg_name, operator, version) or (pkg_name, None, None) if no constraint, or None.
    Operator is one of: >=, <=, =, >, <
    The name may also be a file path starting with '/'.
    """
    if not log:
        return None
    m = re.search(
        r"nothing provides\s+([/\w][\w\-\.\+/]*)(?:\s+([><=]+)\s*([\w\.\*\+~]+))?",
        log, re.IGNORECASE
    )
    if m:
        return (m.group(1), m.group(2), m.group(3))
    return None


def parse_unowned_directory_from_log(log: str) -> str | None:
    """Extract an unowned directory path from 'directories not owned' errors."""
    if not log:
        return None
    m = re.search(r"director(?:y|ies) not owned by a package.*?\n\s*[–-]\s+(\S+)", log, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1)
    return None


# --- File-path BuildRequires: detection, AI hint and deterministic fix ---

_FILEPATH_BUILDREQUIRES_RE = re.compile(r'(?im)^\s*BuildRequires\s*:\s*(/[^\s%]+)')


def parse_filepath_buildrequires(spec_text: str) -> list[str]:
    """File paths used as BuildRequires in a spec, in order of appearance,
    without duplicates. Commented lines are not matched."""
    paths = []
    for m in _FILEPATH_BUILDREQUIRES_RE.finditer(spec_text or ""):
        p = m.group(1)
        if p not in paths:
            paths.append(p)
    return paths


def spec_write_hint(file_path, new_text: str) -> str:
    """Hint appended to spec writes that BuildRequire file paths instead of
    package names.

    openSUSE practice is to depend on package names: a file path like
    '/usr/bin/protoc' only resolves if some binary package in the build repos
    owns that exact path, so it is fragile and often unresolvable. Looks the
    providers up via the gitexplorer API so the AI can fix the lines right
    away; on lookup failure it at least points at the search_package tool.
    """
    if not str(file_path).endswith('.spec'):
        return ""
    paths = parse_filepath_buildrequires(new_text)
    if not paths:
        return ""
    msg = ("\n[pbuild-ai] File-path BuildRequires in {name}: {paths} — BuildRequires "
           "must be PACKAGE NAMES, never file paths. Look up the providing package "
           "with search_package(search_by='file', query='<path>') and replace the "
           "path via edit_file.").format(name=Path(file_path).name, paths=", ".join(paths))
    try:
        from pbuild_ai.query_gitexplorer import query_package_by_file, format_results
        for p in paths[:3]:
            found = query_package_by_file(p, limit=10)
            if found:
                msg += f"\nPackages providing '{p}' (require one of these PACKAGE names):\n" + format_results(found)
            else:
                msg += f"\nNo package provides '{p}' in any openSUSE/SLE repo — replace or drop that BuildRequires."
    except Exception:
        pass
    return msg


def fix_build_error(spec_path, spec_text, build_log, log=print):
    """Before the AI looks at the failure: replace file-path BuildRequires
    ('BuildRequires: /usr/bin/protoc') with the providing package's NAME when
    the gitexplorer files endpoint returns exactly one candidate name.

    Ambiguous providers, unknown paths and failed lookups are left to the AI,
    which receives the candidates in the injected lookup results. Only paths
    the build log actually complains about are touched. Returns the new spec
    text, or None when nothing was changed.
    """
    paths = parse_filepath_buildrequires(spec_text)
    if not paths:
        return None
    from pbuild_ai.query_gitexplorer import query_package_by_file
    new_text = spec_text
    changed = False
    for p in paths:
        if p not in (build_log or ""):
            continue
        try:
            found = query_package_by_file(p, limit=10)
        except Exception:
            continue
        names = sorted({r["package_name"] for r in found})
        if len(names) != 1:
            continue
        pkg = names[0]
        replaced = re.sub(rf'(?im)^(\s*BuildRequires\s*:\s*){re.escape(p)}(?=\s*(?:#.*)?$)',
                          rf'\g<1>{pkg}', new_text)
        if replaced != new_text:
            log(f"[SKILL] unresolvable: BuildRequires '{p}' -> '{pkg}' "
                f"(sole provider via gitexplorer)")
            new_text = replaced
            changed = True
    return new_text if changed else None
