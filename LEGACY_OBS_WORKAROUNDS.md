# Using pbuild-ai with OBS Legacy Sources

pbuild-ai itself is agnostic about how sources are stored.

However, some mechanics it applies in spec files do not work out of the box with legacy sources.
Projects (a set of packages) are also harder to handle without git.

These are recommended workarounds for sources not managed by git.

## Remote Assets

pbuild-ai may convert obs_scm services to remote assets. However, remote assets are **not** enabled by default on non-git sources.

To enable them, add a `_service` file with the following content:

```xml
<services>
  <service name="download_assets" />
</services>
```

## Directories

git repositories may contain directories and pbuild-ai may create them.

OBS Legacy needs to store them as obscpio archive. To create it, just call

  # osc add DIRECTORY

and add the resulting DIRECTORY.obscpio file.

Also keep in mind to refresh the archive when files get changed inside.
At build time it will appear as directory again.

## openSUSE Policy

openSUSE Policy currently requires you to include the referenced source file; you cannot rely solely on the OBS instance to fetch it at build time.

You need to manually run the service and commit the tarball with the sources:

```bash
osc service run download_assets
osc add <downloaded tarball>
osc commit
```

## Building in a Project

A checkout of legacy projects does not specify how to do a standalone build using pbuild, nor does it define itself as a project.

You need to create two files manually:

### `_manifest` — to identify it as a project:

```yaml
subdirectories:
- .
```

### `_pbuild` — to point pbuild to online OBS repositories:

```xml
<pbuild>
  <preset name="tumbleweed" default>
    <config>tumbleweed</config>
    <repo>https://download.opensuse.org/repositories/YOUR_PROJECT_PATH/openSUSE_Tumbleweed</repo>
    <repo>config:</repo>
    <arch>x86_64</arch>
  </preset>
</pbuild>
```

## Project Status

After running e.g. `--fix`, `--update`, or `--update-only`, some package sources may have been modified. You need to know which ones.

With git you can easily check which package sources have been modified using e.g. `git status`. OBS, however, has no record of the package set state.

The fastest way to find modified source packages is:

```bash
for i in *; do echo "$i"; cd "$i" && osc status > .status; cd -; done
wc -l */.status | grep -v " 0 "
```
