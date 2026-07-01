# go_skill.py

SKILL_NAME = "lang_go"

TARGET_PATTERN = r"(?i)^(?:golang|go-).*\.spec$"
CONTENT_PATTERN = r"(?i)(?:%\{go_|%goprep\b|%gobuild\b|%goinstall\b|%gosrc\b|%gofilelist\b|BuildRequires:\s*golang-packaging|<service\s+name=\"go_modules\")"
PROMPT_PATTERN = r"(?i)(?:golang\s+packag|go\s+rpm|go\s+module|goprep|gobuild|goinstall|vendor\.tar)"


OLLAMA_SPEC_PROMPT = """
You are an expert in Go (golang) RPM packaging for openSUSE. Follow the openSUSE Go packaging guidelines from https://en.opensuse.org/openSUSE:Packaging_Go

## Go modules approach (modern, preferred)

Most Go applications now use Go modules. Use this approach:

### Vendor modules

git based packages have RemoteAsset support for go modules enabled by default.
To activate it, copy go.mod and go.sum file next to the build description
(eg. the PACKAGE.spec file)

### Spec template (modules)

```
BuildRequires:  golang-packaging
BuildRequires:  zstd
Source0:        %{name}-%{version}.tar.gz

%prep
%autosetup -p1 -a1

%build
go build \\
   -mod=vendor \\
   -buildmode=pie

%install
install -D -m0755 %{name} %{buildroot}%{_bindir}/%{name}
install -D -m0644 man/%{name}.1 %{buildroot}%{_mandir}/man1/%{name}.1

%files
%license LICENSE
%doc README.md
%{_bindir}/%{name}
%{_mandir}/man1/%{name}.1%{?ext_man}
```

## Old style (library packages using golang-packaging macros)

Use this template for Go libraries that don't support modules:

```
%global provider_prefix github.com/example/repo
%global import_path     %{provider_prefix}

Name:           golang-%{provider_prefix}
Version:        0.0.0+git20140916
Release:        0
BuildRequires:  golang-packaging

%{go_provides}

%prep
%autosetup -p1 -n %{name}-%{version}

%build
%goprep %{import_path}
%gobuild .

%install
%goinstall
%gosrc

%gofilelist

%check
%gotest %{import_path}

%files -f file.lst
%license LICENSE
%doc README
```

## Naming convention

- Name starts with `golang-` prefix
- Derived from the Go import path (e.g., `github.com/user/repo` → `golang-github-user-repo`)
- Strip leading `go-` from package name (e.g., `go-logging` → `golang-github-xxx-logging`, not `golang-github-xxx-go-logging`)
- Skip `.go` suffix in import paths (replace with `-go` only if needed)

## Versioning

- Use `0.0.0+gitYYYYMMDD` for VCS snapshots
- Use upstream version tags if available

## Key macros

| Macro | Purpose |
|---|---|
| `%goprep IMPORTPATH` | Prepares the GOPATH layout (creates `BUILD/go/src/IMPORTPATH`) |
| `%gobuild [dir]` | Builds and installs compiled code into `%{_builddir}` |
| `%goinstall` | Copies compiled code to `%{buildroot}` |
| `%gosrc` | Copies `.go`, `.s`, `.h` source files to `%{go_contribsrcdir}` |
| `%gofilelist` | Generates `file.lst` for `%files -f file.lst` |
| `%gotest IMPORTPATH` | Runs tests |
| `%{go_provides}` | Adds `Provides: %{name}-devel` and `%{name}-devel-static` |
| `%{go_contribdir}` | `%{_libdir}/go/contrib/pkg/linux_%{go_arch}` |
| `%{go_contribsrcdir}` | `%{_datadir}/go/contrib/src/pkg` |

## BuildRequires

- Always: `BuildRequires: golang-packaging`
- If linking C code: add the corresponding `-devel` packages

## Requires

Go packages are statically built — ldconfig does not detect shared library dependencies.
**Your BuildRequires are also your Requires.** Add explicit `Requires:` lines matching your `BuildRequires:`.

Use: `Requires: golang(IMPORTPATH)` for Go source dependencies.

## ExclusiveArch

Go only works on certain architectures. Use:
```
ExclusiveArch:  %{go_arches}
```

## Warnings

- Do NOT use `go get` in %build — the build environment has no network access.
- Do NOT quote macros like `%goprep`, `%gobuild`, `%goinstall`, `%gosrc`, `%gotest` — they take arguments and quoting breaks them.
- `spec-cleaner` may incorrectly quote these macros — verify after running it.
- Examples with `main()` functions in library packages often need to be moved to an `examples/` subdirectory to avoid "main redeclared in this block" errors.
"""

