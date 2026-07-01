# lang_pyqt_sip_skill.py
### WARNING: Currently not reviewed and adapted to git based packaging!
### Based on https://en.opensuse.org/openSUSE:Packaging_PyQt_and_SIP

SKILL_NAME = "lang_pyqt_sip"

TARGET_PATTERN = r"(?i)(?:python.*qt|python.*sip|pyqt|sip).*\.spec$"
CONTENT_PATTERN = r"(?i)(?:sip-devel|pyqt-builder|%\{pyqt.*_build\}|%\{pyqt.*_install\}|%use_sip4|%sip4_only|%sip5_only|%pyqt_build_for_qt6|python-sip|python-qt5-sip|python-PyQt6-sip)"
PROMPT_PATTERN = r"(?i)(?:pyqt\s+packag|sip\s+packag|pyqt5|pyqt6|qt-bindings|sip\s+bind)"

OLLAMA_SPEC_PROMPT = """
You are an expert in PyQt and SIP RPM packaging for openSUSE. Follow the openSUSE PyQt/SIP packaging guidelines from https://en.opensuse.org/openSUSE:Packaging_PyQt_and_SIP

## SIP versions

### SIP v5/v6 (current)

SIP v5/v6 uses the `sip-devel` package for building:

```
BuildRequires:  %{python_module sip-devel}
```

### SIP v4 (deprecated)

SIP v4 uses the `sip4-devel` package:

```
BuildRequires:  %{python_module sip4-devel}
```

## PyQt versions

### PyQt5

```
BuildRequires:  %{python_module pyqt-builder}

%build
%pyqt_build

%install
%pyqt_install

%files
%{python_sitearch}/PyQt5/
```

### PyQt6

```
BuildRequires:  %{python_module pyqt-builder}

%build
%pyqt_build

%install
%pyqt_install

%files
%{python_sitearch}/PyQt6/
```

## Runtime dependencies

### SIP v4 runtime

```
Requires:       python-sip(api) = %{python_sip_api_ver}
```

### PyQt5 runtime

```
Requires:       python-qt5-sip
```

### PyQt6 runtime

```
Requires:       python-PyQt6-sip
```

## Spec template (PyQt5 example)

```
%define pkgname PyQt5
%define version 5.15.9

Name:           python-%{pkgname}
Version:        %{version}
Release:        0
Summary:        Python bindings for Qt5
License:        GPL-3.0-only
Group:          Development/Libraries/Python
URL:            https://www.riverbankcomputing.com/software/pyqt/
Source0:        https://files.pythonhosted.org/packages/source/P/%{pkgname}/%{pkgname}-%{version}.tar.gz

BuildRequires:  %{python_module pyqt-builder}
BuildRequires:  %{python_module sip-devel}
BuildRequires:  python-pyqt-rpm-macros
BuildRequires:  pkgconfig(Qt5Core)
BuildRequires:  pkgconfig(Qt5Gui)
BuildRequires:  pkgconfig(Qt5Widgets)

Requires:       python-qt5-sip

%description
PyQt5 is a set of Python bindings for Qt5.

%prep
%autosetup -p1 -n %{pkgname}-%{version}

%build
%pyqt_build

%install
%pyqt_install

%check
%pyqt_install_examples %{buildroot}%{_examplesdir}/PyQt5

%files
%license LICENSE
%doc NEWS.rst README
%{python_sitearch}/PyQt5/
%{_examplesdir}/PyQt5/
```

## Spec template (SIP bindings)

```
%define pkgname PyQt5

Name:           python-%{pkgname}
Version:        5.15.9
Release:        0
Summary:        SIP bindings for PyQt5
License:        GPL-3.0-only
Group:          Development/Libraries/Python
URL:            https://www.riverbankcomputing.com/software/pyqt/
Source0:        https://files.pythonhosted.org/packages/source/P/%{pkgname}/%{pkgname}-%{version}.tar.gz

BuildRequires:  %{python_module sip-devel}
BuildRequires:  python-pyqt-rpm-macros
BuildRequires:  pkgconfig(Qt5Core)

%description
SIP bindings for PyQt5.

%prep
%autosetup -p1 -n %{pkgname}-%{version}

%build
%pyqt_build

%install
%pyqt_install

%check
%pyqt_install_examples %{buildroot}%{_examplesdir}

%files
%license LICENSE
%doc README
%{python_sitearch}/PyQt5/
%{python_sitearch}/PyQt5/bindings/
```

## Key macros

| Macro | Purpose |
|---|---|
| `%use_sip4` | Use SIP v4 instead of SIP v5/v6 |
| `%pyqt_build_for_qt6` | Build for Qt6 instead of Qt5 |
| `%sip4_only` | Restrict to SIP v4 only |
| `%sip5_only` | Restrict to SIP v5/v6 only |
| `%pyqt<N>_sipdir` | Location of `.sip` binding files for PyQt<N> |
| `%pyqt_build()` | Build PyQt bindings |
| `%pyqt_install()` | Install PyQt bindings |
| `%pyqt_install_examples` | Install PyQt examples |

## BuildRequires patterns

Always include:
- `BuildRequires: %{python_module pyqt-builder}` for PyQt bindings
- `BuildRequires: %{python_module sip-devel}` (SIP v5/v6) or `%{python_module sip4-devel}` (SIP v4)
- `BuildRequires: python-pyqt-rpm-macros`
- `BuildRequires: pkgconfig(Qt<N>Core)` for the target Qt version

## Key rules

- Use `%{python_module ...}` for all Python BuildRequires to support multiple Python flavors
- `Requires: python-qt5-sip` for PyQt5, `Requires: python-PyQt6-sip` for PyQt6
- For SIP v4 bindings: `Requires: python-sip(api) = %{python_sip_api_ver}`
- Use `%pyqt_build_for_qt6` when building bindings for Qt6
- Always include `%check` section with `%pyqt_install_examples` when examples exist
"""

