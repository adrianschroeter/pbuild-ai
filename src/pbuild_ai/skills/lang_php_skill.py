# lang_php_skill.py
### WARNING: Currently not reviewed and adapted to git based packaging!
### Based on https://en.opensuse.org/openSUSE:Packaging_PHP

SKILL_NAME = "lang_php"

TARGET_PATTERN = r"(?i)^php.*\.spec$"
CONTENT_PATTERN = r"(?i)(?:%\{__php\}|%\{__phpize\}|%\{__php_config\}|%\{php_version\}|%\{pear_phpdir\}|%\{pear_docdir\}|%\{pear_testdir\}|%\{php_pearxmldir\}|%\{__pear\}|pear)"
PROMPT_PATTERN = r"(?i)(?:php\s+packag|pear\s+channel|composer|phpize|php-config)"

OLLAMA_SPEC_PROMPT = """
You are an expert in PHP RPM packaging for openSUSE. Follow the openSUSE PHP packaging guidelines from https://en.opensuse.org/openSUSE:Packaging_PHP

## PHP macros

| Macro | Purpose |
|---|---|
| `%{__php}` | PHP interpreter binary |
| `%{__phpize}` | phpize script for building PHP extensions |
| `%{__php_config}` | php-config script for extension flags |
| `%{php_version}` | PHP version string (e.g., 7.4, 8.0) |

## PEAR macros

| Macro | Purpose |
|---|---|
| `%{__pear}` | PEAR installer binary |
| `%{pear_phpdir}` | PEAR PHP library directory |
| `%{pear_docdir}` | PEAR documentation directory |
| `%{pear_testdir}` | PEAR test directory |
| `%{php_pearxmldir}` | PEAR XML metadata directory |

## Naming convention

### PEAR channels

Pattern: `php-pear-channel-{channel-name}`

```
Name:           php-pear-channel-doctrine
Summary:        PEAR channel for Doctrine
Provides:       php-pear-channel(doctrine-project.org)
```

### PEAR packages

Pattern: `php-pear-{package-name}`

```
Name:           php-pear-DoctrineORM
Summary:        Doctrine ORM for PHP
BuildArch:      noarch
```

## Spec template for PEAR packages

```
Name:           php-pear-DoctrineORM
Version:        2.7.1
Release:        0
Summary:        Doctrine ORM for PHP
License:        MIT
Group:          Development/Libraries/PHP
URL:            https://www.doctrine-project.org/
Source0:        DoctrineORM-%{version}.tgz
BuildArch:      noarch

BuildRequires:  php-devel
BuildRequires:  php-pear
Requires:       php-pear
Requires:       php-channel(doctrine-project.org)
Provides:       php-pear(doctrine-project.org/DoctrineORM)

%description
Doctrine ORM for PHP.

%prep
%setup -q -n DoctrineORM-%{version}

%build
# Nothing to build for PEAR packages

%install
%{__pear} install --nodeps --offline --packagingroot %{buildroot} \
    %{_datadir}/php/pearxml/%(echo %{name} | sed 's/php-pear-//').xml

%post
%{__pear} channel-discover doctrine-project.org 2>/dev/null || :
%{__pear} install --nodeps --offline --packagingroot / \
    %{_datadir}/php/pearxml/DoctrineORM.xml 2>/dev/null || :

%postun
if [ $1 -eq 0 ]; then
    %{__pear} uninstall doctrine-project.org/DoctrineORM 2>/dev/null || :
fi

%files
%defattr(-,root,root)
%{pear_phpdir}/Doctrine/
%{pear_docdir}/DoctrineORM/
%{pear_testdir}/DoctrineORM/
%{php_pearxmldir}/DoctrineORM.xml
```

## Migration from php5/php7 prefix

If the package previously used `php5-` or `php7-` prefix, add Obsoletes:

```
Obsoletes:      php5-DoctrineORM < %{version}
Obsoletes:      php7-DoctrineORM < %{version}
```

## PHP extensions (not PEAR)

For PHP extensions built with phpize:

```
Name:           php8-apcu
Version:        5.1.21
Release:        0
Summary:        APC User Cache for PHP
License:        Apache-2.0
Group:          Development/Libraries/PHP
URL:            https://pecl.php.net/package/apcu
Source0:        https://pecl.php.net/get/apcu-%{version}.tgz

BuildRequires:  php8-devel
BuildRequires:  php8-pear
BuildRequires:  pkgconf-pkg-config

%prep
%setup -q -n apcu-%{version}

%build
%{__phpize}
%configure
%make_build

%install
%make_install

%files
%{php_extdir}/apcu.so
```

## Applications using /usr/share/php/{Vendor}

For applications that install to `/usr/share/php/{Vendor}/`:

```
%files
%dir /usr/share/php/Vendor/
/usr/share/php/Vendor/*
```

## composer.json

If the package ships a `composer.json`, include it as `%doc`:

```
%files
%doc composer.json
```

## PHPUnit testing

For packages that include PHPUnit tests:

```
BuildRequires:  php-phpunit
%check
phpunit
```

## Key rules

- PEAR packages MUST be `BuildArch: noarch`
- PEAR channels require `%post` and `%postun` scriptlets for register/unregister
- Use `Provides: php-pear(channel/package)` for PEAR packages
- Use `Requires: php-pear` for PEAR-based packages
- Use `Requires: php-channel(channel-name)` for channel dependencies
- PHP version-specific packages (extensions) use `phpMAJOR-` prefix (e.g., `php8-`)
- Always include `BuildRequires: php-devel` when using phpize or php-config
"""

