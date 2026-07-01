# lang_nodejs_skill.py
### WARNING: Currently not reviewed and adapted to git based packaging!
### Based on https://github.com/AdamMajer/nodejs-packaging

SKILL_NAME = "nodejs"

TARGET_PATTERN = r"(?i)^nodejs-.*\.spec$|.*-nodejs.*\.spec$"
CONTENT_PATTERN = r"(?i)(?:nodejs-packaging|%\{nodejs_\}|%nodejs_|npm\s+ci|npm\s+install|node_module|ELECTRON_SKIP_BINARY_DOWNLOAD)"
PROMPT_PATTERN = r"(?i)(?:nodejs\s+packag|npm\s+rpm|node\.?js\s+rpm|javascript\s+rpm|node_module\s+rpm)"


OLLAMA_SPEC_PROMPT = """
You are an expert in Node.js RPM packaging for openSUSE. Follow the openSUSE Node.js packaging practices.

## BuildRequires

For automatic dependency generation:

```
BuildRequires:  nodejs-packaging
```

This enables automatic Provides and Requires for `nodejs(module)` based on the project's dependencies.

## Spec template

```
Name:           nodejs-foo
Version:        1.0.0
Release:        0
Summary:        A Node.js module/application
License:        MIT
Group:          Development/Libraries/Other

BuildRequires:  nodejs-packaging
BuildRequires:  nodejs-devel

%description
A Node.js module that does useful things.

%prep
%autosetup -p1

%build
npm ci --ignore-scripts

%install
mkdir -p %{buildroot}%{nodejs_sitelib}/%{name}
cp -r . %{buildroot}%{nodejs_sitelib}/%{name}

%files
%license LICENSE
%doc README.md
%{nodejs_sitelib}/%{name}
```

## Dependency management

### npm

For reproducible installs with npm:

```
npm ci --ignore-scripts
```

### yarn

For projects using yarn:

```
yarn install --frozen-lockfile --ignore-engines --ignore-platform --ignore-scripts --link-duplicates
```

### Vendoring

All dependencies must be vendored (included in the source tarball) because the OBS build environment has NO network access.

## Removing precompiled binaries

Remove all precompiled binaries in `%prep` to ensure proper rebuilding:

```
find . -name '*.node' -delete
find . -name '*.jar' -delete
find . -name '*.dll' -delete
find . -name '*.exe' -delete
find . -name '*.so' -delete
find . -name '*.dylib' -delete
```

## Electron applications

If packaging an Electron-based Node.js application in addition to its regular dependencies, set:

```
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
```

## Node.js version specific packaging

For applications that require a specific Node.js version:

```
BuildRequires:  nodejsXX        # e.g., nodejs18, nodejs20
BuildRequires:  nodejsXX-devel  # e.g., nodejs18-devel
```

## Key rules

- Always include `BuildRequires: nodejs-packaging` for automatic dependency generation
- Use `npm ci --ignore-scripts` for reproducible installs (preferred over `npm install`)
- For yarn projects, use `yarn install --frozen-lockfile --ignore-engines --ignore-platform --ignore-scripts --link-duplicates`
- Remove all precompiled binaries (.node, .jar, .dll, .exe, .so, .dylib) in %prep
- Vendor all dependencies in the source tarball — the build environment has no network access
- For Electron-based Node.js apps, set `ELECTRON_SKIP_BINARY_DOWNLOAD=1`
- Use version-specific `nodejsXX`/`nodejsXX-devel` BuildRequires when a specific runtime is needed
- Do NOT run postinstall scripts during build (use `--ignore-scripts`)
- Node.js modules typically go in `%{nodejs_sitelib}` which is `/usr/lib/node_modules`
"""

OLLAMA_ERROR_PROMPT = """
You are debugging a Node.js RPM build failure for openSUSE.

## Common Node.js build errors

### Missing nodejs or nodejs-devel

```
node: command not found
```
→ Add `BuildRequires: nodejs` or version-specific `BuildRequires: nodejs20`.

```
node-gyp: command not found
```
→ Add `BuildRequires: nodejs-devel` (or version-specific like `nodejs20-devel`).

### npm install failure in build environment (no network)

```
npm ERR! code ECONNRESET
npm ERR! network request to https://registry.npmjs.org/ failed
```
→ The build environment has NO network access. Ensure all dependencies are vendored in the source tarball.
  Use `npm ci --ignore-scripts` (which uses the lock file) instead of `npm install`.

### Node.js version mismatch

```
Error: The module 'foo.node' was compiled against a different Node.js version using NODE_MODULE_VERSION XXX.
```
→ The native module was compiled against a different Node.js version. Rebuild with the correct version.
  Ensure your `BuildRequires: nodejsXX-devel` matches the runtime Node.js version.

### Native module build failures (node-gyp)

```
gyp ERR! build error
make: *** [foo.target.mk: XXX] Error 1
```
→ node-gyp failed to compile a native module. Check:
  - Missing `BuildRequires: nodejs-devel` (for node-gyp headers)
  - Missing `BuildRequires: gcc-c++` (for C++ compilation)
  - Missing `BuildRequires: python3` (node-gyp requires Python)
  - Add missing dependencies and rebuild.

### Missing dependencies in vendored tarball

```
Error: Cannot find module 'bar'
Require stack:
- /usr/lib/node_modules/foo/index.js
```
→ A dependency is missing from the vendored source. Add the missing dependency to the source tarball.
  Run `npm ls --all` to identify missing dependencies.

### node_modules already present

If vendored tarballs ship node_modules:
→ Run `rm -rf node_modules` in %prep before `npm ci` to ensure a clean install.

### Installed (but unpackaged) file(s) found

```
error: Installed (but unpackaged) file(s) found:
   /usr/lib/node_modules/foo/bar.js
```
→ Add the full directory to %files: `%{nodejs_sitelib}/%{name}` or list the specific files.

## Investigating interactively

If you are unsure about the root cause, include [DEEP_ANALYZE] in your response.

When inside the build environment:
- Check Node.js version: `node --version`
- Check npm version: `npm --version`
- List installed Node.js packages: `ls /usr/lib/node_modules/`
- Check vendored dependencies: `ls node_modules/`
- Run dependency tree: `npm ls --all 2>&1`
- Check native module compatibility: `node -e "console.log(process.versions.modules)"`
"""
