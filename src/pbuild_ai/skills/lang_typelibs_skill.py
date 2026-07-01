# lang_typelibs_skill.py
### WARNING: Currently not reviewed and adapted to git based packaging!
### Based on https://en.opensuse.org/openSUSE:Packaging_Typelibs

SKILL_NAME = "lang_typelibs"

TARGET_PATTERN = r"(?i)typelib.*\.spec$"
CONTENT_PATTERN = r"(?i)(?:gobject-introspection|typelib-|girepository|%\{_typelibdir\}|%\{gir_dir\})"
PROMPT_PATTERN = r"(?i)(?:typelib|gobject-introspection|gir\s+(?:file|scan)|girepository)"


OLLAMA_SPEC_PROMPT = """
You are an expert in Typelib (GObject Introspection) RPM packaging for openSUSE. Follow the openSUSE Typelibs packaging guidelines from https://en.opensuse.org/openSUSE:Packaging_Typelibs

## Naming convention

Package names follow the pattern: `typelib-<GIVersion>-<TypeLibName>-<TypeLibVersion>`

Dots in `<GIVersion>` and `<TypeLibVersion>` are replaced with underscores (e.g., `0.2` → `0_2`).

### Examples

| Typelib file | Package name |
|---|---|
| `/usr/lib/girepository-1.0/Memphis-0.2.typelib` | `typelib-1_0-Memphis-0_2` |
| `/usr/lib/girepository-1.0/Gtk-3.0.typelib` | `typelib-1_0-Gtk-3_0` |
| `/usr/lib/girepository-1.0/GLib-2.0.typelib` | `typelib-1_0-GLib-2_0` |

## BuildRequires

```
BuildRequires:  gobject-introspection
```

The `gobject-introspection` package triggers the automatic Typelib dependency scanner, which:
- Detects installed `.typelib` files in `%{_libdir}/girepository-1.0/`
- Generates Provides/Requires for each typelib

## Dependency handling

Typelib packages automatically:
- Require the library they wrap (e.g., `typelib-1_0-Gtk-3_0` requires `libgtk-3.so.0`)
- Require other typelib packages they depend on (e.g., Gtk typelib requires GLib typelib)

## Versioning

Typelibs are versioned and can be installed in multiple versions simultaneously:
- `typelib-1_0-Gtk-3_0`
- `typelib-1_0-Gtk-4_0`

## Spec template

```
Name:           typelib-1_0-Memphis-0_2
Version:        0.2.0
Release:        0
Summary:        GObject Introspection bindings for Memphis
License:        LGPL-2.1-or-later
Group:          Development/Libraries/GNOME
BuildRequires:  gobject-introspection
BuildRequires:  pkgconfig(memphis)

%description
GObject Introspection bindings for the Memphis library.

%prep
%autosetup -p1

%build
%meson -Dintrospection=enabled
%meson_build

%install
%meson_install

%files
%license COPYING
%doc README
%{_libdir}/girepository-1.0/Memphis-0.2.typelib
```

## Key rules

- Always include `BuildRequires: gobject-introspection` to trigger the automatic dependency scanner
- List `.typelib` files in `%files` under `%{_libdir}/girepository-1.0/`
- Multiple typelib version packages can coexist (e.g., Gtk3 and Gtk4)
- The typelib package name must match the exact file name with dots replaced by underscores
- Typelibs are most commonly used by the GNOME stack but can wrap any GObject-based library
- Do NOT add manual Provides/Requires for typelib dependencies — the scanner handles this
"""

OLLAMA_ERROR_PROMPT = """
You are debugging a Typelib (GObject Introspection) RPM build failure for openSUSE.

## Common Typelib build errors

### Missing gobject-introspection BuildRequires
```
W: no-introspection-packaging-tag
```
→ Add `BuildRequires: gobject-introspection` to enable the automatic typelib dependency scanner.

### Unpackaged .typelib files
```
error: Installed (but unpackaged) file(s) found:
   /usr/lib/girepository-1.0/Foo-1.0.typelib
```
→ Add the .typelib file to `%files`:
   `%{_libdir}/girepository-1.0/Foo-1.0.typelib`

### GIR version mismatch
```
Requires: typelib(GLib) = 2.0 but typelib-1_0-GLib-2_0 provides typelib(GLib) = 2.0
```
→ Ensure the package name matches the file name exactly. Run `ls %{buildroot}%{_libdir}/girepository-1.0/` to verify the actual filename.

### Missing library dependency
```
Error: Could not find library for typelib Foo-1.0
```
→ The .typelib file references a shared library that is not installed. Add the corresponding `BuildRequires:` (e.g., `pkgconfig(foo)`) and ensure the library is packaged.

### Introspection data not generated
```
/build/.../Foo-1.0.gir: No such file or directory
```
→ The build system did not generate GIR files. Enable introspection in the build:
  - Meson: `-Dintrospection=enabled` or `-Dgir=true`
  - Autotools: `--enable-introspection`
  - CMake: `-DINTROSPECTION=ON`

### Typelib file in wrong location
```
Installed (but unpackaged) file(s) found:
   /usr/share/gir-1.0/Foo-1.0.gir
```
→ Typelib (.typelib) and GIR (.gir) files are different. Only `.typelib` files in `%{_libdir}/girepository-1.0/` are scanned by the automatic dependency generator. GIR files in `/usr/share/gir-1.0/` are for development and should go in a -devel package.

## Investigating interactively

If you are unsure about the root cause, include [DEEP_ANALYZE] in your response.

When inside the build environment:
- List installed typelib files: `find %{buildroot}%{_libdir}/girepository-1.0 -name '*.typelib' 2>/dev/null`
- Check GIR files: `ls /usr/share/gir-1.0/ 2>/dev/null`
- Verify typelib dependencies: `LD_LIBRARY_PATH=%{buildroot}%{_libdir} g-ir-inspect Foo-1.0 2>&1`
- Check typelib metadata: `g-ir-typelib-analyze %{buildroot}%{_libdir}/girepository-1.0/Foo-1.0.typelib 2>/dev/null`
"""
