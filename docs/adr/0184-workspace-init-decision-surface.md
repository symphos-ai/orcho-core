# ADR 0184 — Workspace-init project-plugin decision surface

- Status: Accepted
- Date: 2026-08-28
- Relates to: ADR 0078 (verification contract env assertions and CLI), ADR
  0163 (existing-project managed workspace onboarding)

## Context

`orcho workspace fine-tune` is a repository inspector. ADR 0078 establishes
that it is pure-read in both modes: it may produce a candidate, but it does not
write a project plugin. That boundary protects a project tree from an inferred
contract being materialised merely because a discovery command ran.

The previous onboarding path created an empty workspace template and then
asked users to carry its contents into each project. That made the write owner
unclear and forced every client to reproduce a full MCP configuration block
from init output.

## Decision

The workspace template remains the single rendered empty `PLUGIN = {}`
authoring surface. It is still created under the workspace scaffold when
scaffolding is enabled.

Interactive `workspace init` owns the only onboarding decision that may write
a project plugin. After successful project discovery and registration, only a
real TTY invocation without `--no-interactive` or `--dry-run` may show one
default-no prompt. A positive answer materialises the fine-tune candidate for
exactly the projects registered by that init. Decline, EOF, Ctrl-C,
non-interactive input, and dry-run all retain generic mode without a project
write. Existing filesystem entries at the destination are skipped, never
replaced.

Fine-tune itself remains pure-read. The materialiser calls it only as an
inspector and is the distinct, explicit write boundary. A materialised
candidate declares a starting set of observed environments and commands; it is
not evidence that selection, schedule, policy, or repair routing has been
approved.

`orcho workspace mcp` is the read-only, reproducible owner of the full MCP
client setup: shell commands, client JSON forms, detected-client markers, and
restart verification. `workspace init` prints only a concise detected-client
summary, any requested `--mcp-config` outcome, and the exact replay command.

This changes no MCP tool, resource, schema, or wire payload. Therefore there
is no MCP companion change for this ADR. The command consumes existing
workspace identity and renders local setup guidance only.

## Consequences

- Users can choose generic mode for a safe first run or explicitly opt in to a
  candidate-backed project plugin without copying a template as a prerequisite.
- The project tree is never changed by unattended onboarding or inspection.
- Existing project configuration remains authoritative and is protected from
  overwrite.
- Complete MCP setup is repeatable on demand and has one production renderer,
  while init remains compact.
