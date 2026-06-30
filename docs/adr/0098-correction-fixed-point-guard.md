# ADR 0098 — Deterministic fixed-point guard for the correction loop

Status: accepted

Date: 2026-06-19

## Context

The auto-correction follow-up loop (ADR 0070) is operator-gated: every round
that still rejects ends back at the correction gate, and the loop continues only
while the operator keeps choosing `fix`. There is no round ceiling — a human
decision sits between every round.

That gate does not protect against a *fixed point*: `final_acceptance` rejects
for the same release blockers as the parent, the correction round makes no
relevant progress on the flagged files or evidence, and the run still finishes
in a way that invites yet another correction. The agent cannot legally decide to
expand scope, waive a blocker, or change the contract, so the same loop repeats —
same findings, same files, same outcome — wasting time and money.

## Decision

Add a deterministic, conservative guard between correction rounds. The guard
compares a correction child against the session that seeded it; when the child
repeats the same final-acceptance blocker identities with no relevant progress,
the loop stops with a dedicated non-converging outcome instead of spawning
another round.

### Blocker identity

Identity is built from **durable evidence**, never from provider prose
(`critique` / `short_summary`). The source is the child's
`session['phases']['final_acceptance']` record:

- `release_blockers` — keyed on blocker code (`id` / `code`, falling back to
  `title`), `severity`, and affected `file`/`path`; the prose `body` /
  `why_blocks_release` are excluded so a reworded explanation of the same
  blocker still collapses to the same key.
- `verification_gaps` — keyed on `required_check` (the actionable command),
  falling back to `risk`.
- `engine_backstop.gaps` — the deterministic missing/stale/failed required-
  receipt backstop (ADR 0090), keyed the same way as `verification_gaps`.

Each evidence item normalizes to a stable lowercase key string; a run's identity
is the `frozenset` of those keys. The normalization and comparison are **pure**
(`pipeline/project/correction_fixed_point.py`): no IO, no subprocess, no
provider. The two progress facts are injected by the driver.

### Firing condition (strictly conjunctive, conservative)

`is_fixed_point` is true only when **all** hold:

1. both parent and child are *rejected-with-blockers* — a rejecting verdict
   (`verdict == REJECTED`, or `ship_ready` / `approved` is `False`) **and** a
   non-empty identity set;
2. the normalized identity sets are non-empty and **equal** — a changed identity
   (a blocker fixed, removed, or newly introduced) counts as progress;
3. there is **no** progress signal — neither a changed child diff
   (`diff.patch` differs from the parent round's) nor fresher/passing receipts
   (a `gate_rerun` that went green, or a changed timestamp-free receipt
   fingerprint).

Any ambiguity (missing/unreadable `diff.patch` or receipt directory) is treated
as *progress present*, suppressing the guard. The guard never fires on
incomplete evidence — a false negative (one extra round) is acceptable; a false
positive (halting a run that did make progress) is not.

### Outcome

When the guard fires, the driver
(`pipeline/project/correction_followup.py::drive_correction_followups`) re-marks
the already-finalized child:

- `mark_run_halted(child, halt_reason='correction_not_converging')` — a new
  terminal `halt_reason`. Run status stays `halted` (terminal, **not**
  `done`/approved delivery); `pipeline/run_state/reducer.py` is unchanged.
- a durable `session['correction_fixed_point']` block:
  `{repeated, parent_run_id, child_run_id, suggested_actions, reason}`.
- `save_session` rewrites the child's `meta.json` with the new outcome.
- best-effort `correction.fixed_point` observability event (not load-bearing).
- an operator block is printed:

  ```text
  Correction is not converging.
  Repeated blockers: R1, R2
  Parent run: <id>
  Child run: <id>
  No relevant blocker evidence changed since parent run.
  Human decision required: retry with new instructions, approve/waive, or halt.
  ```

Finalization renders the outcome honestly: `_HALT_BANNER_LABELS` maps
`correction_not_converging` to an amber (`C.YELLOW`) recoverable banner, and the
evidence summary adds a `Correction: not converging` line with the repeated
identities and parent/child run ids — so the result never reads as a green DONE.

## Non-Goals

- No auto-waiver of repeated blockers.
- No loosening of `final_acceptance` policy.
- No LLM classifier — identity and comparison are deterministic.
- No artificial round ceiling and no change to the operator-gated character of
  the loop outside the fixed-point condition.
- A correction child that changed the flagged file, produced a previously
  missing receipt, or changed blocker identity is never flagged; ordinary
  `gate_rerun` with fresh receipts is never treated as non-converging.

## MCP Validation

`correction_not_converging` is a new **string value** for the existing
`halt_reason` field and `correction_fixed_point` is an additive durable session
block. Neither changes a wire schema, profile shape, mode flag, or gate
primitive, and the SDK/MCP surfaces carry `halt_reason` as a free-form string
(no enum to extend). No matching `orcho-mcp` update is therefore required for
this change; existing `halt_reason`-keyed consumers see the new value
transparently.

## Consequences

- Non-converging correction loops stop deterministically with an operator-
  actionable explanation instead of silently inviting another identical round.
- The guard is conservative by construction: under any evidence ambiguity it
  defers to the operator-gated loop rather than halting.
- The pure evaluator is independently testable and free of provider/IO concerns;
  the driver owns the single IO seam that reads `diff.patch` and receipts.
