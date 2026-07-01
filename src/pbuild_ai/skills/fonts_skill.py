# fonts_skill.py

SKILL_NAME = "fonts"

TARGET_PATTERN = r"(?i).*-fonts\.spec$"
CONTENT_PATTERN = r"(?i)(?:%\{_ttfontsdir\}|%\{_oftfontsdir\}|%reconfigure_fonts|fontpackages-devel|%install_fontsconf|%files_fontsconf)"
PROMPT_PATTERN = r"(?i)(?:font\s+packag|ttf|otf|fontconfig|fonts\.conf|typeface)"

OLLAMA_SPEC_PROMPT = """
You are an expert in font RPM packaging for openSUSE. Follow the openSUSE font packaging guidelines from https://en.opensuse.org/openSUSE:Packaging_Fonts

## Naming convention

All lower-case: `[foundryname-]projectname[-fontfamilyname][-fonttype]-fonts`

Examples: `foo-fonts`, `dejavu-fonts`, `cantarell-fonts`

## BuildRequires

Always:
```
BuildRequires:  fontpackages-devel
%reconfigure_fonts_prereq
```

If the font archive is zipped:
```
BuildRequires:  unzip
```

If packaging fontconfig files:
```
BuildRequires:  fontconfig
```

## Essential macros

| Macro | Purpose |
|---|---|
| `%{_ttfontsdir}` | TrueType font installation dir (`/usr/share/fonts/truetype`) |
| `%{_oftfontsdir}` | OpenType font installation dir (`/usr/share/fonts/opentype`) |
| `%reconfigure_fonts_prereq` | Adds build-time fontconfig dependency |
| `%reconfigure_fonts_scriptlets` | Adds `%post`/`%postun` scriptlets to refresh font cache |
| `%install_fontsconf FILE` | Installs a fontconfig file into availdir and links into confdir |
| `%files_fontsconf_availdir` | Includes the availdir in `%files` |
| `%files_fontsconf_file -l FILE` | Includes a fontconfig file with its confdir link |

## Spec template

```
%define fontname foo

Name:           foo-fonts
Version:        1.0
Release:        0
Summary:        Foo font family
License:        OFL-1.1
Group:          System/X11/Fonts
BuildArch:      noarch
BuildRequires:  fontpackages-devel
%reconfigure_fonts_prereq
Source0:        %{fontname}-%{version}.tar.bz2

%description
Foo fonts is ...

Designer: Name of the font designer

%prep
%autosetup -p1 -n %{fontname}-%{version}

%build
# Usually nothing to do

%install
install -d '%{buildroot}%{_ttfontsdir}'
install -t '%{buildroot}%{_ttfontsdir}' -m 644 *.ttf

%reconfigure_fonts_scriptlets

%files
%license LICENSE
%doc README
%{_ttfontsdir}
```

## Subpackages (multiple families)

For multi-family font projects, split into subpackages:

```
%package -n %{projname}-family1-fonts
Summary:        ...
Group:          System/X11/Fonts

%description -n %{projname}-family1-fonts
...

%files -n %{projname}-family1-fonts
%license OFL.txt
%dir %{_ttfontsdir}/
%{_ttfontsdir}/family1.*
```

## Fontconfig files

To ship a fontconfig file with the font package:

```
Source1:        31-%{fontname}-fonts.conf
BuildRequires:  fontconfig

%install
%install_fontsconf %{SOURCE1}

%files
%files_fontsconf_availdir
%files_fontsconf_file -l 31-%{fontname}.conf
```

## Key rules

- Always set `BuildArch: noarch` (fonts are architecture-independent)
- Use `%{_ttfontsdir}` for TrueType (`.ttf`) and `%{_oftfontsdir}` for OpenType (`.otf`)
- Always call `%reconfigure_fonts_scriptlets` in `%install` to generate `%post`/`%postun` scriptlets
- Do NOT set `Requires` on the main font package — fontconfig handles discovery
- Do NOT mark font files as `%config` — users override via fontconfig
- Do NOT use `%{_datadir}/fonts/...` directly — always use the `%{_ttfontsdir}`/`%{_oftfontsdir}` macros
- Provide locale capabilities for multi-language fonts: `Provides: scalable-font-LANG`, `Provides: locale(LANG)`
- Font packages belong in the `M17N:fonts` repository on OBS
"""