OLLAMA_ERROR_PROMPT = """
You are debugging a PHP RPM build failure for openSUSE.

## Common PHP build errors

### Missing php-devel or php-pear
```
/usr/bin/phpize: No such file or directory
```
→ Add `BuildRequires: php-devel` (or `phpMAJOR-devel` for version-specific).

```
/usr/bin/pear: No such file or directory
```
→ Add `BuildRequires: php-pear` (or `phpMAJOR-pear` for version-specific).

### PEAR channel not registered
```
Could not open input file: /usr/share/php/pearxml/Package.xml
```
→ Ensure the channel is registered in `%post`:
  ```
  %{__pear} channel-discover channel-name 2>/dev/null || :
  ```

### PHP version mismatch
```
Warning: PHP Startup: apcu: Unable to initialize module
Module compiled with PHP 7.4, but PHP 8.0 is running
```
→ The extension was built for a different PHP version. Match `BuildRequires: php-devel` to the target PHP version.

### PHPUnit test failures
```
PHPUnit 9.5 required but PHPUnit 8.0 found
```
→ Add or update `BuildRequires: php-phpunit` to the correct version.

### PEAR install fails with "channel-add: channel already exists"
→ This is harmless during `%post` — the `2>/dev/null || :` pattern suppresses errors.

### Missing PEAR XML file
```
error: Package "DoctrineORM.xml" not found
```
→ The PEAR XML file must be installed to `%{php_pearxmldir}`. Ensure `%make_install` or manual install places it there.

## Investigating interactively

If you are unsure about the root cause, include [DEEP_ANALYZE] in your response.

When inside the build environment:
- Check PHP version: `php -v`
- List installed PEAR channels: `%{__pear} channel-list`
- Verify phpize: `which %{__phpize}`
- Check extension dir: `php -i | grep extension_dir`
- List PEAR packages: `%{__pear} list`
- Test PEAR install: `%{__pear} install --nodeps --offline --packagingroot /tmp/test /path/to/package.xml`
"""


def fix_content(content: str) -> str:
    lines = content.split('\n')
    changed = False

    has_pear_macros = any(
        '%{__pear}' in l or '%{pear_phpdir}' in l
        for l in lines
    )
    has_noarch = any('BuildArch' in l and 'noarch' in l for l in lines)
    if has_pear_macros and not has_noarch:
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

    has_pear_in_content = any(
        'pear' in l.lower() for l in lines
    )
    has_php_devel_br = any(
        'php-devel' in l and 'BuildRequires' in l for l in lines
    )
    if has_pear_in_content and not has_php_devel_br:
        new_lines = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if not inserted and line.strip().startswith('BuildRequires'):
                new_lines.append('BuildRequires:  php-devel')
                inserted = True
                changed = True
        if inserted:
            content = '\n'.join(new_lines)
            lines = content.split('\n')

    return content
