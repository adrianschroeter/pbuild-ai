pbuild-ai is an AI helper for the [pbuild](https://github.com/openSUSE/pbuild) tool.
It works on single packages or entire package sets and is designed to
work with all package formats and AI models.

The main functionality is either:

* Analyze the source (`--analyze`; this is the default when no other option is given)
* Modify sources via a natural-language prompt (`--modify`)
* Run builds and fix possible build failures (`--fix`)
* Update sources to current upstream version and verify (`--update`)
* Generate a new package from scratch (`--generate`)

pbuild-ai is safe by default — it only allows modifications to local
package sources.

The goal is a fully automated tool to update, fix, or prepare local
package sources. It is designed around git-based projects or single
packages, but pbuild-ai does not depend on git. However, git is useful
for reviewing and exchanging changes.

## Notes

It intentionally does not support pushing sources or creating pull
requests. The user is expected to review local changes before requesting
reviews from others.

Since the tool could run indefinitely, it is recommended to use AI
servers with a fixed-cost plan — ideally a local instance on your own
hardware.

## Single package

Create a simple analysis of a package source with no modifications.
The only required argument is the directory to work in:

```bash
git clone https://src.opensuse.org/pool/bc
pbuild-ai bc
```

Test-build a package and attempt to fix failures automatically.
Tumbleweed is the default build target; use `--dist` to change it:

```bash
git clone https://src.opensuse.org/pool/bc
pbuild-ai --fix bc
```

Update to the latest upstream version, test-build, and fix:

```bash
git clone https://src.opensuse.org/pool/bc
pbuild-ai --update bc
```

### `--analyze`

Explicitly analyze spec files and exit (same behavior as running without any
flags). No build is performed. Conflicts with `--fix`, `--update`, `--generate`,
`--changelog`, and `--modify`.

### `--changelog`

Prepend a changelog entry for the current version in the `.changes` file, then
exit. No build or research is performed. Conflicts with `--fix` and `--analyze`.

### `--dist DISTRIBUTION_CONFIG`

Controls the build target distribution (e.g., `tumbleweed`, `leap-16.1`).
The value is passed through to pbuild and determines which repository
set is used for dependency resolution. Defaults to `tumbleweed` if unset.

## Package set (project mode)

When your working directory contains a `_manifest` file, it is parsed
to determine the set of packages and their locations. The directory
should also have a `_pbuild` file to define the repositories to build
against.

A common scenario is referencing the same git repository that is also
built inside an OBS project. The published binaries from OBS can be
used for local building so not every package needs to be rebuilt from
scratch. A fully self-contained local build is also supported.

pbuild-ai can be called without extra options, or with `--fix` or `--update`.
Pass a package name as an optional second argument to target a single
package inside the project:

```bash
pbuild-ai --fix bc
pbuild-ai --fix bc --modify "disable network-dependent tests"
```


## AGENTS.md

Your package or project may contain an `AGENTS.md` file to give hints on
how to process or update. Packages listed there under a "skip" rule are
excluded from the build loop.

```text
skip: package-a, package-b, package-c
```

Lines matching `skip\s*:` (case-insensitive) are parsed for a
comma-separated list of package names to exclude from building. This is
useful when a project contains auxiliary packages that do not need
local testing.

## `_pbuild` file format

When working on a package set (project mode), a `_pbuild` file in the
working directory defines which repositories to build against. It follows
the standard pbuild format:

```text
repo oss https://download.opensuse.org/repositories/my_project/openSUSE_Factory oss
repo update https://download.opensuse.org/update/openSUSE-Factory oss
```

Each line specifies a repository alias, a source URL, and an architecture.
This is required for dependency resolution when building packages
outside of OBS.

## Tool calling & Security

pbuild-ai enforces a defined set of rules that only allow local
modifications of the package sources. By default, it only calls tools
that are safe for your system. Reading any file outside the working
directory is blocked.

Running inside a VM is not required but adds an extra layer of safety.

You can extend the tool set by placing scripts in a `tool-scripts/`
directory. To enable them, pass `--allow-tool-scripts`. pbuild-ai does
not allow modifying scripts inside this directory.

## Advanced options

### `--deep-analyze`

Opens an interactive investigation session inside the build
environment right away. pbuild-ai can execute any command inside it to diagnose
failures. If a fix is applied, the changes are exported as a patch file
and the spec file is updated to include it.

This mode may get enabled automatically when the AI is unsure about
the cause of an error.

### `--modify PROMPT`

Applies source changes based on the given prompt without running a test
build. Shows the diff and exits. Combine with `--fix` to also build and
verify after the modification.

### `--fix`

Test-build a package and attempt to fix failures automatically. Requires Ollama
to diagnose build failures and apply source changes.
Conflicts with `--analyze` and `--changelog`.

### `--fix --prompt PROMPT`

Runs a test build, and if it fails, passes your hint to Ollama to guide
the fix towards your preferred solution.

### `--prompt HINT`

Used alongside `--fix` or `--modify`. Provides a general hint that is
preferred over Ollama's generic analysis when making source changes.

### `--ollama-server URL`

Sets the Ollama server URL. Overrides the `OLLAMA_HOST` environment
variable. Defaults to `http://localhost:11434`.

### `--model NAME`

Sets the Ollama model name. Overrides the `OLLAMA_MODEL` environment
variable. Defaults to `gemma4`.

### `--generate PROMPT`

Creates a new openSUSE RPM package from scratch. The given prompt
describes what to package. pbuild-ai researches upstream sources via
web fetch, asks clarifying questions, and writes spec files and
supporting files into the workspace directory. No build is performed
after generation.

