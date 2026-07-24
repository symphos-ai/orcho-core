# ADR 0155 — Workspace shared and personal config layers

- **Status:** Accepted
- **Date:** 2026-07-24
- **Related:** [ADR 0062](0062-workspace-config-git-dir.md)

## Context

Workspace configuration previously had one workspace-local JSON file,
`$ORCHO_WORKSPACE/.orcho/config.local.json`. It was suitable for personal
preferences and direct project-alias readers, but its `.local` name made it a
poor place for team policy that should be reviewed and committed. Teams need a
shared layer without changing package or user configuration semantics.

## Decision

Add `$ORCHO_WORKSPACE/.orcho/config.json` as a shared workspace layer. The
complete precedence order, from lowest to highest, is package
`config.local.json`, user `config.local.json`, workspace `config.json`,
workspace `config.local.json`, then environment variables. Package and user
scopes do not gain a `config.json` candidate.

`config.json` holds committable team policy; `config.local.json` holds
gitignored personal overrides and wins over it. This is naming parity with the
common `settings.json` / `settings.local.json` pair. Workspace init scaffolds a
neutral comment-only shared file and an `.orcho/.gitignore` that ignores only
the personal filename.

The shared layer participates in existing partial phase overlays and
`profiles_v2` overlays. `profile customize` remains a personal-file writer;
team overlays are authored directly in the shared file.

## Consequences

- Teams can commit workspace policy without committing individual preferences.
- Existing package/user precedence and JSON merge semantics remain unchanged.
- The personal workspace file retains its current concrete scaffold and project
  alias / `git_dir` examples under ADR 0062.
- There is no migration of direct project-map readers in this decision: they
  remain personal-file readers. There is also no MCP or SDK wire-shape change.

## Alternatives considered

1. **Keep only `config.local.json`.** Rejected because it cannot clearly
   express committable team policy.
2. **Add `config.json` at package and user scope.** Rejected because it would
   change established scope semantics and introduce ambiguous ownership.
3. **Migrate project aliases and `git_dir` to the shared file.** Rejected as
   outside this layering decision and inconsistent with ADR 0062's current
   direct-reader contract.
4. **Make `profile customize` write shared config.** Rejected because the CLI
   writer is intentionally personal; shared policy requires explicit review.
