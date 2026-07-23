# ADR 0146 — Cross-child outcome and gate admission

- **Status:** Accepted
- **Date:** 2026-07-21
- **Related:** ADR 0025, ADR 0050

## Context

Cross dispatch used a normal Python return from a child pipeline as success
evidence.  Its `finally` block consequently replaced `halted`, `failed`, and
other durable child outcomes with checkpoint `sub_status=done`.  That makes a
cross checkpoint claim readiness which the exact persisted child session does
not support.

## Decision

The returned child `session.status` is the sole authority for dispatch
readiness.  Dispatch classifies it using the canonical status vocabulary:

| Child status / payload | Dispatch outcome | Checkpoint sub-status |
| --- | --- | --- |
| `done`, `success`, `completed` | success | `done` |
| `halted` with canonical final-acceptance rejection and a typed rejecting release record | release-evaluable rejection | `done` |
| `awaiting_phase_handoff` with a mapping payload | pause | existing handoff proxy |
| `failed`, other `halted`, `interrupted` | failure | `failed` |
| missing, non-string, unknown, or pause without a mapping payload | fail-closed failure | `failed` |

The exact returned session remains persisted under
`session.phases.projects[alias]`; checkpoint classification is a separate,
derived readiness fact.  Dispatch continues later sibling aliases after a
failure and returns ordered immutable `blocking_aliases`, rather than asking a
later admission decision to reread mutable session or checkpoint state.

A rejected child release is not dispatch success and never becomes ship-ready.
It is nevertheless complete gate input: contract review may inspect it and
cross final acceptance preserves the specific `CFA_CHILD_REJECTED_<alias>`
blocker instead of misreporting a completed child as missing.

Dry runs and profiles with no project-scoped child steps retain their separate
simulation/skip behavior and are not synthetic missing child returns.

## Consequences

Contract-check admission consumes the immutable dispatch readiness result.
Readiness is not contract compatibility: a blocked child must be represented
as a gate precondition, not as a synthetic interface rejection. For each
blocked alias the cross session persists a minimal contract phase entry:

```json
{
  "approved": false,
  "verdict": "NOT_EVALUABLE",
  "not_evaluable": true,
  "source": "precondition",
  "reason": "child_readiness",
  "child_status": "halted",
  "child_reason": "operator requested stop",
  "findings": [],
  "risks": [],
  "checks": []
}
```

This entry is neither `SKIPPED` (so it has no `on_skip` policy) nor
`REJECTED` (so it does not assert interface incompatibility). Contract
provider and gate-decision calls do not run when such entries are produced.
The CFA precondition turns it into `CFA_MISSING_CHILD_<alias>` with the exact
status and halt/error reason; it does not add `CFA_CONTRACT_REJECTED_<alias>`
and its synthesized `contract_status.interfaces` is `not_applicable`.

Resume skips only checkpoint aliases whose derived sub-status is exactly
`done`; failed, halted, interrupted, and unrecognised prior states are
dispatched again. `NOT_EVALUABLE` is not a completed contract cache entry, so
a successful retry performs real contract evaluation. Existing completed
`APPROVED`, `REJECTED`, and explicit `SKIPPED` gate entries remain reusable.

## Out of scope

This decision does not introduce fail-fast dispatch, dependency scheduling,
rejected-child correction, a new persisted ledger, or a policy that retries
or repairs a blocked child automatically.
