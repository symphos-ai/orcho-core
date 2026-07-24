# ADR 0154 — Typed halt resume control state

- **Status:** Accepted
- **Date:** 2026-07-23
- **Related:** [ADR 0120](0120-unattended-no-interactive-phase-handoffs.md) and [ADR 0153](0153-gate-handoff-retry-executability.md)

## Context

ADR 0120 introduced an unattended terminal halt when it would be unsafe to
invent a phase-handoff decision.  That halt is not an operator's terminal
`halt` decision: on a later checkpoint resume it must first restore the exact
handoff that was withheld.  ADR 0153 likewise requires that action menus are
executable and audit-grade, rather than recomputed from a changed profile.

## Decision

Expected resume-boundary failures belong to the narrow `ResumeControlError`
family. `ResumeVerificationLedgerError` remains importable from its historical
module but is part of that family. Callers convert only this family into a
durable, reason-preserving halted outcome; unrelated exceptions retain ordinary
crash and `atexit` behaviour.

An unattended halt persists `phase_handoff_unattended["phase_handoff"]` as the
complete canonical payload alongside its policy reason/note: id,
phase/type/trigger identity, artifacts, and the exact `available_actions` list.
Its first checkpoint resume validates
and re-arms that payload before dispatch or `on_resume`, changing the live
state to `awaiting_phase_handoff`. A normal `phase_handoff_halt` remains a
terminal parent refusal; `phase_handoff_unattended_halt` is the sole exception.

Legacy compact unattended blocks are refused with a typed, reason-preserving
failure. Missing identity, artifacts, or actions are audit-critical and must
not be guessed or recomputed.

For a resumable unattended halt, the scheduled-gate ledger remains open until
the operator writes a decision and the run eventually reaches a normal terminal
outcome. This is strictly **skip finalization**. Replaying or opening a new
epoch from a finalized ledger is prohibited; existing finalized-ledger
fail-closed behaviour remains in force.

## Consequences

The resume implementation has one control-plane catch boundary and stable
durable diagnosis. Public SDK/MCP payload shapes do not change. ADR 0120's
unattended policy remains conservative, while ADR 0153's exact executable menu
is preserved through re-arm.
