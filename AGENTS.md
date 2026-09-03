# pbuild-ai Agent Rules

## Permission & Safety
- **Copyright headers are sacred.** Never modify, reword, or remove the copyright header block (leading `#` lines) in `.spec` files. The tool enforces this at the write_file level — any attempt is silently reversed.
- **Workspace sandbox is absolute.** All file reads and writes are confined to the workspace directory. Paths outside the workspace are rejected. Writing to `tool-scripts/` via write_file is explicitly blocked.
- **Only safe HTTPS URLs allowed.** file:// and private/local IP addresses are blocked by `is_safe_url()`.
- **Git push is forbidden.** Only local git operations (clone, add, diff, log, submodule, status) are permitted. Push is blocked at the tool level.
- **tool-scripts execution requires `--allow-tool-scripts`.** Without this flag scripts are not run silently: with `--interactive` the user is asked once per script name to grant execution for the rest of the process, otherwise the call is refused and reported to the AI. Scripts are looked up in `tool-scripts/`, then `skills/`, then `.agents/skills/`; a workspace-relative path (e.g. `.agents/skills/post-update.sh`) is also accepted. Missing directories are silently skipped.
- **The AI can abort a run** by responding with a line containing `[ABORT: reason]`. This happens when AGENTS.md mandates a step that cannot be performed. pbuild-ai then stops with exit code 1 instead of continuing into the build phase.
- **remove_file / rename_file** operate inside the workspace sandbox, same as read_file/write_file. `rename_file` creates parent directories if needed. `rename_file` refuses if destination already exists.

## Exit Values

| Code | Meaning |
|------|---------|
| 0 | Success — all requested operations completed. |
| 1 | Fix failure — build errors could not be resolved after exhausting all fix attempts, no changes were made when changes were required, or the last build attempt failed. |
| 2 | Internal error — AI API returned an error (HTTP 4xx/5xx, schema rejection, connection failure), or an unexpected exception occurred in pbuild-ai itself. |

