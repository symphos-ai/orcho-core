# ADR 0185 — Workspace-stored MCP server identity

- Status: Accepted
- Date: 2026-08-28
- Relates to: ADR 0184 (workspace-init decision surface)

## Context

`orcho workspace init` resolves an MCP server identity — a server name and a
launcher command — and `orcho workspace mcp` renders the full client setup for
it. Before this ADR the identity lived only in init's stdout: reproducing the
setup required replaying `--workspace`, `--mcp-server-name`, and
`--orcho-mcp-command` explicitly, and a bare `orcho workspace mcp` re-derived
the name from the current directory, which can differ from what init chose.
The workspace already persists per-workspace state in
`.orcho/config.local.json`, but had no owner for the MCP identity.

## Decision

`workspace init` persists the resolved identity in the workspace-local config
under one key:

```json
{ "mcp": { "server_name": "orcho-<slug>", "command": "orcho-mcp" } }
```

The write is additive and non-destructive: it never touches other keys, and a
missing, unreadable, or non-object config file leaves init successful with the
identity simply not stored. Identity resolution at init is: explicit
`--mcp-server-name` (or a name derived from an explicit `--workspace-name`),
else the stored identity, else the derived default — so a repeat init without
flags does not silently rename the server. The launcher command resolves as
explicit flag, else stored value, else `orcho-mcp`.

`orcho workspace mcp` stays read-only. With no flags it resolves the active
workspace as before and then prefers the stored identity (personal
`config.local.json` before shared `config.json`, matching the config layering)
over the cwd-derived default. Flags remain overrides.

Init's summary renders the bare pointer `Full setup: orcho workspace mcp`
when the identity was stored, and falls back to the explicit-flag replay line
(for example on `--dry-run`) when it was not. `WorkspaceInitResult` carries
this as `mcp_identity_stored`.

## Consequences

- A bare `orcho workspace mcp` from the project or group directory reproduces
  init's client setup without any flags.
- The stored identity is a convenience record: deleting the key only reverts
  name derivation to the contextual default; nothing else depends on it.
- Scripted invocations that pass all three flags behave exactly as before.
