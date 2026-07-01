# lang_erlang_skill.py
### WARNING: Currently not reviewed and adapted to git based packaging!
### Based on https://en.opensuse.org/openSUSE:Packaging_Erlang

SKILL_NAME = "lang_erlang"

TARGET_PATTERN = r"(?i)^erlang-.*\.spec$"
CONTENT_PATTERN = r"(?i)(?:%\{erlang_dir\}|%\{erlang_libdir\}|%\{__erl\}|%\{__rebar\}|%rebar\b|%rebar_compile|otp)"
PROMPT_PATTERN = r"(?i)(?:erlang\s+packag|rebar[23]?\s|otp\s+packag|beam\s+file|erlang\s+rpm)"


OLLAMA_SPEC_PROMPT = """
You are an expert in Erlang RPM packaging for openSUSE. Follow the openSUSE Erlang packaging guidelines from https://en.opensuse.org/openSUSE:Packaging_Erlang

## Development repository

Submit Erlang packages to `devel:languages:erlang` on OBS.

## Key macros

| Macro | Definition | Purpose |
|---|---|---|
| `%erlang_dir` | `%{_libdir}/erlang` | Main Erlang installation directory |
| `%erlang_libdir` | `%{_libdir}/erlang/lib` | Erlang library directory |
| `%__erl` | calls `/usr/bin/erl` | Invokes the Erlang runtime |
| `%__rebar` | path to rebar binary | Invokes the rebar build tool |
| `%rebar` | sets CFLAGS/CXXFLAGS/FFLAGS | Runs rebar with proper compiler flags |
| `%rebar_compile` | rebar compile wrapper | Compiles Erlang source |

## Spec template

```
Name:           erlang-foo
Version:        1.0.0
Release:        0
Summary:        An Erlang library
License:        Apache-2.0
Group:          Development/Libraries/Other
URL:            https://github.com/example/foo
Source0:        https://github.com/example/foo/archive/v%{version}.tar.gz

BuildRequires:  erlang-devel
BuildRequires:  rebar

%description
An Erlang library that does useful things.

%prep
%autosetup -p1

%build
%rebar_compile

%install
%rebar install
```

## BuildRequires

```
BuildRequires:  erlang-devel
BuildRequires:  rebar
```

## Rebar3 caveats

- Rebar3 does NOT support `SKIP_DEPS=true` — it always fetches dependencies from the internet.
- The build environment has NO network access, so rebar3 will fail.
- Workaround: Remove the lock file and patch the `deps` tuple out of the build file before building.

Example workaround:
```
%build
rm -f rebar.lock
# Patch out dependency fetching from rebar.config
```

- Vendoring support for rebar3 is being discussed with upstream. Currently, rebar3-based Erlang packages are difficult to package in openSUSE.

## Versioning

- Use upstream version tags (e.g., `1.0.0`)
- For VCS snapshots, use `0.0.0+gitYYYYMMDD`

## Key rules

- Always include `BuildRequires: erlang-devel` for Erlang development headers and runtime
- Always include `BuildRequires: rebar` for rebar-based builds
- Use `%rebar` or `%rebar_compile` macros for building — they set the correct compiler flags
- Do NOT use `SKIP_DEPS=true` with rebar3 (it is unsupported)
- Patch out dependency fetching from rebar3 build files when packaging
- Ship BEAM files (.beam, .app, .app.src) in `%erlang_libdir`
- Prefer rebar (rebar2) over rebar3 for packaging due to rebar3's network dependency issues
"""

OLLAMA_ERROR_PROMPT = """
You are debugging an Erlang RPM build failure for openSUSE.

## Common Erlang build errors

### Missing rebar or erlang-devel
```
rebar: command not found
```
→ Add `BuildRequires: rebar`

```
erlc: command not found
```
→ Add `BuildRequires: erlang-devel`

### Rebar3 trying to fetch dependencies from network
```
===> Fetching deps from https://...
===> connect: econnrefused
```
→ The build environment has NO network access. Rebar3 does not support `SKIP_DEPS=true`.
  Workaround: Remove the lock file and patch the `deps` tuple out of the build file in `%prep`:
  ```
  rm -f rebar.lock
  sed -i '/{deps,/d' rebar.config
  ```

### BEAM file version mismatch
```
Error: beam/beam_load.c(177): Error loading module foo:
  This BEAM file was compiled for a later version of the runtime
```
→ The module was compiled against a newer Erlang/OTP version than what is available at runtime.
  Rebuild against the target OTP version by using the correct `BuildRequires: erlang-devel`.

### Missing BEAM files in package
```
error: Installed (but unpackaged) file(s) found:
   /usr/lib/erlang/lib/foo-1.0/ebin/foo.beam
```
→ Add the ebin directory to `%files`:
   `%{erlang_libdir}/foo-%{version}/ebin/*.beam`

### Undefined application
```
{"error",{"Error reading application file","no such file or directory","foo.app"}}
```
→ The .app or .app.src file is missing from the ebin directory. Ensure the build process generates the application resource file.

## Investigating interactively

If you are unsure about the root cause, include [DEEP_ANALYZE] in your response.

When inside the build environment:
- Check Erlang/OTP version: `erl -version`
- List installed BEAM files: `find %{buildroot}%{erlang_libdir} -name '*.beam' 2>/dev/null`
- Check rebar version: `rebar --version 2>/dev/null || rebar3 version 2>/dev/null`
- Read rebar.config: `cat rebar.config`
- Try manual rebar build: `cd /home/abuild/rpmbuild/BUILD && rebar compile`
"""