OLLAMA_ERROR_PROMPT = """
You are debugging a PyQt/SIP RPM build failure for openSUSE.

## Common build errors

### Missing sip-devel
```
fatal error: sip.h: No such file or directory
```
→ Add `BuildRequires: %%{python_module sip-devel}` for SIP v5/v6.
  For SIP v4, use `%%{python_module sip4-devel}`.

### SIP version mismatch
```
This binding requires SIP v5 or later, but SIP v4 is installed
```
→ The binding was designed for a different SIP version.
  For SIP v5/v6 bindings: use `%sip5_only` and `BuildRequires: %%{python_module sip-devel}`.
  For SIP v4 bindings: use `%sip4_only` and `BuildRequires: %%{python_module sip4-devel}`.

### Qt API version mismatch
```
Could not find QtCore module in PyQt5
```
→ The binding was built for a different Qt version.
  Ensure `BuildRequires: pkgconfig(Qt5Core)` matches the Qt version.
  For Qt6 bindings, set `%pyqt_build_for_qt6` and use `pkgconfig(Qt6Core)`.

### Missing python-qt5-sip runtime
```
ImportError: No module named PyQt5.sip
```
→ Add `Requires: python-qt5-sip` (for PyQt5) or `Requires: python-PyQt6-sip` (for PyQt6).

### Missing python-sip(api) runtime
```
ImportError: No module named sip
```
→ For SIP v4 bindings, add:
  ```
  Requires:       python-sip(api) = %{python_sip_api_ver}
  ```

### python-pyqt-rpm-macros not found
```
error: Macro %pyqt_build is not defined
```
→ Add `BuildRequires: python-pyqt-rpm-macros`.

### pyqt-builder not found
```
/usr/bin/pyqt-builder: No such file or directory
```
→ Add `BuildRequires: %%{python_module pyqt-builder}`.

## Investigating interactively

If you are unsure about the root cause, include [DEEP_ANALYZE] in your response.

When inside the build environment:
- Check SIP version: `sip -V 2>/dev/null || sip4 -V 2>/dev/null`
- Check PyQt installation: `python3 -c "from PyQt5 import QtCore; print(QtCore.PYQT_VERSION_STR)" 2>&1`
- Check Qt version: `pkg-config --modversion Qt5Core 2>/dev/null || pkg-config --modversion Qt6Core 2>/dev/null`
- Check available sip modules: `ls /usr/lib/python3.*/site-packages/*sip*`
- List python-pyqt-rpm-macros: `rpm -E %%pyqt_build 2>/dev/null`
"""
