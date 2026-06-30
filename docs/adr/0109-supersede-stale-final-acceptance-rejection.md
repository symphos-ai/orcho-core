# ADR 0109 — Supersede a stale rejection terminal and phantom delivery-gate on an approved re-run

Status: Accepted

## Overview

Finalization reconciles **both** the terminal run-state **and** the delivery
gate to the *latest authoritative* `final_acceptance` verdict — not just the
first one a run ever produced. A successful repeat or resumed `final_acceptance`
(verdict `APPROVED`) **evicts** the stale rejection terminal
(`halt_reason="final_acceptance_rejected"`, `rejected_outcome`,
`delivery_override`, the nested `halt` block) **and** the phantom rejected
`commit_delivery` gate left behind by a prior `REJECTED` attempt of the same
run. The invariants ADR 0106 established for a genuinely rejected release are
left exactly as they were.

This is an append-only refinement of [ADR 0106](0106-rejected-release-terminal-and-override.md):
ADR 0106 made finalization write the rejection terminal + gate when the release
*is* rejected; ADR 0109 makes the same helper *also* unwind that terminal + gate
when a later authoritative verdict approves the release.

## Context

ADR 0106 closed the "silent rejected success" hole: a rejected release now
writes `halt_reason="final_acceptance_rejected"` + a structured
`rejected_outcome` (no delivery), or a durable `delivery_override` marker (an
operator override that actually applied delivery), and persists the rejected
`commit_delivery` decision so the SDK reads a decidable `fix`-or-`halt`
correction gate. That direction is correct and unchanged.

What ADR 0106 did not cover is the **second pass over the same run**. A real
dogfood run (`20260625_102656_214b4d`) reproduced the gap: the run rejected at
`final_acceptance`, the operator repaired the change, and a repeat/resumed
`final_acceptance` returned `APPROVED`. Two pieces of stale residue from the
first (rejected) pass survived into the approved terminal:

1. **Stale terminal markers.** A prior finalization had written
   `halt_reason="final_acceptance_rejected"`, `rejected_outcome`, the nested
   `halt` block (and, on an override pass, `delivery_override`) into the session.
   On the approved re-run, the approved branch of
   `_apply_rejected_release_terminal_outcome` simply early-returned, leaving
   those markers in place. The run finalized `done`, yet `meta.json`, the
   `run.end` payload, and `sdk.status.load_status` still surfaced a
   `final_acceptance_rejected` halt and rejected blockers — a clean success that
   read as a rejected halt.

2. **A phantom rejected delivery-gate.** On the first pass, `run.py`'s
   `_run_commit_delivery` persisted the rejected delivery decision (a
   `not_applicable` record carrying a non-`APPROVED` `release_verdict`) per ADR
   0106 §2. On the approved re-run, the now-`APPROVED` decision resolved to
   `not_applicable` / `no_diff` (nothing to ship) and hit the early return in
   `run.py` (`_run_commit_delivery`, lines 1324–1328) that drops those statuses
   **without overwriting** `commit_delivery`. The stale rejected record
   therefore survived, and `sdk/run_control/delivery.py` re-read it as a
   decidable correction gate: `delivery_decision_state` returned
   `decidable=True, kind="correction"` and `decide_delivery` refused shipping
   actions with `blocker="release_blocked"` — a phantom gate on a clean,
   already-approved run.

The two holes share a root cause: finalization treated the rejected verdict as
write-once, so once the rejection terminal/gate existed nothing reconciled it
back when a later authoritative verdict approved the release.

## Decision

The approved branch of `_apply_rejected_release_terminal_outcome`
(`pipeline/project/finalization.py`) becomes a **bidirectional reconciler**
rather than a silent early-return. It is the single chokepoint that runs after
`_run_commit_delivery` and **before** `run.end` emission and `save_session`, so
the in-memory session it reconciles is exactly what the `run.end` payload and
the persisted `meta.json` observe — no separate read-surface patch is needed.

The helper keeps its existing guards (`status == "done"`, `dry_run` off, a
`phases` Mapping) and the `_release_rejected_from_phases(phases)` decision
point:

- **Rejected authoritative verdict** (`_release_rejected_from_phases` is true) —
  unchanged ADR 0106 behavior: write the actionable `halted` terminal +
  `rejected_outcome` (no delivery) or the `delivery_override` marker (override
  that applied delivery).

