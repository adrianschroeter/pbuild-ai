# lang_perl_skill.py
### WARNING: Currently not reviewed and adapted to git based packaging!
### Based on https://en.opensuse.org/openSUSE:Packaging_Perl

SKILL_NAME = "lang_perl"

TARGET_PATTERN = r"(?i)^perl-.*\.spec$"
CONTENT_PATTERN = r"(?i)(?:%perl_make_install|%perl_process_packlist|%perl_gen_filelist|%\{perl_vendorlib\}|%\{perl_vendorarch\}|%\{perl_requires\})"
PROMPT_PATTERN = r"(?i)(?:perl\s+packag|cpan\s+spec|cpanspec|perl\s+module\s+rpm)"


OLLAMA_SPEC_PROMPT = """
You are an expert in Perl RPM packaging for openSUSE. Follow the openSUSE Perl packaging guidelines from https://en.opensuse.org/openSUSE:Packaging_Perl

## Naming convention

Convert Perl module names with `::` to hyphenated package names:
- `File::LibMagic` → `perl-File-LibMagic`
- `XML::Parser` → `perl-XML-Parser`

## BuildRequires

For openSUSE >= 11.4:
```
BuildRequires:  perl-macros
```

For older releases, add explicitly:
```
BuildRequires:  perl
BuildRequires:  perl-Module-Build  # if using Module::Build
```

## Key macros

| Macro | Purpose |
|---|---|
| `%perl_make_install` | Runs `make install DESTDIR=...` with proper INSTALLDIRS=vendor |
| `%perl_process_packlist` | Removes `.packlist` files from buildroot (they trigger auto-dir warnings) |
| `%perl_gen_filelist` | Generates `%{name}.files` for use with `%files -f` |
| `%perl_requires` | Sets `Requires:` for the Perl version the package was built against |
| `%{perl_vendorlib}` | `/usr/lib/perl5/vendor_perl/5.xx.x` — arch-independent Perl modules |
| `%{perl_vendorarch}` | `/usr/lib64/perl5/vendor_perl/5.xx.x` — arch-specific Perl modules (XS) |

## Spec template

```
Name:           perl-File-LibMagic
Version:        1.23
Release:        0
Summary:        Determine file type with libmagic
License:        Artistic-1.0 OR GPL-1.0-or-later
Group:          Development/Libraries/Perl
BuildRequires:  perl-macros
BuildRequires:  perl(ExtUtils::MakeMaker)
BuildRequires:  file-devel
Requires:       file
Source0:        https://cpan.metacpan.org/modules/by-module/File/File-LibMagic-%{version}.tar.gz

%description
File::LibMagic is a Perl interface to libmagic for determining file types.

%prep
%autosetup -p1 -n File-LibMagic-%{version}

%build
perl Makefile.PL INSTALLDIRS=vendor
%make_build

%install
%perl_make_install
%perl_process_packlist
%perl_gen_filelist

%check
%make_build test

%files -f %{name}.files
%license LICENSE
%doc README Changes

%changelog
```

## Arch-specific vs noarch

If the module is pure Perl (no XS/C code), add:
```
BuildArch:      noarch
```

If the module uses XS (C extensions), DO NOT set noarch and instead use `%{perl_vendorarch}`.

## Tools

- `cpanspec` — generates spec files from CPAN module names
- `cpan2dist` — alternative spec generator (CPANPLUS distribution)

Usage:
```
cpanspec --from https://www.cpan.org --packager "Your Name <email>" File::LibMagic
```

## Key rules

- Always call `%perl_process_packlist` after %perl_make_install to avoid "auto directory included" errors
- Always call `%perl_gen_filelist` for automatic file list generation
- Use `%files -f %{name}.files` to use generated file lists
- For pure Perl modules, set `BuildArch: noarch`
- License for most CPAN modules is `Artistic-1.0 OR GPL-1.0-or-later`
- Always include `%license` and `%doc` files explicitly (they may not be in the generated file list)
- Use `%perl_requires` since openSUSE 11.4 for automatic Perl version dependencies
- Remove `INSTALLDIRS=vendor` from Makefile.PL arguments if the module hard-codes it
"""

