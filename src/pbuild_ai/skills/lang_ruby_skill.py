# lang_ruby_skill.py
### WARNING: Currently not reviewed and adapted to git based packaging!
### Based on https://en.opensuse.org/openSUSE:Packaging_Ruby

SKILL_NAME = "lang_ruby"

VERSION_API = {
    "url": "https://rubygems.org/api/v1/gems/{name}.json",
    "version_key": "version",
    "name_regex": r"^rubygem-",
}

TARGET_PATTERN = r"(?i)^(?:rubygem-|ruby-).*\.spec$"
CONTENT_PATTERN = r"(?i)(?:%gem_install|gem2rpm|rubygem|%\{gem_|gem\s+install)"
PROMPT_PATTERN = r"(?i)(?:ruby\s+(?:gem|packag)|rubygem|gem2rpm|\bgem\b.*spec)"

OLLAMA_SPEC_PROMPT = """
You are an expert in Ruby RPM packaging for openSUSE. Follow the openSUSE Ruby packaging guidelines from https://en.opensuse.org/openSUSE:Packaging_Ruby

## Naming convention

- Package name: `rubygem-{gemname}`
- Example: `rubygem-foo` for the `foo` Ruby gem

## Tooling

Use `gem2rpm` to generate a spec stub:

```
gem2rpm -o rubygem-foo.spec --fetch foo
```

For updates, use the gem file directly:

```
gem2rpm *.gem -o *.spec
```

On Tumbleweed, install gem2rpm via:

```
zypper in rubygem(gem2rpm)
```

Make sure to use system Ruby before running gem2rpm:

```
rvm use system
```

## BuildRequires

`gem2rpm` deliberately omits BuildRequires to reduce build latency. You may need to add:

- `BuildRequires: ruby-devel` — required for gems with binary (C) extensions
- `ruby-common` provides all necessary Ruby macros
- Automatic Requires/Provides are generated via `rubygems.attr`

## Spec template

```
%install
%gem_install -f
```

The `%gem_install` macro handles everything: builds, installs, and sets up the correct paths.

## Provides and Requires

Ruby gem dependencies are expressed as:

```
Requires: rubygem(1.9.1:gem-name)
Requires: rubygem(1.9.1:gem-name:version)
Provides: rubygem(1.9.1:gem-name) = version
```

The version suffix for backward compatibility follows the pattern `gem-name-0_6`.

## Key rules

- Use `%gem_install -f` in `%install` — do NOT manually call `gem install`
- `ruby-common` provides all RPM macros for Ruby
- Binary gems (those that compile C extensions) need `BuildRequires: ruby-devel`
- gem2rpm omits BuildRequires deliberately — add them based on gem dependencies
- Automatic dependency resolution via `rubygems.attr` handles most Requires/Provides
"""

OLLAMA_ERROR_PROMPT = """
You are debugging a Ruby RPM build failure for openSUSE.

## Common Ruby build errors

### ruby.h is missing
```
fatal error: ruby.h: No such file or directory
```
→ The gem has a binary (C) extension. Add `BuildRequires: ruby-devel`.

### Gem install failure due to missing deps
```
ERROR: While executing gem ... (Gem::DependencyError)
    Unable to resolve dependency: user requested 'foo (= 1.0)'
```
→ A required gem is not installed. Find the corresponding rubygem package and add `BuildRequires: rubygem(1.9.1:foo)`.

### Old gem packaging macros not working
```
error: %gem_install: unknown macro
```
→ The spec was written for an old Ruby packaging style. Update to use `%gem_install` from `ruby-common`. Ensure `ruby-common` is available.

### No such file to load
```
Gem::LoadError: Unable to activate rubygem-foo-1.0, because rubygem-bar-0.9 conflicts with bar >= 1.0
```
→ Version conflict — the spec needs an updated version requirement. Check gem dependencies and update BuildRequires/Requires accordingly.

## Investigating interactively

If you are unsure about the root cause, include [DEEP_ANALYZE] in your response.

When inside the build environment:
- Check gem dependencies: `gem dependency %{name}`
- List installed gems: `gem list --local`
- Check Ruby version: `ruby --version`
- Try manual gem install: `gem install --local path/to/gem.gem`
- Check for C extension: `ls ext/` or check if the gemspec has `extensions`
"""


def fix_content(content: str) -> str:
    """Pre-build spec content fixes for common Ruby packaging issues."""
    lines = content.split('\n')
    changed = False

    # 1. Add BuildRequires: ruby-devel if binary extension detected
    has_binary_tell = any('extconf' in l or 'mkmf' in l or '.so' in l or '.bundle' in l for l in lines)
    has_ruby_devel = any('ruby-devel' in l and 'BuildRequires' in l for l in lines)

    if has_binary_tell and not has_ruby_devel:
        new_lines = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if not inserted and line.strip().startswith('BuildRequires'):
                new_lines.append('BuildRequires:  ruby-devel')
                inserted = True
                changed = True
        if inserted:
            content = '\n'.join(new_lines)

    return content
