# ADR 0159 — Cross resume continuation and CFA precondition re-arming

- **Status:** Accepted
- **Date:** 2026-07-26

## Context

A cross run can pause because an individual child requires an operator decision.
The durable parent then carries a `project:<alias>:<child-handoff-id>` proxy,
while the child retains its own handoff. A resume must not stop after retrying
that child: remaining ready aliases still precede `contract_check` and CFA.

CFA can also pause on a REJECTED result. A result with
`source="precondition"` records a temporary absence of evaluable child facts,
unlike an agent rejection or parse error, which is an actual review outcome.
Treating all three `continue` decisions as an override retained stale CFA facts
and could bypass a newly evaluable child set.

## Decision

- Apply a project-proxy decision to its physical child, clear the active parent
  handoff from live and durable parent state, retain only that child's
  `awaiting_phase_handoff` checkpoint cursor, and dispatch the resolved child
  once before returning to the existing graph scheduler.
- Re-read only declared `<run_dir>/<alias>/meta.json` paths for canonical child
  facts; checkpoint `sub_status` is routing information, never completion
  evidence.
- For `source="precondition"`, CFA `continue` consumes the decision, removes
  the stale CFA phase entry and active handoff, clears CFA checkpoint markers,
  persists the invalidated state, rebuilds the CFA context, and performs a
  fresh CFA evaluation.
- For `source="agent"` and `source="parse_error"`, retain the explicit
  operator override and its audit marker without another reviewer invocation.
- Re-reduce canonical parent facts immediately before CFA and before terminal
  finalization.

## Consequences

The resume path makes progress through every ready child before cross gates,
and a durable `status="done"` child cannot become `CFA_MISSING_CHILD_<alias>`
merely because an older parent snapshot was incomplete. An all-done parent
finishes `done` without a synthetic child-readiness residue.

The public SDK decision API, MCP payloads, checkpoint schema, and wire shapes
remain unchanged. Missing-child guidance tells operators to decide the handoff
and resume the same cross run rather than start a new pipeline.

## Rejected alternatives

1. **Make a project retry a separate scheduler/state machine.** Rejected: the
   existing immutable graph already selects ready project nodes.
2. **Treat precondition CFA continue as a universal override.** Rejected:
   readiness facts may have changed, so it can mask a new CFA result.
3. **Trust embedded parent child sessions or checkpoint status on resume.**
   Rejected: only the declared durable child meta paths are authoritative.

## Out of scope

- New SDK/MCP decision endpoints or public schema fields.
- Changes to mono-pipeline resume, unattended decisions, or agent/parse-error
  CFA override semantics.
- A new cross state machine or generic recovery framework.
