# ADR 0091 — Correction `gate_rerun` executes current-run verification receipts

Status: accepted

Date: 2026-06-13

Supersedes: the `gate_rerun` route detail in ADR 0086 that skipped directly to
`final_acceptance` without executing verification commands.

## Context

ADR 0086 introduced `correction_triage.kind = gate_rerun` as a shortcut route:
skip code-changing phases and let `final_acceptance` re-check the retained
worktree. That was sufficient before delivery readiness had durable required
receipts.

With the verification contract delivery gate, this shortcut became a loop:

1. `final_acceptance` rejects because required receipts are missing or stale.
2. The operator chooses `fix`.
3. The correction child classifies `gate_rerun`.
4. The route skips `implement`, `review_changes`, and `repair_changes`.
5. `final_acceptance` runs immediately in the new child run and still has no
   current-run receipts.

Running receipts manually for the parent child does not solve the loop, because
the next `fix` mints another child run id and the closing gate evaluates that
current run.

## Decision

`gate_rerun` remains a shortcut route for code phases, but it is no longer a
no-op between `correction_triage` and `final_acceptance`.

When `correction_triage` completes with route kind `gate_rerun`, Orcho executes
the current child run's required verification receipts before downstream phases
are skipped:

- `orcho verify env` equivalent for the contract's `default_env`, when present;
- `orcho verify run --required` equivalent for the current child `run_id`.

Receipts are written into the current child run directory, not the parent run.
The route then still skips `implement`, `review_changes`, and `repair_changes`,
and `final_acceptance` remains the authoritative release gate. If receipt
execution fails, the error is recorded as route evidence and final acceptance
will reject using the normal readiness diagnostics.

`contract_ack` is unchanged: it skips code phases without running verification
commands.

## Consequences

- The operator no longer gets trapped in a `fix -> new child -> missing receipts`
  loop for pure verification-rerun corrections.
- The route is still cheap compared with a full code-fix correction: no planning,
  no implementation turn, no review turn.
- The current child run becomes self-contained evidence: its run directory owns
  the receipts that `final_acceptance` evaluates.
- A contract-less `gate_rerun` stays a harmless no-op shortcut for existing mock
  and legacy tests.

