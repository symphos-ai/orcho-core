# ADR 0095 — Auditable verification-gate timeline over additive durable trails

- Status: Accepted
- Date: 2026-06-14
- Relates to: ADR 0081 (scheduling and repair routing, `gate_repair`), ADR 0082
  (final-acceptance readiness), ADR 0083 (delivery-gate awareness), ADR 0090
  (require-gates cannot end in a silent green run), ADR 0094 (auto-run
  missing/stale required receipts; the `verification_autorun` trail)
- Supersedes: nothing. This ADR is purely **additive** — it adds durable
  evidence trails and presentation, and changes no gate-selection, execution,
  policy, or routing decision.

## Context — the official gate signal was not auditable per event

After ADR 0094, a run carried an append-only `state.extras['verification_autorun']`
trail proving what the Stage 9 / correction auto-run did. But the **scheduled
gate hooks** (`gate_repair.run_gate_hook` for `before_phase` / `after_phase` /
`before_delivery`) left no comparable durable record of their routing decisions.
The DONE/HALTED summary and any live view had to **infer** a gate's status from
the on-disk receipt directory, which cannot distinguish three very different
states that produce the same `present` receipt:

- a gate that **ran and passed on this hook** (`executed_pass`);
- a gate whose receipt was already **fresh, so this hook never executed it**
  (`skipped_fresh`); and
- a required gate **withheld** as manual/operator-only (`skipped_manual`).

Reading "present on disk → ran/pass" silently turned a fresh-but-unexecuted gate
into a false "ran/pass", and made operator questions un-answerable without
reading raw JSON: *"was this gate scheduled but skipped because fresh?"*, *"did a
gate run but its receipt go missing?"*, *"did a test run only in the transcript,
never as an official receipt?"*.

The fix had a hard constraint: it must be **observational only**. The gate
routing, the set of commands executed, the `GateRepairOutcome`, `sdk.verify`, and
gate-selection must all stay byte-identical — this is auditing, not new policy.

## Decision

Add two **additive, append-only** durable trails and an aggregation/presentation
layer over them. Every change is read-only with respect to routing and execution.

### 1. Autorun event identity (additive enrichment)

`verification_autorun.py::_record_autorun_evidence` enriches each appended trail
entry with two additive keys — `phase` and a derived `source`
(`stage9_autorun` / `correction_pre_review` / `gate_rerun`, from
`_autorun_source(phase, reason)`). `ReceiptAutoRunResult.to_evidence()` and its
ADR 0094 key set are **unchanged** (a compat test pins them); the enrichment is
applied to the recorded dict, not to `to_evidence()`. The correction
`gate_rerun` route already records through the same sink (`phase=correction_triage`
→ `source=gate_rerun`).

### 2. Scheduled-gate routing-decision recorder (append-only)

`gate_repair.py` records one decision per scheduled gate per hook firing into a
new append-only list `state.extras['verification_gate_events']`. Each record is
`{hook, phase, command, gate_set, decision, exit_code, receipt_path}` where
`decision ∈ {executed_pass, executed_fail, skipped_fresh, skipped_manual}`:

- `_record_executed_gate_event` stamps `executed_pass` / `executed_fail`
  **inline**, right after `_run_gate_command` runs a gate (proof the hook
  executed it — never inferred from disk). The pass/fail split is the
  **authoritative receipt rollup** (`gate_repair._passed` →
  `verification_receipt.command_receipt_passed`): exit code `0` AND every
  declared assertion passed AND an empty execution `detail` — the same rollup
  readiness/delivery enforce. An exit-0 receipt with a failed assertion or a
  non-empty detail is `executed_fail`, never a false-green `executed_pass`. The
  autorun trail's `failed` set is authoritative the same way: it is re-read from
  the post-run `classify_required_receipts` pass, so an exit-0 assertion/detail
  failure lands in `failed` (→ `ran/fail`) and never renders as `ran/pass`.