- **Approved authoritative verdict** (`_release_rejected_from_phases` is false)
  — instead of returning silently, delegate to a small focused helper,
  `_supersede_stale_rejection_residue(session)`, which:
  1. Idempotently pops the stale top-level terminal-rejection markers
     (`halt_reason`, `halted_at`, `rejected_outcome`, `delivery_override`, and
     the nested `halt` block).
  2. **Pointwise** reconciles `commit_delivery`: it removes the record **only**
     when `release_verdict` is present and `!= "APPROVED"` (the phantom rejected
     gate). A `commit_delivery` whose `release_verdict` is `"APPROVED"` or
     empty/absent is the legitimate current applied/parked APPROVED gate —
     including one parked on a verification or delivery-scope block per ADR 0099
     / ADR 0100 — and is left untouched. The companion `multi_project_delivery`
     block is dropped only when its `primary_status` mirrors the superseded
     phantom's `status`.

The reconciliation deliberately stays in `finalization.py`. It is **not** pushed
down into `pipeline/run_state/terminal.py`: `rejected_outcome` /
`delivery_override` / the `halt` block / `commit_delivery` are
finalization-domain markers, and the low-level terminal writer
(`mark_run_done`) must not know about them.

### ADR 0106 invariants explicitly preserved

The supersede branch is reachable **only** on the approved path
(`status == "done"` and `_release_rejected_from_phases` false). A genuinely
rejected release never reaches it, so every ADR 0106 invariant holds unchanged:

- **Actionable halted without delivery** — a rejected release with a real diff
  and no applied delivery still flips `done` → `halted` with
  `halt_reason="final_acceptance_rejected"` and a structured `rejected_outcome`.
- **Override marker** — a rejected release whose delivery was actually applied
  (operator override) still stays `done` carrying the durable
  `delivery_override` marker.
- **No-diff terminal** — the more specific no-diff reject keeps its own
  `halt_reason="final_acceptance_no_diff"` terminal; it settles non-`done`
  first, so the `done`-guard leaves it untouched.
- **Correction gate for a current REJECTED** — a run parked on a *current*
  rejected `commit_delivery` still reads as a decidable `fix`-or-`halt`
  correction gate (`decidable=True, kind="correction"`,
  `decide_delivery` refusing shipping actions with `release_blocked`).

## Consequences

- A successful repeat/resumed `final_acceptance` lands a clean `done` whose
  `meta.json` carries none of `halt_reason` / `rejected_outcome` /
  `delivery_override` / `halt` and no rejected `commit_delivery`. The `run.end`
  payload and the persisted `meta.json` agree.
- The SDK read-surfaces reconcile **without any separate edit**, because they
  project off the now-clean `meta.json`: `sdk.status.load_status` no longer
  surfaces a stale `final_acceptance_rejected` halt or rejected blockers in
  `RunMeta.extra`, and `delivery_decision_state` returns
  `decidable=False, kind="none", reason="no pending delivery gate"` with
  `decide_delivery` no longer refusing shipping actions via `release_blocked`.
- No new command, action, or wire shape is introduced. The approved path simply
  stops carrying residue from a superseded rejected pass; the rejected path is
  byte-for-byte ADR 0106.
- The reconciliation is precise: only a non-`APPROVED` `release_verdict` is
  treated as phantom, so an APPROVED gate parked on a verification or
  delivery-scope decision (ADR 0099 / ADR 0100) is never disturbed.

## Related

- [ADR 0106](0106-rejected-release-terminal-and-override.md) — rejected
  final-acceptance terminal semantics and observable override (the ADR this
  refines, append-only; the rejected branch is unchanged).
- [ADR 0099](0099-deferred-delivery-decision-gate.md) — deferred-delivery
  decision service (the parked APPROVED gate that must survive reconciliation).
- ADR 0100 — defer-mode parked delivery decision (verification / scope parking
  that an APPROVED `commit_delivery` legitimately carries).
- [ADR 0107](0107-companion-repo-delivery-disclosure.md) — companion-repo
  delivery disclosure (`multi_project_delivery`, dropped here only when it
  mirrors a superseded phantom).
