# ADR 0153 — Verification-gate handoff retry executability

- **Status:** Accepted
- **Date:** 2026-07-23
- **Related:** [ADR 0081](0081-verification-contract-scheduling-and-repair-routing.md), [ADR 0130](0130-verification-failure-classification.md), [ADR 0149](0149-canonical-continuation-preflight-and-verification-retry.md), and [ADR 0150](0150-verification-retry-observability-parity.md)

## Context

A verification-gate handoff previously advertised `retry_feedback` for every
non-hygiene failure. Some active profiles, including `small_task`, have no
`repair_changes` step and therefore cannot execute that action. In addition,
a persisted retry decision can outlive a profile projection that removes the
repair step. The retry preflight correctly raises a typed control-plane
blocker, but allowing it to escape leaves a resumed run without its normal
pause state.

The decision artifact is immutable and exact-payload idempotent. Reusing its
handoff id for a re-parked menu would make a different recovery action conflict
with the recorded `retry_feedback` decision.

## Decision

`pipeline.project.gate_repair._request_handoff` owns the executable action
menu for verification failures. It receives the active profile from every
handoff caller, including the selected-gate rerun path, and offers
`retry_feedback` only when `_repair_step(profile)` resolves a repair step.
Non-hygiene ordering otherwise remains `continue`, `halt`,
`continue_with_waiver`. The hygiene menu remains exactly
`continue_with_waiver`, `halt`.

`apply_verification_handoff_resume` catches only
`VerificationHandoffRetryBlocked`. It re-publishes a normal gate handoff with
a fresh `:retry_blocked` identity, preserves the original gate identity and
recovery subject, and writes the blocker reason into existing artifacts and
operator-visible output. The ordinary pause tail then restores session,
checkpoint, events, and `meta.json` to `awaiting_phase_handoff`.

The old decision artifact remains audit-grade evidence. A fresh id makes the
new menu decidable through the existing SDK membership validation without
overwriting or weakening exact-payload idempotency.

## Consequences

- Operators receive only actions their current profile can execute.
- A legacy persisted retry decision produces a recoverable pause rather than a
  crash or an interrupted lifecycle.
- Provider and process exceptions are not caught by this control-plane path.
- No top-level handoff payload field, SDK schema, SDK production code, MCP
  adapter, or transport wire shape changes.

## Out of scope

- unattended-halt resume semantics;
- a generalized action-policy matrix or new action kinds;
- cross-project handoff behavior;
- profile JSON changes;
- MCP transport or SDK wire changes.
