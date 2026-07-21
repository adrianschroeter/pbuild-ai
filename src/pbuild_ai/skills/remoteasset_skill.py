# Activate when the user's prompt mentions obs_scm / RemoteAsset conversion
PROMPT_PATTERN = r"(?i)(convert|replace|migrate).*(obs_scm|service|remote.?asset)"

# Also activate if the _service file content is visible in the spec
CONTENT_PATTERN = r"obs_scm"

OLLAMA_SPEC_PROMPT = """
You are converting an openSUSE package from `obs_scm` source service to the RemoteAsset mechanism.

## What to do

Read the '_service' file for getting the URL, version and upstream tag.

Remove the `_service` file entirely and add hint lines for pbuild right before the `Source:` line 
in the spec file. Add `#!RemoteAsset: URL` and `#!CreateArchive` prefix hint lines to activate it.
These must be an own line each.

## Example conversion

### Before (_service file)
```
<services>
  <service name="obs_scm" mode="manual">
    <param name="url">https://github.com/owner/repo.git</param>
    <param name="scm">git</param>
    <param name="revision">v1.0.0</param>
    <param name="version">1.0.0</param>
  </service>
  <service name="set_version" mode="manual"/>
  <service name="tar" mode="buildtime"/>
  <service name="recompress" mode="buildtime">
    <param name="file">*.tar</param>
    <param name="compression">xz</param>
  </service>
</services>
```

### Before (spec file)
```
Source:        %{name}-%{version}.tar.gz
```

### After (spec file)
```
#!RemoteAsset: git+https://github.com/owner/repo#v%{version}
#!CreateArchive
Source:        %{name}-%{version}.tar.xz
```

## Rules
- The `#!RemoteAsset: URL` line must be added and contain the actual download URL with the git+ prefix
  like git+https://github.com/openSUSE/build#20260623 The fragment part points to a hash, branch or tag..
- The `#!CreateArchive` line must be added immediately follow `#!RemoteAsset`.
- The '#!' prefix is a hint for the build tool to process these lines.
- The `Source:` defines the tar ball to be created now.
- All three statements must be in an own line.
- If there is no `_service` file, just add the two prefix lines to the `Source:` line.
- Avoid any other change.

Make sure that you remove also the belonging tar, recompress and set_version services when 
removing the obs_scm service.

Make the changes using write_file. Write both the spec file and the _service file if needed.
The _service can get removed entirely if no other services are inside.
"""