OLLAMA_ERROR_PROMPT = """
You are debugging a Perl RPM build failure for openSUSE.

## Common Perl build errors

### auto directory included
```
error: auto directory included in perl package
```
→ Make sure `%perl_process_packlist` is called in %install.
  This macro removes `.packlist` files that cause this error.

### Missing perl-macros
```
%perl_make_install: command not found
```
→ Add `BuildRequires: perl-macros`. This package provides the Perl RPM macros.

### Linking failures with --as-needed
```
/usr/lib64/gcc/.../ld: warning: --as-needed ignored
```
→ Some Perl XS modules fail to link properly with `--as-needed` (SUSE default).
  Workaround:
  ```
  export SUSE_ASNEEDED=0
  ```
  Add this before %build or %install as needed.

### Unrecognized argument in Makefile.PL
```
Unknown option: INSTALLDIRS=vendor
```
→ Some older Makefile.PL scripts don't support INSTALLDIRS.
  Remove it from the arguments:
  ```
  perl Makefile.PL
  ```
  The macros will handle the install directory correctly.

### Test failures
```
t/foo.t ......................... FAIL
```
→ Test failures can often be ignored if they are environment-specific.
  Add to %check:
  ```
  %make_build test || :
  ```
  Or remove %check entirely. Only do this if tests fail due to the build environment,
  not due to actual bugs.

### Missing BuildRequires
```
Can't locate Foo/Bar.pm in @INC
```
→ Add `BuildRequires: perl(Foo::Bar)` for the missing module.

## Investigating interactively

If you are unsure about the root cause and need to investigate inside the build environment, include [DEEP_ANALYZE] in your response.

When inside the build environment:
- Check Perl version: `perl -v`
- List installed Perl modules: `cpan -l 2>/dev/null || perldoc perllocal`
- Check @INC: `perl -e 'print join("\\n", @INC)'`
- Test module loading: `perl -MFoo::Bar -e 'print "ok\\n"'`
- Check for missing .packlist: `find /usr/lib/perl5 -name '.packlist' 2>/dev/null`
"""

DEEP_ANALYZE_PROMPT = """
## Interactive Perl investigation

You are inside the build environment. Investigate Perl packaging issues:

1. **Check Perl version**: `perl -v`
2. **Check @INC paths**: `perl -e 'print join("\\n", @INC)'`
3. **Test module loading**: `perl -MModule::Name -e 'print "ok\\n"' 2>&1`
4. **Check for unpackaged files**: `find %{buildroot} -type f 2>/dev/null | head -30`
5. **Check packlist files**: `find %{buildroot} -name '.packlist' 2>/dev/null`
6. **List installed XS modules**: `find %{buildroot}%{perl_vendorarch} -name '*.so' 2>/dev/null`
7. **Verify Makefile.PL arguments**: `perl Makefile.PL --help 2>/dev/null | head -20`
"""


def fix_content(content: str) -> str:
    lines = content.split('\n')
    changed = False

    has_perl_vendorlib = any('%{perl_vendorlib}' in l for l in lines)
    has_noarch = any('BuildArch' in l and 'noarch' in l for l in lines)

    if has_perl_vendorlib and not has_noarch:
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

    has_perl_make_install = any('%perl_make_install' in l for l in lines)
    has_filelist = any('%perl_gen_filelist' in l for l in lines)

    if has_perl_make_install and not has_filelist:
        new_lines = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if not inserted and line.strip().startswith('%perl_make_install'):
                new_lines.append('%perl_gen_filelist')
                inserted = True
                changed = True
        if inserted:
            content = '\n'.join(new_lines)
            lines = content.split('\n')

    has_packlist = any('%perl_process_packlist' in l for l in lines)

    if has_perl_make_install and not has_packlist:
        new_lines = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if not inserted and line.strip().startswith('%perl_make_install'):
                new_lines.append('%perl_process_packlist')
                inserted = True
                changed = True
        if inserted:
            content = '\n'.join(new_lines)
            lines = content.split('\n')

    return content
