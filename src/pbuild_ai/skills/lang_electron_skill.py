# lang_electron_skill.py
### WARNING: Currently not reviewed and adapted to git based packaging!
### Based on https://en.opensuse.org/openSUSE:Packaging_Electron

SKILL_NAME = "electron"

TARGET_PATTERN = r"(?i)electron.*\.spec$"
CONTENT_PATTERN = r"(?i)(?:%\{electron_rebuild\}|%\{electron_req\}|%\{electron_check_native\}|ELECTRON_SKIP_BINARY_DOWNLOAD|nodejs-electron|electron\.asar)"
PROMPT_PATTERN = r"(?i)(?:electron\s+packag|electron-builder|asar\s+file|electron\s+rpm|native\s+modul)"


OLLAMA_SPEC_PROMPT = """
You are an expert in Electron RPM packaging for openSUSE. Follow the openSUSE Electron packaging guidelines from https://en.opensuse.org/openSUSE:Packaging_Electron

## Build environment setup

Set `ELECTRON_SKIP_BINARY_DOWNLOAD=1` in `%build` to prevent the build from downloading prebuilt Electron binaries:

```
%build
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
npm ci --ignore-scripts
```

## Native modules

### Rebuild native modules

Use `%electron_rebuild` in `%build` to rebuild native Node.js modules against the system Electron:

```
%build
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
npm ci --ignore-scripts
%electron_rebuild
```

### Dependencies

```
Requires:  nodejs-electron%{_isa}
```

Use `%electron_req` as a shorthand for common Electron runtime dependencies.

## Spec template

```
Name:           electron-foo
Version:        1.0.0
Release:        0
Summary:        An Electron application
License:        MIT
Group:          Development/Tools/Other

BuildRequires:  nodejs-electron-devel

Requires:       nodejs-electron%{_isa}
%electron_req

%description
An Electron application that does useful things.

%prep
%autosetup -p1

%build
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
npm ci --ignore-scripts
%electron_rebuild

%install
# Install the application unpacked
mkdir -p %{buildroot}%{_libdir}/%{name}
cp -r . %{buildroot}%{_libdir}/%{name}

%check
%electron_check_native

%files
%license LICENSE
%doc README.md
%{_libdir}/%{name}
```

## Check section

For testing native modules after build:

```
%check
%electron_check_native
```

For unstable or experimental modules:

```
%check
%electron_check_native_unstable
```

## Removing precompiled binaries

Remove all precompiled binaries in `%prep` to ensure they are rebuilt for the target architecture:

```
find . -name '*.node' -delete
find . -name '*.jar' -delete
find . -name '*.dll' -delete
find . -name '*.exe' -delete
find . -name '*.dylib' -delete
find . -not -type d -name '*.so' -delete
find . -name '*.o' -delete
find . -name '*.a' -delete
find . -name '*.wasm' -delete
```

## C/C++ compiler flags

When building native modules, ensure the correct flags are set:

```
export CFLAGS="%{optflags} -fpic -fno-semantic-interposition"
export CXXFLAGS="%{optflags} -fpic -fno-semantic-interposition"
```

## Dependency management

Prefer npm over yarn for reproducible installs:

```
npm ci --ignore-scripts
```

Or with yarn:

```
yarn install --frozen-lockfile --ignore-scripts
```

## Asar archives

Avoid asar archives if possible — ship the application unpacked:
- Asar prevents security scanners from inspecting the application code
- Asar makes patching difficult for downstream distributions
- Ship the full directory tree in `%{_libdir}/%{name}` instead

## Known issues and workarounds

### esbuild DRM checks

esbuild includes DRM checks that fail in the build environment. Patch `install.js` and set:

```
export ESBUILD_BINARY_PATH=/path/to/esbuild
```

### @electron/fuses

`@electron/fuses` is broken by design for distribution packaging. Patch or remove the fuse checks.

### app.isPackaged

`app.isPackaged` always returns `false` when run from the package manager. Patch the application code if it depends on this value.

### process.execPath

`process.execPath` is broken when installed from system packages. Patch the application code to use the correct path.

### app.relaunch

`app.relaunch` is broken. Create a wrapper script that manually execs the application instead.

## Key rules

- Always set `ELECTRON_SKIP_BINARY_DOWNLOAD=1` in `%build`
- Use `%electron_rebuild` to rebuild native modules against the system Electron
- Remove all precompiled binaries in `%prep`
- Ship unpacked — avoid asar archives
- Set proper C/C++ flags: `-fpic -fno-semantic-interposition`
- Patch esbuild DRM checks if needed
- Patch `@electron/fuses`, `app.isPackaged`, `process.execPath`, and `app.relaunch` as needed
"""

