# pbuild-ai Agent Rules

## Permission & Safety
- **Copyright headers are sacred.** Never modify, reword, or remove the copyright header block (leading `#` lines) in `.spec` files. The tool enforces this at the write_file level — any attempt is silently reversed.
- **Workspace sandbox is absolute.** All file reads and writes are confined to the workspace directory. Paths outside the workspace are rejected. Writing to `tool-scripts/` via write_file is explicitly blocked.
- **Only safe HTTPS URLs allowed.** file:// and private/local IP addresses are blocked by `is_safe_url()`.
- **Git push is forbidden.** Only local git operations (clone, add, diff, log, submodule, status) are permitted. Push is blocked at the tool level.
- **tool-scripts execution requires `--allow-tool-scripts`.** Without this flag, tool-scripts/ is never executed. If the directory doesn't exist, it's silently skipped.

