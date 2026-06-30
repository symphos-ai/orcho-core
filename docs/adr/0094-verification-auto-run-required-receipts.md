# ADR 0094 — Auto-run missing/stale required receipts before final acceptance

- Status: Accepted
- Date: 2026-06-13
- Relates to: ADR 0080 (native command-receipts, Stage 3), ADR 0082
  (final-acceptance readiness, Stage 5), ADR 0083 (delivery-gate awareness,
  Stage 6), ADR 0089 (delivery receipt continuity, Stage 8), ADR 0090
  (require-gates cannot end in a silent green run), ADR 0091 (correction
  `gate_rerun` executes current-run receipts)
- Supersedes: the bespoke `gate_rerun` runner introduced in ADR 0091 — its
  env+required execution is now one call into the shared Stage 9 executor.

## Context — the manual-leak incidents

Runs `20260613_104716` and `20260613_125608` reached `final_acceptance` with
their required delivery receipts **missing or stale**. The readiness block and
the delivery gate did exactly what ADR 0089/0090 promised: they refused to go
green and surfaced copy-paste `orcho verify env …` / `orcho verify run --required
…` hints. But that is a dead end for the model reviewer — `final_acceptance`
cannot run shell commands — so the only way forward was to **leak the shell work
back to the human operator**: stop the run, have a person run the verify
commands by hand in the right worktree, then resume. The contract was never
falsely green (the ADR 0090 invariant held), yet the "happy path" routinely
detoured through a manual shell step that the engine already knew how to perform.

ADR 0091 had already solved the *narrow* case: a correction `gate_rerun` child
materialises its own required receipts before `final_acceptance`. But that
executor was private to the correction route, so a **first, non-correction** run
with missing/stale receipts still leaked to the operator. The same materialise
logic was needed one layer up — before every final phase — and it should not be
duplicated.

## Decision

Before a **final phase** runs, Orcho materialises the run's *missing* and
*stale* required delivery receipts through a single shared executor, so the
`before_delivery` gate, the `final_acceptance` readiness render, and the Stage 6
delivery gate all read fresh on-disk evidence. Manual `orcho verify env/run`
becomes a **fallback / escape-hatch**, no longer the normal route to a green run.

### Shared executor (one runner, two callers)

`pipeline/project/verification_autorun.py::materialize_required_receipts` is the
only runner. It takes an explicit run context — `run_id`, `run_dir`,
`project_dir`, `checkout`, the projected `contract`, a
`PlaceholderContext` (`ctx`), `workspace`, parent receipt sources, `dry_run`,
and a `reason` — and returns a frozen `ReceiptAutoRunResult`
(`attempted / ran_envs / ran_commands / skipped_manual / skipped_fresh / failed /
errors / receipt_paths`, plus `to_evidence()`).

Classification reuses the **same** `classify_required_receipts` that readiness
and delivery use (ADR 0089), against the explicit `ctx` — so the auto-run's
notion of present/missing/stale/failed (including ADR 0084 cross-repo dependency
HEAD drift and ADR 0089 parent inheritance) is identical to the gates that read
the result. Execution is strictly through `sdk.verify.verify_env` /
`sdk.verify.verify_run`: one env pass per needed env, one command pass over the
targets — **no retry loop**.

Crucially, the executor classifies and targets the **delivery-selected** command
set, not merely `verification.required`. It threads the run's **full**
`state.extras` into `classify_required_receipts`, so `delivery_gate_plan` reuses
the **cached `before_delivery` routing plan** and the same selection context that
readiness (Stage 5) and the delivery gate (Stage 6) consult — *not* a fresh plan
rebuilt from the live worktree. That makes **path-selected delivery gates** (for
example `cli-sdk-unit`, scheduled only because the diff touched its paths)
materialise automatically alongside the static `required` set. An earlier
revision passed a stripped `state.extras` (only the parent-runs key), so the
executor saw `verification.required` alone and silently under-targeted: a
path-selected gate the readiness/delivery surfaces still demanded was left
`missing`, re-leaking that command to a manual `orcho verify run`. Threading the
full extras removes that desync — auto-run now targets exactly what the gates
enforce, and the manual CLI is the fallback, never the happy path.

The correction `gate_rerun` route (ADR 0091) now **delegates** to this executor
instead of running its own `verify env` + `verify run --required` pair; its
`GateRerunExecution` is a thin adapter that maps `ReceiptAutoRunResult` onto the
historical `gate_rerun_execution` evidence keys.

### Auto-run policy (allowed vs not allowed)

Targets are exactly the required delivery commands classified **missing** or
**stale**. The following are deliberately **not** auto-run:

