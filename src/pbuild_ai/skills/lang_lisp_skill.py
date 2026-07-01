# lang_lisp_skill.py
### WARNING: Currently not reviewed and adapted to git based packaging!
### Based on https://en.opensuse.org/openSUSE:Packaging_Lisp

SKILL_NAME = "lang_lisp"

TARGET_PATTERN = r"(?i)^cl-.*\.spec$|.*lisp.*\.spec$"
CONTENT_PATTERN = r"(?i)(?:common-lisp-controller|\.asd\b|common-lisp/source|register-common-lisp|fasl)"
PROMPT_PATTERN = r"(?i)(?:lisp\s+packag|common\s+lisp\s+rpm|asdf\s+system|cl-\w+)"


OLLAMA_SPEC_PROMPT = """
You are an expert in Common Lisp RPM packaging for openSUSE. Follow the openSUSE Lisp packaging guidelines from https://en.opensuse.org/openSUSE:Packaging_Lisp

## Naming convention

Lisp libraries use the `cl-` prefix:
- `cl-ppcre` for Common Lisp Portable Perl-Compatible Regular Expressions
- `cl-alexandria` for Alexandria utilities
- `cl-flexi-streams` for Flexi-Streams

## BuildRequires

```
BuildRequires:  common-lisp-controller
BuildRequires:  cl-asdf
```

## Spec template

```
Name:           cl-ppcre
Version:        2.0.3
Release:        0
Summary:        Portable Perl-Compatible Regular Expressions for Common Lisp
License:        BSD-2-Clause
Group:          Development/Libraries/Other
BuildArch:      noarch
BuildRequires:  common-lisp-controller
BuildRequires:  cl-asdf
Source0:        https://github.com/ediethelm/cl-ppcre/archive/v%{version}.tar.gz

%description
CL-PPCRE is a portable regular expression library for Common Lisp.

%prep
%autosetup -p1 -n cl-ppcre-%{version}

%build
# Lisp libraries are typically source-only; no compilation needed

%install
mkdir -p %{buildroot}%{_datadir}/common-lisp/source/%{name}/
cp -r * %{buildroot}%{_datadir}/common-lisp/source/%{name}/
mkdir -p %{buildroot}%{_datadir}/common-lisp/systems/
if [ -f %{buildroot}%{_datadir}/common-lisp/source/%{name}/%{name}.asd ]; then
  ln -sf ../source/%{name}/%{name}.asd %{buildroot}%{_datadir}/common-lisp/systems/%{name}.asd
fi

%post
register-common-lisp-implementation clisp

%preun
unregister-common-lisp-implementation clisp

%files
%license LICENSE
%doc README
%dir %{_datadir}/common-lisp/source/%{name}/
%{_datadir}/common-lisp/source/%{name}/
%{_datadir}/common-lisp/systems/%{name}.asd
```

## Key paths

| Path | Purpose |
|---|---|
| `%{_datadir}/common-lisp/source/NAME/` | Library source code |
| `%{_datadir}/common-lisp/systems/` | ASDF system symlinks (.asd files) |

## Key rules

- No -devel sub-package is needed (source files are installed by default)
- Use `.asd` (ASDF system definition) files to define the system
- Create a symlink from `.asd` file to `%{_datadir}/common-lisp/systems/`
- `%post` scriptlet: call `register-common-lisp-implementation` for each Lisp implementation
- `%preun` scriptlet: call `unregister-common-lisp-implementation` for each Lisp implementation
- `.fasl` files are implementation-specific binary compiled files — do not ship them in the package
- Set `BuildArch: noarch` (Lisp source is architecture-independent)
- Use the `cl-` prefix for all Lisp library packages
"""

OLLAMA_ERROR_PROMPT = """
You are debugging a Common Lisp RPM build failure for openSUSE.

## Common Lisp build errors

### Missing common-lisp-controller
```
/usr/sbin/register-common-lisp-implementation: No such file or directory
```
→ Add `BuildRequires: common-lisp-controller`.

### .asd file not found
```
System definition file NAME.asd not found
```
→ The `.asd` file is missing or not symlinked correctly.
  Verify the `.asd` file exists in the source and is installed to:
  `%{_datadir}/common-lisp/source/NAME/`
  Then ensure the symlink exists in `%{_datadir}/common-lisp/systems/`.

### fasl version mismatch
```
fasl file version mismatch: file is #x... expected #x...
```
→ `.fasl` files are implementation and version-specific binary files.
  Delete any bundled `.fasl` files in %prep:
  `find . -name '*.fasl' -delete`
  Do NOT ship `.fasl` files in packages — they are compiled at install time.

### Installed (but unpackaged) file(s) found
```
error: Installed (but unpackaged) file(s) found:
   /usr/share/common-lisp/source/foo/foo.asd
```
→ Add the source directory and system symlink to `%files`:
  ```
  %dir %{_datadir}/common-lisp/source/%{name}/
  %{_datadir}/common-lisp/source/%{name}/
  %{_datadir}/common-lisp/systems/%{name}.asd
  ```

### No %post scriptlet for Lisp implementation registration
If the Lisp implementation cannot find the newly installed library:
→ Add %post and %preun scriptlets with `register-common-lisp-implementation` and
  `unregister-common-lisp-implementation` for each supported Lisp implementation.

## Investigating interactively

If you are unsure about the root cause and need to investigate inside the build environment, include [DEEP_ANALYZE] in your response.

When inside the build environment:
- Check source installation: `ls -la %{buildroot}%{_datadir}/common-lisp/source/`
- Check system symlinks: `ls -la %{buildroot}%{_datadir}/common-lisp/systems/`
- Verify .asd files: `find . -name '*.asd' 2>/dev/null`
- Check for bundled .fasl: `find . -name '*.fasl' 2>/dev/null`
"""

DEEP_ANALYZE_PROMPT = """
## Interactive Lisp investigation

You are inside the build environment. Investigate Lisp packaging issues:

1. **Check source installation**: `ls -la %{buildroot}%{_datadir}/common-lisp/source/ 2>/dev/null`
2. **Check system symlinks**: `ls -la %{buildroot}%{_datadir}/common-lisp/systems/ 2>/dev/null`
3. **Find .asd files**: `find %{buildroot} -name '*.asd' 2>/dev/null`
4. **Check for bundled .fasl**: `find . -name '*.fasl' 2>/dev/null`
5. **Verify ASDF registration**: `cat %{buildroot}%{_datadir}/common-lisp/systems/*.asd 2>/dev/null | head -10`
6. **Check for other Lisp implementations**: `ls /usr/bin/*cl* 2>/dev/null || ls /usr/bin/sbcl 2>/dev/null`
"""


def fix_content(content: str) -> str:
    return content