OLLAMA_ERROR_PROMPT = """
You are debugging a font RPM build failure for openSUSE.

## Common font build errors

### bdftopcf: command not found
→ Add `BuildRequires: bdftopcf` (on openSUSE >= 12.2)

### xmkmf/imake: command not found
→ Add `BuildRequires: imake` and `BuildRequires: xorg-cf-files`

### mkfontdir: command not found
→ Add `BuildRequires: mkfontdir`

### fc-cache: command not found
→ Add `BuildRequires: fontconfig` (already pulled by `%reconfigure_fonts_prereq`)

### Installed (but unpackaged) file(s) found
```
error: Installed (but unpackaged) file(s) found:
   /usr/share/fonts/truetype/foo.ttf
```
→ The font files are installed but not listed in `%files`. Add `%{_ttfontsdir}` or `%{_oftfontsdir}`.

### No %post scriptlet for font cache refresh
If the font works after manual `fc-cache` but not immediately after install:
→ Make sure `%reconfigure_fonts_scriptlets` is called in `%install` section.

### Wrong BuildArch
```
Package foo-fonts is marked noarch but contains architecture-dependent files
```
→ Fonts MUST be `BuildArch: noarch`. Ensure no binary executables end up in the package.

### Obsoletes self-obsoletion warning
```
W: self-obsoletion OldName <= 1.0 obsoletes OldName = 1.0
```
→ This is expected during font renames. Normal rpmlint warning, can be ignored.

## Investigating interactively

If you need to investigate inside the build environment, include [DEEP_ANALYZE] in your response.

When inside the build environment:
- Check font files: `ls -la %{buildroot}%{_ttfontsdir}/`
- Verify font metadata: `fc-scan %{buildroot}%{_ttfontsdir}/foo.ttf 2>/dev/null | head -20`
- Check fontconfig: `FC_DEBUG=1 fc-match foo 2>&1 | head -30`
"""

DEEP_ANALYZE_PROMPT = """
## Interactive font investigation

You are inside the build environment. Investigate font packaging issues:

1. **List installed font files**: `find %{buildroot}%{_ttfontsdir} -type f 2>/dev/null || find %{buildroot}%{_oftfontsdir} -type f 2>/dev/null`
2. **Check font metadata**: `fc-scan /path/to/font.ttf 2>/dev/null`
3. **Verify fontconfig integration**: `fc-list | grep FONTNAME`
4. **Validate font file**: `python3 -c "from fontTools import ttLib; f = ttLib.TTFont('/path/to/font.ttf'); print(f)"` 2>/dev/null || echo "fontTools not available"
5. **Check for missing license files**: Fonts must include license (OFL, GPL, etc.) in `%files`
"""


def fix_content(content: str) -> str:
    """Pre-build spec content fixes for common font packaging issues."""
    lines = content.split('\n')
    changed = False

    # 1. Ensure BuildArch: noarch if font macros are used
    has_font_macros = any(
        '%{_ttfontsdir}' in l or '%{_oftfontsdir}' in l or 'fontpackages-devel' in l
        for l in lines
    )
    has_noarch = any('BuildArch' in l and 'noarch' in l for l in lines)
    if has_font_macros and not has_noarch:
        new_lines = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if not inserted and line.strip().startswith('BuildRequires'):
                new_lines.append('BuildArch:      noarch')
                inserted = True
                changed = True
        if inserted:
            content = '\n'.join(new_lines)
            lines = content.split('\n')

    # 2. Ensure %reconfigure_fonts_scriptlets is present if font dirs are used
    has_reconfigure = any('%reconfigure_fonts_scriptlets' in l for l in lines)
    has_install_section = False
    install_end = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == '%install':
            has_install_section = True
        if has_install_section and install_end == -1 and stripped.startswith('%') and not stripped.startswith('%{') and stripped != '%install':
            install_end = i
            break
    if install_end == -1 and has_install_section:
        install_end = len(lines)

    if has_font_macros and not has_reconfigure and install_end > 0:
        new_lines = []
        for i, line in enumerate(lines):
            new_lines.append(line)
            if i == install_end - 1:
                new_lines.append('%reconfigure_fonts_scriptlets')
                changed = True
        if changed:
            content = '\n'.join(new_lines)
            lines = content.split('\n')

    # 3. Add fontpackages-devel BuildRequires if _ttfontsdir or _oftfontsdir is used
    has_fontpkg_br = any('fontpackages-devel' in l and 'BuildRequires' in l for l in lines)
    if has_font_macros and not has_fontpkg_br:
        new_lines = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if not inserted and line.strip().startswith('BuildRequires'):
                new_lines.append('BuildRequires:  fontpackages-devel')
                inserted = True
                changed = True
        if inserted:
            content = '\n'.join(new_lines)

    return content