- **manual_only / operator-only** — a command marked `manual_only`, or closed
  behind an unrequested operator gate-set, is never auto-run even when it is also
  `required`. It is recorded in `skipped_manual` and stays an explicit operator
  escape-hatch (the raw set comes from
  `sdk.verify.manual_or_operator_only_commands`, *before* the
  `verify run` subtraction of `required`, so a `required` + `manual_only` command
  stays manual).
- **fresh / present** — a required receipt that already classifies `present`
  (incl. an inherited valid parent receipt) is left untouched and recorded in
  `skipped_fresh`; its command is never executed.
- **failed** — a `failed` current-subject receipt is **never re-run** in the
  normal path. A fresh same-diff failure must stay failed, never silently
  re-greened (the ADR 0090 "never falsely green" invariant). A failure produced
  *by* the auto-run is persisted as a failed receipt and reported in
  `result.failed`.
- **dry-run** — `attempted=False`, no command executes, nothing is recorded.
- **no contract / empty resolved delivery-required set** — strict no-op
  (`attempted=False`). The empty-set check fires *after* the delivery plan
  resolves, so a contract with an empty static `verification.required` but a
  non-empty path-selected delivery set still materialises; only a genuinely empty
  resolved set no-ops.

Executor failures degrade into `errors` and are never raised: `final_acceptance`
and the delivery gate remain the authoritative release verdict.

### Integration point

The wiring lives in `pipeline/project/run.py::_PipelineRun._on_phase_pre`. After
the correction-route skip check and **before** `evaluate_pre_phase_gates`, when
`name in FINAL_PHASES` (`final_acceptance` / `compliance_check`,
`pipeline.verification_contract.FINAL_PHASES`) and the phase is not route-skipped,
it calls the thin run-adapter
`auto_run_required_receipts(self, name, reason=…)`. The adapter resolves context
from the run (`output_dir`, `project_path`) and `state.extras`
(`verification_contract`, `verification_placeholders` as `ctx`, parent sources
under `verification_parent_runs`), building `ctx` via
`placeholder_context_for(...)` only when absent. `_on_phase_pre` stays thin —
import, guard, call — and the `final_acceptance` handler / `review_support` are
untouched.

### Durable evidence (fixed contract)

`auto_run_required_receipts` records two sinks:

- **Append-only audit trail.**
  `state.extras['verification_autorun']` is a list; each auto-run appends one
  `ReceiptAutoRunResult.to_evidence()` entry. It accumulates across every final
  phase that triggered an auto-run.
- **Per-phase session mirror.** The same entry is mirrored at
  `session['phase_log'][phase]['verification_autorun']` (nested dicts created on
  demand), keyed by the phase that triggered it.

Guard no-ops (dry-run / no `output_dir` / no contract) record nothing, so a
dry-run stays fully side-effect-free.

### Readiness / delivery consistency

Because the auto-run writes the same command-receipts that
`classify_required_receipts` reads, and readiness (Stage 5, via
`review_support`) and the delivery gate (Stage 6, `assess_delivery_verification`)
both read those on-disk receipts, the three surfaces are constructively
guaranteed to agree after the auto-run: a materialised present receipt clears the
gate; a materialised failure is reported `failed` by all three.

### Contract provenance invariant

Classification keys off `state.extras['verification_contract']` (the projected
contract for the run), while `sdk.verify` canonically **reloads** the contract
from the target project's `plugin.py` at execution time. This is the accepted
existing invariant (the same one ADR 0091's `gate_rerun` honoured): the executor
passes a `project_dir` matching the run's `meta['project']`, so both views resolve
the same contract and there is no second source of truth.

## Consequences

- The normal path no longer leaks shell work to the operator: a first run with
  missing/stale required receipts materialises them itself before the gate reads.
- No wire change: `COMMAND_RECEIPT_SCHEMA_VERSION` stays **2**; the verification
  contract schema and the `orcho verify` CLI are unchanged (`verify env` /
  `verify run` behaviour is byte-identical). `verification_autorun` is an
  in-process `state.extras` evidence key, not a serialized contract field.
- One runner: the ADR 0091 bespoke `gate_rerun` runner is gone; correction and
  pre-final-acceptance materialisation share `materialize_required_receipts`.
- The "never falsely green" invariant is preserved end-to-end: failed receipts
  are not re-run, manual/operator-only required commands stay blocked with the
  manual instruction, and any residual missing/stale/failed/error keeps the gate
  red (e.g. `gate_rerun` `required_passed` is true only when `failed`, `errors`,
  and `skipped_manual` are all empty).
- Manual `orcho verify env/run` remains available as a fallback / escape-hatch
  for operator-only commands and out-of-band debugging — not the happy path.
