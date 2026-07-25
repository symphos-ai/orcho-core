# ADR 0157 — Cross CLI intent fidelity

- **Status:** Accepted
- **Date:** 2026-07-25
- **Related:** ADR 0148

## Context

The `orcho cross` facade and the direct `orcho-cross` engine CLI use different
phase-routing vocabulary. The facade must not advertise a capability that it
does not forward. Separately, `--mock` is an operator safety decision: a
checkpoint or follow-up resume must not accidentally change a mock run into a
real-provider run when the flag is omitted.

## Decision

Fresh cross runs persist a top-level boolean `mock` in the parent `meta.json`.
On a CLI resume, provider mode is resolved in this order:

1. An explicit `--mock` selects mock mode.
2. Otherwise, a boolean `meta.mock` is inherited.
3. Metadata without `mock` is legacy metadata and uses the historical
   args-driven fallback (real unless `--mock` was supplied), with one warning.

A present non-boolean `meta.mock` is invalid and stops the resume; it is never
coerced with Python truthiness. Inherited mode is resolved before provider and
phase-agent construction. Thus an inherited mock resume remains hermetic and
does not invoke a real provider.

The public `orcho cross` facade exposes only the cross engine capability
subset. It accepts canonical phase flags `--model-implement`,
`--model-repair-changes`, `--model-review-changes` and their `--runtime-*`
counterparts, then adapts them to the direct engine's historical
`--*-build`, `--*-fix`, and `--*-review` aliases. It also forwards `--model`
and exposes `--hypothesis` / `--no-hypothesis`. Plan reuse, attachments, and
the worktree-isolation override remain mono-only `orcho run` capabilities.

## Consequences

- Resuming a mock cross run without repeating `--mock` remains mock; the CLI
  identifies inherited mode in its output.
- Legacy runs retain a defined, visible fallback instead of silent semantic
  drift.
- Parser inventory tests keep advertised facade options synchronized with
  engine argv forwarding.
- This is an additive durable metadata field only. `run.start` events,
  `CrossRunRequest`, SDK/MCP schemas, and engine/MCP capabilities do not
  change.

## Alternatives considered

1. **Treat omitted `--mock` as explicit false.** Rejected: it can turn a
   resumed mock run into a real-provider run.
2. **Coerce persisted values with `bool(value)`.** Rejected: values such as
   `"false"` would select mock mode silently.
3. **Add mono-only flags to the cross engine.** Rejected: it expands engine
   and MCP scope rather than making the facade truthful.
