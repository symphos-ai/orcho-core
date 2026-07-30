# ADR 0170 — Public SDK workspace cleanup

- **Status:** Accepted
- **Date:** 2026-07-30
- **Extends:** [ADR 0021](0021-public-sdk-boundary.md), [ADR 0167](0167-retention-enforcement-and-workspace-cleanup.md), and [ADR 0169](0169-run-root-retention-and-pause-expiry.md)

## Decision

`sdk.cleanup` is the typed public boundary for workspace cleanup.
`report_workspace_cleanup` is read-only: it resolves the ordinary SDK reader
context and projects the engine selection into frozen, slotted counts and
deterministically ordered reason summaries. It creates no receipts, archives,
metadata updates, or checkout mutations.

`reclaim_workspace_cleanup` is explicitly side-effecting. Callers must provide
both a cleanup tier and a disposition. The SDK delegates cutoff defaults,
selection, re-verification, execution, byte accounting, and archive policy to
the engine; it copies final durable receipt facts into a frozen, slotted result.
The engine recomputes selection immediately before mutation, so a preceding
report is never an executable plan.

MCP is explicitly out of scope: this additive Python SDK surface creates no MCP
tool, resource, schema, or wire contract.

## Consequences

CLI and other consumers can render the same counts and reason summaries without
importing engine cleanup types. Engine retention semantics, tiers, dispositions,
receipt layout, and archive routing remain unchanged.