- `_reconcile_skipped_gate_events` runs a per-hook reconciliation over the plan's
  entries that routing did **not** execute and records the skip decision from
  durable facts only (`_skip_decision_for`): a command withheld as
  manual/operator-only → `skipped_manual`; a fresh `present` receipt (via the
  read-only `classify_required_receipts` that readiness/delivery use) →
  `skipped_fresh`; `missing`/`stale` → **no hook event** (it stays run-level
  residual, never a per-hook skip).

The recorder is strictly append-only and tolerant of stub runs. `run_gate_hook`
threads an `executed` set and calls the recorder on both exit paths, but the
routing decisions (`_run_gate_command`, `_route_failed_gate`), the executed
command set, and the returned `GateRepairOutcome` are unchanged — a unit test
pins `GateRepairOutcome` byte-identity with the recorder active.

### 3. Per-event timeline aggregation

`verification_timeline.py::build_verification_timeline` merges the two trails into
one ordered `VerificationTimeline` of `VerificationGateEvent`s plus run-level
residual and on-disk receipt names. The **invariant**: a scheduled gate's status
is classified **only** from its recorded routing decision —
`executed_pass→ran/pass`, `executed_fail→ran/fail`, `skipped_fresh→skipped_fresh`,
`skipped_manual→skipped_manual`. ran/pass is **never** inferred from an on-disk
receipt, so a fresh receipt with no execution this hook is a `skipped_fresh`
event, not ran/pass and not an absent event. Run-level residual
(`missing`/`stale`) comes from a separate readiness reclassification and never
overrides a hook decision. The aggregate omits (`None`) when there is no
contract, receipt, autorun trail, or gate-event.

### 4. Presentation (DONE + live), evidence-only

- **DONE/HALTED.** `render_verification_gate_done_block(timeline)` renders a
  compact per-hook block: `events: N official gate events`, one count-first line
  per event over its non-empty buckets (`ran/pass`, `ran/fail`, `skipped fresh`,
  `skipped manual`), a `receipts:` line, and a `residual: missing=… stale=…` line
  when proof is open. Wired through `finalization.py`
  (`FinalizationResult.verification_gate_lines`), guarded on `output_dir`.
- **Live terminal.** `render_gate_live_block` (autorun) and the new
  `render_scheduled_gate_live_block(events, hook_label=…)` (scheduled, classified
  only from the hook's recorded decisions) print as separate framed
  `+-- Official verification gates` blocks. `run.py` captures the
  `verification_gate_events` **delta** around each gate seam
  (`evaluate_pre_phase_gates` / `evaluate_post_phase_gates`) and prints one block
  per hook — TERMINAL-only, no-op without decisions. Printing is read-only over
  recorded evidence; `gate_repair` execution is untouched.

### 5. Official receipt vs ad-hoc transcript (channel distinction)

`verification_readiness.py` adds a single `transcript_not_proof_note()` helper.
When a required receipt is missing/stale **and** exploratory (ad-hoc transcript)
commands were observed, `render_readiness_block` emits `official receipt missing;
transcript commands are not accepted as proof`, with the official `orcho verify`
commands kept as remediation, never as proof. The fully-ready / present /
no-exploratory blocks are byte-identical; final-acceptance and correction/review
share the same render path, so the wording has one source of truth.

## Consequences

- The official gate signal is **auditable per event**: the timeline answers
  "scheduled but skipped fresh", "ran but receipt missing", and "transcript
  command vs official receipt" without reading raw JSON.
- **No behavior change.** Gate-selection, `sdk.verify`, gate policy, scheduled
  routing (`run_gate_hook` / `_route_failed_gate`), the executed command set, and
  `GateRepairOutcome` are unchanged — the recorder/reconciler only append
  evidence (pinned by tests).
- **No wire/schema change.** `verification_autorun` (enriched additively) and
  `verification_gate_events` are in-process `state.extras` evidence keys, not
  serialized contract fields; the ADR 0094 autorun `to_evidence()` keys are
  frozen; the verification-contract schema and `orcho verify` CLI are unchanged.
- Live and DONE blocks are omitted when there is no contract and no
  receipts/trail/gate-events, so an empty/misleading block is never printed.
