# ADR 0191 — Delivery ledger, the unresolved-action guard, and delivery reconciliation

- **Status:** Accepted
- **Date:** 2026-09-07
- **Related:** [ADR 0032](0032-commit-decision-gate.md),
  [ADR 0099](0099-deferred-delivery-decision-gate.md),
  [ADR 0106](0106-rejected-release-terminal-and-override.md),
  [ADR 0114](0114-run-control-state-projection-and-classifier-ownership.md),
  [ADR 0119](0119-delivery-branch-policy.md),
  [ADR 0188](0188-typed-acceptance-criteria-and-criterion-matrix.md)

## Context

A dogfood run (`20260907_100016_02dcb1`) ended with a real commit in the
operator's checkout and no durable trace of it. Three independent defects
lined up:

1. **An unresolved action reached Git (E1).** The run used
   `commit.decision_mode = "defer"`. Its resume was launched by an MCP
   supervisor without `--no-interactive` and without a TTY.
   `resolve_commit_delivery` computes interactivity as
   `not no_interactive and stdio_interactive()` and parked the decision as
   `action="none"` / `status="pending"`. The producer in
   `pipeline/project/run.py` parked only when `no_interactive` was set, so it
   handed the parked decision to `apply_commit_delivery`, which had no guard
   for `none`: it transported the patch, ran `git add` and `git commit -s`,
   and only then failed in `_persist` because the audit schema rejects
   `action="none"`. The release was REJECTED by the engine backstop; the
   commit landed anyway; the process exited 1; `meta.json` still said
   `running` with no `commit_delivery` block.
2. **Executable criteria the run could never prove (E2).** The plan carried
   four `executable` criteria with no `gate_refs`. ADR 0188's addendum binds
   such criteria to the run's *selected* gates, but the project declares no
   verification contract, so no scheduled-gate ledger existed and there was
   nothing to bind to. `validate_plan` accepted the plan; the criteria
   stayed `missing` for the whole run; the final-acceptance backstop turned
   the model's APPROVED into REJECTED after the entire implement / review
   budget was spent, with the reason rendered as a bare `missing:`.
3. **Status lost the side effect and recommended an unsafe resume (E3).**
   With no delivery record, the status surfaces reported
   `delivery_committed=false` and `final_acceptance=null` (the record lived
   only in the checkpoint store), and the diagnosis suggested a plain resume
   — which would have re-entered delivery for a diff that was already in the
   checkout. The final-acceptance record also kept only the engine's verdict;
   the model's own verdict survived nowhere durable.

## Decision

### 1. Fail-closed delivery execution

- `apply_commit_delivery` returns an unchanged decision for any action
  outside `{fix, approve, apply, skip, halt}` before the dirty guard, the
  transport, staging, commit or publish. The caller's correctness is not a
  precondition.
- The producer parks on the decision itself: `status="pending"` with
  `action="none"` halts the run at `commit_delivery_pending`, whichever
  predicate made the run headless. `no_interactive` is no longer re-derived
  in the producer.
- The audit artifact a successful `approve` / `apply` will write is
  validated **before** any mutation (`_preflight_audit`). A schema refusal
  can only ever happen with the checkout untouched.

### 2. Delivery ledger

`pipeline/engine/delivery_ledger.py` owns a small durable record per
decision, `commit_decisions/<id>.delivery.json`, written around the commit:

- `intent` — before the first mutating git op of a commit: action, commit
  target, base, HEAD and branch before, message, strategy, staged paths;
- `committed` — right after `git commit`, with the sha;
- `recorded` — once the audit artifact exists.

`reconcile_delivery` is the read-only reader (`rev-parse`, `symbolic-ref`,
`cat-file`, `log` only). It answers `recorded`, `committed_unrecorded`,
`intent_only` (the intent is matched against Git by parent + subject),
`commit_missing`, `unreadable`, `none`, or `legacy_commit` — a commit with
the run's deterministic fallback subject on a run that kept no ledger.

`resolve_commit_delivery` consults it before any gate or diff:

- a ledger-backed commit (`committed_unrecorded` / `recorded`) is **adopted**:
  the audit is completed from the intent and the decision returns
  `committed` with `provenance="resume_adopted"` — a resume after a crash
  between commit and audit is idempotent and never creates a second commit;
- a `legacy_commit` is **never adopted** (nothing durable says who decided
  it): the resolve refuses to deliver again and returns `not_applicable`
  carrying the sha and `provenance="existing_commit"`, which the producer
  persists so status readers name the commit.

### 3. Provenance and operator reconciliation

`CommitDeliveryDecision.provenance` (`""` / `resume_adopted` /
`existing_commit` / `reconciled`) is serialised only when non-empty. The
rejected-release reducer stamps it onto the `delivery_override` marker and,
for `reconciled`, words the marker as a reconciliation — not as an operator
override and not as an approval of the rejected release.

`sdk.run_control.delivery_reconcile` exposes the read-state
(`inspect_delivery_reconciliation`) and the command
(`reconcile_delivery_record`); `orcho reconcile-delivery <run_id>` is the CLI
(dry-run by default, `--apply --commit <sha>` to record). The command writes
the audit artifact with `operator` / `note`, sets `meta.commit_delivery`
with `provenance="reconciled"`, restores `final_acceptance` from the
checkpoint store when meta never received it, and settles the terminal
through the same reducers finalization uses. The checkout is never mutated.

### 4. Diagnosis and status

`run_diagnosis` gains the condition `delivery_inconsistent`
(`recommended_next_action="reconcile_delivery"`), classified before every
delivery-gate and terminal branch whenever Git holds a delivery commit the
run does not record. It is never a resume target; MCP mirrors it as a
non-resumable condition and refuses `orcho_run_resume` before spawning.
The live terminal card distinguishes an unknown delivery (a run that failed
before recording anything) from a recorded absence.

The final-acceptance session record now carries `engine_backstop`, including
`model_verdict` / `model_ship_ready`, so the model's verdict and the engine's
verdict are always separately readable.

### 5. Plan review diagnoses unprovable executable criteria

`plan_gate_ref_problems` reports an implied executable criterion when the run
declares no scheduled gate at all or every declared gate is resolved as not
selected. `validate_plan` routes it as the same planner-fixable rejection an
unresolvable explicit ref gets, naming the fix: reclassify as
`agent_assertion` / `human`, or declare the check as a project gate. A
plan-only context (`run_dir is None`) stays permissive, as before. The
criterion matrix states why an unbound implied row is `missing` and what
would bind it.

## Consequences

- A delivery commit is either recorded, adopted on resume, or reported with
  its sha; it can no longer vanish from the run's story.
- Operators of pre-ledger runs record an orphaned commit explicitly, with
  attribution, instead of editing `meta.json` by hand.
- A plan whose executable criteria have no gate to bind to is rejected in
  planning (one extra round) instead of at final acceptance (the whole run).
- `delivery_inconsistent` is a new label in the closed diagnosis vocabulary;
  MCP consumers must recognise it (shipped together with `orcho-mcp`).

## Out of scope

- Binding a human criterion decision to the source revision it judged
  (`hd-C8-1` in the dogfood run was recorded against an older tree and still
  reads `accepted`). Follow-up: a decision records the fingerprint of the
  run-owned diff it judged, and the matrix reports it `stale` when the diff
  changes.
- Automatic adoption of a `legacy_commit`.
