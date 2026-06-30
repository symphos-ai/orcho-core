# ADR 0062 — git_dir lives in workspace config, not plugin.py

**Status:** Accepted
**Date:** 2026-06-01

## Context

`PluginConfig.git_dir` (introduced with the worktree foundation, ADR 0033) let plugin authors
declare that the actual git repository root was a subdirectory of the
project folder (monorepo / nested git layout). Two runtime readers consumed
this field:

1. `resolve_git_root()` in `pipeline/engine/run_diff.py` — for diff capture.
2. The effective `git_root` line in `pipeline/project/app.py` — for
   pre-run-dirty checking and worktree isolation.

This worked for simple cases but had several problems:

- `PluginConfig` is a **plugin-authored file** inside the project tree.
  Orcho already owns the workspace config (`$ORCHO_WORKSPACE/.orcho/config.local.json`);
  putting git topology there is more consistent.
- Users registering a new project via `orcho workspace init` have no way
  to declare a nested git_dir without creating a `plugin.py` first.
- The field made it impossible to register a nested-git project purely
  through workspace tooling.

## Decision

**Delete `PluginConfig.git_dir`** and move the concept exclusively into the
workspace config projects map. The entry shape becomes:

```json
{
  "projects": {
    "my-mono":  "/path/to/my-mono",
    "my-nested": {"path": "/path/to/my-nested", "git_dir": "SubProject"}
  }
}
```

- Plain string entries (no `git_dir`) remain the common form.
- Object entries are written only when `git_dir` is non-empty.
- Both shapes are parsed by `load_workspace_project_aliases` and by the
  new `load_workspace_project_git_dir(project_path)` accessor.

Both runtime readers switch to `load_workspace_project_git_dir` in the
**same commit** to prevent the partial-migration failure mode where diff
works but worktree isolation hard-fails.

`orcho workspace init` gains interactive discovery: for child folders that
were not auto-detected (no root marker), the CLI asks the user whether to
register them. When yes, it scans for nested `.git` dirs/files and records
the shallowest one as `git_dir` in the config.

## Consequences

- `PluginConfig.git_dir` is removed with no backcompat path (per
  `orcho-core/AGENTS.md` "No Backcompat Ceremony").
  Existing `plugin.py` files that set `git_dir` will emit an "unknown key"
  warning (existing normalising-validator behaviour) and the field will be
  silently dropped; the workspace config becomes the authoritative source.
- `orcho-web` and `orcho-mcp` do not read `config.local.json` projects map
  directly; no cross-repo contract change is required for MCP wire format.
- `sdk.init_workspace` remains non-interactive; all I/O lives in the CLI.
- The `--no-interactive` flag (or absent TTY) skips discovery prompts and
  prints a hint instead.

## Alternatives considered

- **Keep `plugin.git_dir` as a fallback** — rejected because it creates a
  dual-source-of-truth and complicates future workspace-first tooling.
- **Add a CLI flag `--git-dir` to `workspace init`** — too narrow; doesn't
  help for multi-project groups or repeated inits.
