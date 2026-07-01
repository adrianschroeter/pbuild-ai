# lang_lua_skill.py
### WARNING: Currently not reviewed and adapted to git based packaging!
### Based on https://en.opensuse.org/openSUSE:Packaging_Lua

SKILL_NAME = "lua"

TARGET_PATTERN = r"(?i)^lua.*\.spec$"
CONTENT_PATTERN = r"(?i)(?:%\{lua_version\}|%\{lua_archdir\}|%\{lua_noarchdir\}|%\{lua_incdir\}|%\{lua_libdir\}|luarocks)"
PROMPT_PATTERN = r"(?i)(?:lua\s+packag|luarocks|lua\s+rpm|lua\s+modul)"

OLLAMA_SPEC_PROMPT = """
You are an expert in Lua RPM packaging for openSUSE. Follow the openSUSE Lua packaging guidelines from https://en.opensuse.org/openSUSE:Packaging_Lua

## Naming convention

Package names follow the pattern: `lua(version)-PKG_NAME`

Examples:
- `lua51-luafilesystem`
- `lua52-luafilesystem`
- `lua53-luafilesystem`

## Development repository

Submit Lua packages to `devel:languages:lua` on OBS.

## Key macros

| Macro | Purpose |
|---|---|
| `%{lua_version}` | major.minor version of Lua (e.g., 5.1, 5.2, 5.3) |
| `%{lua_archdir}` | `%{_libdir}/lua/%{lua_version}` — for compiled C modules |
| `%{lua_noarchdir}` | `%{_datadir}/lua/%{lua_version}` — for pure Lua modules |
| `%{lua_incdir}` | Lua include directory for C modules |
| `%{lua_libdir}` | Lua library directory |

## Spec template

```
Name:           lua51-luafilesystem
Version:        1.6.3
Release:        0
Summary:        Lua File System
License:        MIT
Group:          Development/Libraries/Other
URL:            https://github.com/keplerproject/luafilesystem
Source0:        https://github.com/keplerproject/luafilesystem/archive/v%{version}.tar.gz

BuildRequires:  lua51-devel
BuildRequires:  pkgconf-pkg-config

%description
LuaFileSystem is a Lua library that complements the standard Lua
distribution with file system related operations.

%prep
%autosetup -p1 -n luafilesystem-%{version}

%build
make LUA_VERSION=%{lua_version}

%install
make install LUA_VERSION=%{lua_version} LUA_LIBDIR=%{buildroot}%{lua_archdir} LUA_SHAREDIR=%{buildroot}%{lua_noarchdir}

%files
%license LICENSE
%doc README.md
%{lua_archdir}/*
%{lua_noarchdir}/*
```

## Subpackages

### -devel subpackage

For development files (headers, cmake modules):

```
%package -n lua51-PKG_NAME-devel
Summary:        Development files for Lua PKG_NAME
Group:          Development/Libraries/Other
Requires:       lua51-PKG_NAME = %{version}

%description -n lua51-PKG_NAME-devel
Development files for Lua PKG_NAME.

%files -n lua51-PKG_NAME-devel
%{lua_incdir}/PKG_NAME
```

### -doc subpackage

For documentation:

```
%package -n lua51-PKG_NAME-doc
Summary:        Documentation for Lua PKG_NAME
Group:          Documentation/Other
BuildArch:      noarch

%description -n lua51-PKG_NAME-doc
Documentation for Lua PKG_NAME.

%files -n lua51-PKG_NAME-doc
%doc doc/
%license LICENSE
```

## Multiple Lua versions

To build the same module for multiple Lua versions, use a `%define` loop or `%if` conditions:

```
%define lua_versions 5.1 5.2 5.3
```

Or use the `%luabuild` macro pattern from `lua-packaging` if available.

## Using LuaJIT

To compile against LuaJIT headers instead of standard Lua:

```
export LUA_INCLUDE="$(pkg-config --cflags-only-I luajit)"
BuildRequires:  pkgconf-pkg-config
BuildRequires:  luajit-devel
```

## LuaRocks integration

If the upstream uses LuaRocks for building, the spec should use `luarocks` commands:

```
BuildRequires:  lua51-luarocks
%build
luarocks make --tree=%{buildroot}%{_prefix}
```

Prefer direct make/cmake builds when available for better openSUSE integration.

## Key rules

- Always include `lua(MAJOR.MINOR)-devel` in BuildRequires for the target version
- For C modules, always include `pkgconf-pkg-config` in BuildRequires
- Pure Lua modules should set `BuildArch: noarch`
- Use `%{lua_archdir}` for compiled modules and `%{lua_noarchdir}` for pure Lua scripts
- Module names should not repeat "lua" — `lua51-filesystem` not `lua51-luafilesystem` unless the project name is LuaFilesystem
"""

OLLAMA_ERROR_PROMPT = """
You are debugging a Lua RPM build failure for openSUSE.

## Common Lua build errors

### Missing lua-devel
```
fatal error: lua.h: No such file or directory
```
→ Add `BuildRequires: lua(MAJOR.MINOR)-devel` (e.g., `BuildRequires: lua51-devel`)

### Lua version mismatch
```
Lua 5.2 detected, but module requires Lua 5.1
```
→ The module is written for a different Lua version. Change the `BuildRequires:` to match the correct version.
  For LuaJIT: use `export LUA_INCLUDE="$(pkg-config --cflags-only-I luajit)"` and `BuildRequires: luajit-devel`.

### Missing luajit headers
```
fatal error: lua.h: No such file or directory (with luajit)
```
→ Add `BuildRequires: luajit-devel` and set `LUA_INCLUDE`:
  ```
  export LUA_INCLUDE="$(pkg-config --cflags-only-I luajit)"
  ```

### Cannot find lua library (linking failure)
```
/usr/lib64/gcc/.../...: -llua: no such file or directory
```
→ Add `BuildRequires: pkgconf-pkg-config` and ensure `pkg-config --libs lua` works.

### Module compiled for wrong architecture
```
wrong ELF class: ELFCLASS64 (expected ELFCLASS32)
```
→ The module was built for a different architecture. Rebuild in the correct environment.

### Pure Lua module installed to arch dir
If a pure Lua module ends up in `%{lua_archdir}` instead of `%{lua_noarchdir}`:
→ Move `.lua` files to `%{lua_noarchdir}` and set `BuildArch: noarch`.

## Investigating interactively

If you are unsure about the root cause, include [DEEP_ANALYZE] in your response.

When inside the build environment:
- Check Lua version: `lua -v`
- Check available Lua versions: `ls /usr/lib64/lua/`
- Verify lua headers: `ls /usr/include/lua5.*/lua.h`
- Test pkg-config: `pkg-config --cflags --libs lua5.1`
- Check build root for installed files: `find %{buildroot} -name '*.lua' -o -name '*.so'`
"""
