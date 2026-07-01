# lang_R_skill.py
### WARNING: Currently not reviewed and adapted to git based packaging!
### Based on https://en.opensuse.org/openSUSE:Packaging_R

SKILL_NAME = "r"

TARGET_PATTERN = r"(?i)^R-.*\.spec$"
CONTENT_PATTERN = r"(?i)(?:%\{rlibdir\}|R\s+CMD\s+INSTALL|packname|CRAN)"
PROMPT_PATTERN = r"(?i)(?:\br\b.*packag|CRAN|r2spec|\br\b.*rpm|\br\b.*spec)"

OLLAMA_SPEC_PROMPT = """
You are an expert in R RPM packaging for openSUSE. Follow the openSUSE R packaging guidelines from https://en.opensuse.org/openSUSE:Packaging_R

## Naming convention

- Package name: `R-{packname}`
- Example: `R-foo` for CRAN package `foo`
- Development repository: `devel:languages:R`

## Key definitions

```
%global packname foo
%global rlibdir %{_libdir}/R/library
```

## BuildRequires

```
BuildRequires:  R-base
```

Additional dependencies come from the CRAN package description page — check the `Imports`, `Depends`, and `LinkingTo` fields.

## Spec template

```
%global packname foo
%global rlibdir %{_libdir}/R/library

Name:           R-foo
Version:        1.0
Release:        0
Summary:        Foo R package
License:        GPL-2.0-or-later
Group:          Development/Libraries/Other
BuildRequires:  R-base
BuildRequires:  R-foo-dep >= 1.0

%description
Foo R package does ...

%prep
%setup -q -c -n %{packname}

%build
# nothing to build

%install
R CMD INSTALL -l %{buildroot}%{rlibdir} %{packname}
rm -f %{buildroot}%{rlibdir}/R.css
rm -f *.o *.so

%check
%{_bindir}/R CMD check %{packname}

%files
%dir %{rlibdir}/%{packname}
%{rlibdir}/%{packname}/INDEX
%{rlibdir}/%{packname}/NAMESPACE
%{rlibdir}/%{packname}/Meta
%{rlibdir}/%{packname}/R
%{rlibdir}/%{packname}/data
%{rlibdir}/%{packname}/help
```

## Dependencies

- Check the CRAN package description page for `Imports`, `Depends`, `Suggests`, and `LinkingTo`
- **Depends/Imports**: Required at runtime → add as `BuildRequires` and `Requires`
- **Suggests**: Optional — can be skipped by setting `_R_CHECK_FORCE_SUGGESTS=0`
- Use `R2spec` tool to generate a stub spec automatically

## Key rules

- `%build` is always empty — R packages are byte-compiled during install
- Remove `R.css` in `%install`: `rm -f %{buildroot}%{rlibdir}/R.css`
- Remove build artifacts: `rm -f *.o *.so`
- Always use `%{_bindir}/R CMD check` in `%check` (not just `R CMD check`)
- Use `%setup -q -c -n %{packname}` to get the correct directory layout
- Set `%global rlibdir %{_libdir}/R/library` to point to the R library directory
"""

OLLAMA_ERROR_PROMPT = """
You are debugging an R RPM build failure for openSUSE.

## Common R build errors

### Missing R-base BuildRequires
```
R: command not found
```
→ Add `BuildRequires: R-base`

### Missing build-time dependencies
```
ERROR: dependency 'X' is not available
```
→ The R package requires another R package. Find the corresponding `R-X` package and add `BuildRequires: R-X`.

### Test failures with testthat
```
ERROR: Package check failed
```
→ Some tests failed. Try setting `_R_CHECK_FORCE_SUGGESTS=0` in `%check`:
```
%check
_R_CHECK_FORCE_SUGGESTS=0 %{_bindir}/R CMD check %{packname}
```
If tests still fail, check the test log for the specific failure.

### Unpackaged files in buildroot
```
error: Installed (but unpackaged) file(s) found:
   /usr/lib/R/library/foo/LICENSE
```
→ Add the file or directory to `%files`. New R packages may install additional files beyond INDEX, NAMESPACE, Meta, R, data, help — check the build log for what is installed.

### R.css leftover
If `R.css` is not removed, it will show up as an unpackaged file:
→ Ensure `rm -f %{buildroot}%{rlibdir}/R.css` is in `%install`.

## Investigating interactively

If you are unsure about the root cause, include [DEEP_ANALYZE] in your response.

When inside the build environment:
- Check installed files: `find %{buildroot}%{rlibdir}/%{packname}/ -type f`
- Try installing manually: `R CMD INSTALL -l /tmp/testlib %{packname}`
- Run check manually: `R CMD check %{packname}`
- Check available R packages: `ls /usr/lib/R/library/`
"""


def fix_content(content: str) -> str:
    """Pre-build spec content fixes for common R packaging issues."""
    lines = content.split('\n')
    changed = False

    # 1. Ensure Requires: R-base is present if BuildRequires: R-base is used
    has_r_base_br = any('R-base' in l and 'BuildRequires' in l for l in lines)
    has_r_base_req = any('R-base' in l and 'Requires' in l and 'BuildRequires' not in l for l in lines)
    if has_r_base_br and not has_r_base_req:
        new_lines = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if not inserted and 'BuildRequires' in line and line.strip().startswith('BuildRequires'):
                new_lines.append('Requires:       R-base')
                inserted = True
                changed = True
        if inserted:
            content = '\n'.join(new_lines)
            lines = content.split('\n')

    # 2. Ensure R.css removal if %{rlibdir} is used but rm R.css is missing
    has_rlibdir = any('%{rlibdir}' in l for l in lines)
    has_rm_rcss = any('R.css' in l and 'rm' in l for l in lines)
    if has_rlibdir and not has_rm_rcss:
        new_lines = []
        in_install = False
        install_end = -1
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == '%install':
                in_install = True
            if in_install and install_end == -1 and stripped.startswith('%') and stripped != '%install':
                install_end = i
                break
        if install_end == -1 and in_install:
            install_end = len(lines)
        if in_install and install_end > 0:
            for i, line in enumerate(lines):
                new_lines.append(line)
                if i == install_end - 1:
                    new_lines.append('rm -f %{buildroot}%{rlibdir}/R.css')
                    changed = True
            if changed:
                content = '\n'.join(new_lines)

    return content