OLLAMA_ERROR_PROMPT = """
You are debugging an Electron RPM build failure for openSUSE.

## Common Electron build errors

### electron binary not found
```
Error: Cannot find module 'electron'
```
→ The build is trying to download electron rather than using the system electron.
  Add `export ELECTRON_SKIP_BINARY_DOWNLOAD=1` in `%build` and ensure `BuildRequires: nodejs-electron-devel`.

### Native module fails to load - undefined symbol
```
Error: Module did not self-register: '/path/to/module.node'.
undefined symbol: _ZN2...
```
→ The native module was compiled against a different Node.js/Electron version.
  Run `%electron_rebuild` in `%build` to recompile against the system Electron.

### esbuild DRM check failure
```
Error: Cannot find esbuild binary
esbuild is not allowed to run in this environment
```
→ esbuild contains DRM checks that fail in the OBS build environment.
  Patch install.js and set `ESBUILD_BINARY_PATH`:
  ```
  export ESBUILD_BINARY_PATH=/usr/bin/esbuild
  ```

### @electron/fuses crash
```
Error: @electron/fuses: cannot run in a non-electron environment
```
→ `@electron/fuses` is broken by design for distro packaging. Patch or remove the fuse calls.

### Missing nodejs-electron dependency
```
Requires: nodejs-electron%{_isa}
```
→ Add `Requires: nodejs-electron%{_isa}` and `%electron_req` for runtime dependencies.
  Add `BuildRequires: nodejs-electron-devel` for the build.

### Precompiled binary architecture mismatch
```
error: Architecture mismatch for prebuilt binary
```
→ Remove precompiled binaries in `%prep` and rebuild with `%electron_rebuild`:
  ```
  find . -name '*.node' -delete
  find . -name '*.so' -delete
  ```

### npm ERR! network
```
npm ERR! code ECONNRESET
npm ERR! network request to https://registry.npmjs.org/ failed
```
→ The build environment has NO network access. Use `npm ci --ignore-scripts` with vendored dependencies.

## Investigating interactively

If you are unsure about the root cause, include [DEEP_ANALYZE] in your response.

When inside the build environment:
- Check Electron version: `electron --version`
- Check native modules: `ls %{buildroot}%{_libdir}/%{name}/node_modules/*/build/Release/*.node 2>/dev/null`
- Verify module symbols: `nm -D /path/to/module.node | grep undefined`
- Check npm cache: `ls ~/.npm/_cacache/ 2>/dev/null`
- Test electron rebuild: `electron-rebuild -f -v 2>&1`
"""


def fix_content(content: str) -> str:
    lines = content.split('\n')
    changed = False

    has_electron_rebuild = any('%electron_rebuild' in l for l in lines)
    has_skip_download = any('ELECTRON_SKIP_BINARY_DOWNLOAD' in l for l in lines)

    # 1. Add ELECTRON_SKIP_BINARY_DOWNLOAD=1 if %electron_rebuild used
    if has_electron_rebuild and not has_skip_download:
        new_lines = []
        in_build = False
        inserted = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == '%build':
                in_build = True
                new_lines.append(line)
            elif in_build and not inserted:
                if stripped.startswith('%') and not stripped.startswith('%{') and stripped != '%build':
                    new_lines.append('export ELECTRON_SKIP_BINARY_DOWNLOAD=1')
                    new_lines.append('')
                    new_lines.append(line)
                    inserted = True
                    changed = True
                elif stripped == '':
                    new_lines.append('export ELECTRON_SKIP_BINARY_DOWNLOAD=1')
                    new_lines.append('')
                    inserted = True
                    changed = True
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)
        if not inserted and in_build:
            new_lines.append('')
            new_lines.append('export ELECTRON_SKIP_BINARY_DOWNLOAD=1')
            changed = True
        if changed:
            content = '\n'.join(new_lines)
            lines = content.split('\n')

    # 2. Add Requires: nodejs-electron if %electron_rebuild used
    has_nodejs_electron_req = any('nodejs-electron' in l and 'Requires' in l for l in lines)
    if has_electron_rebuild and not has_nodejs_electron_req:
        new_lines = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if not inserted and line.strip().startswith('Requires'):
                new_lines.append('Requires:       nodejs-electron%{_isa}')
                inserted = True
                changed = True
        if not inserted:
            for i, line in enumerate(lines):
                if line.strip().startswith('BuildRequires') and not inserted:
                    new_lines = lines[:i] + ['Requires:       nodejs-electron%{_isa}'] + lines[i:]
                    inserted = True
                    changed = True
                    break
        if changed:
            content = '\n'.join(new_lines)

    return content
