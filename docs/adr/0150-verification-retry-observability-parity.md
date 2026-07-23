# ADR 0150: Verification retry observability parity

- **Status:** accepted
- **Date:** 2026-07-22

## Decision

A verification-gate `retry_feedback` uses exactly one immutable
`VerificationHandoffRetryContext`, created from the active handoff and the full
`(command, hook, phase)` identity.  Its automatic maximum is inherited from
the active handoff; the fresh repair round is not a new automatic budget.

`repair_changes` continues through the lifecycle FSM, which remains the sole
owner of its phase metric attempt.  The retry path snapshots those FSM metrics
before executing the selected gate again.

`scheduled_gate_ledger.json` remains the sole durable gate trail.  An exact
rerun is a second `execution` event for the same full identity, carrying
`rerun: true` and its command-receipt evidence path.  Replaying the identical
event is idempotent.  Evidence copies that ledger and the SDK timeline projects
the persisted `rerun` fact into its existing `ReceiptEvidence`; no SDK or MCP
wire shape changes.

## Consequences

An exhausted automatic round `2/2` followed by operator retry becomes fresh
round `3/2`, rendered as `human retry 1 after REJECTED verdict`, never as an
impossible automatic `3/3`.  The retained repair subject and exact gate
identity survive the retry boundary.