OLLAMA_ERROR_PROMPT = """
You are debugging a Go RPM build failure for openSUSE.

## Common Go build errors

### cannot find package "IMPORTPATH"
```
test.go:2:8: cannot find package "IMPORTPATH" in any of:
        /usr/lib64/go/src/pkg/IMPORTPATH (from $GOROOT)
        /usr/lib64/go/contrib/src/IMPORTPATH
```
→ Missing BuildRequires for the required Go library.
  Add: `BuildRequires: golang(IMPORTPATH)` and `Requires: golang(IMPORTPATH)`.

### undefined reference to SOME_C_FUNCTION
Go uses cgo to call C code. The build is missing C headers or libraries.
→ Add `BuildRequires: pkgconfig(LIBRARY)` or `BuildRequires: LIBRARY-devel`.

### main redeclared in this block
A directory contains multiple `.go` files with `main()` functions (often in examples/).
→ Move example files with `main()` into an `examples/` subdirectory in `%prep`:
```
mkdir -p examples
mv *_test.go examples/  # or move files with main()
```

### object is [linux amd64 go1.3 X:precisestack] expected [linux amd64 go1.3.1 X:precisestack]
The package was built against a different Go version. Rebuild against the current Go.

### stat "bitbucket.org/taruti/ssh.go" No such file or directory
`.go` in import paths is no longer allowed. Change the import path to use `-go` suffix instead.
→ Replace `%goprep bitbucket.org/taruti/ssh.go` with `%goprep bitbucket.org/taruti/ssh-go`.
  This usually means the project is too old and should be replaced.

### Permission denied: /usr/lib64/go/pkg/tool/linux_amd64/
Go tools try to install into GOROOT which is not writable.
→ Export `GOROOT_TARGET` before %gobuild:
```
export GOROOT_TARGET="%{buildroot}%{_libdir}/go"
```

### BuildRequires duplicates as Requires
If you get runtime "cannot find package" errors, the package needs explicit `Requires:`.
→ Copy each `BuildRequires: golang(IMPORTPATH)` as `Requires: golang(IMPORTPATH)`.

## Investigating interactively

If you are unsure about the root cause, include [DEEP_ANALYZE] in your response.

When inside the build environment:
- Check GOPATH: `go env GOPATH GOROOT`
- List available Go packages: `ls /usr/lib64/go/contrib/pkg/linux_amd64/`
- Check Go version: `go version`
- Try manual build: `cd /home/abuild/rpmbuild/BUILD/go/src/IMPORTPATH && go build .`
"""

DEEP_ANALYZE_PROMPT = """
## Interactive Go investigation

You are inside the build environment. Investigate Go build issues:

1. **Check Go version**: `go version`
2. **Check GOPATH/GOROOT**: `go env GOPATH GOROOT`
3. **List installed Go libraries**: `find /usr/lib64/go/contrib/pkg/linux_amd64/ -name '*.a' | head -20`
4. **Check build directory layout**: `ls -la /home/abuild/rpmbuild/BUILD/go/src/`
5. **Try importing the failing package**: `cd /home/abuild/rpmbuild/BUILD/go/src/IMPORTPATH && go build -v . 2>&1`
6. **Check for C headers needed by cgo**: `pkg-config --list-all | grep -i NEEDED`
7. **Check go.mod and go.sum file** (if go modules)
"""


def fix_content(content: str) -> str:
    """Pre-build spec content fixes for common Go packaging issues."""
    lines = content.split('\n')
    changed = False

    # 1. Add %{go_provides} if golang-packaging is in BuildRequires but %{go_provides} is missing
    has_golang_packaging = any(
        'golang-packaging' in l and 'BuildRequires' in l
        for l in lines
    )
    has_go_provides = any('%{go_provides}' in l for l in lines)

    if has_golang_packaging and not has_go_provides:
        new_lines = []
        insert_before = '%prep'
        inserted = False
        for line in lines:
            if not inserted and line.strip().startswith(insert_before):
                new_lines.append('%{go_provides}')
                new_lines.append('')
                inserted = True
                changed = True
            new_lines.append(line)
        if inserted:
            content = '\n'.join(new_lines)
            lines = content.split('\n')

    # 2. Add ExclusiveArch if missing and golang-packaging is used
    has_exclusive_arch = any('ExclusiveArch' in l for l in lines)
    if has_golang_packaging and not has_exclusive_arch:
        new_lines = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if not inserted and line.strip().startswith('BuildRequires'):
                new_lines.append('ExclusiveArch:  %{go_arches}')
                inserted = True
                changed = True
        if inserted:
            content = '\n'.join(new_lines)

    # 3. Check for quoted macros that shouldn't be quoted
    quoted_macros = ['%goprep', '%gobuild', '%goinstall', '%gosrc', '%gotest']
    for qm in quoted_macros:
        if qm in content:
            import re
            # Fix %{goprep ...} -> %goprep ...
            content = re.sub(r'%\{(' + qm[1:] + r')((?:\s+\S+)?)\}',
                             r'%\1\2', content)
            changed = True

    return content
