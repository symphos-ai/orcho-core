# Run State Machine

This is the living state-machine reference for Orcho run lifecycle state.
It is the contract reviewers and task authors should use when they need to
answer: "what state is this run in, what transition is allowed next, who owns
the write, and what torn shapes are repairable?"

For the layer overview, see [run_state.md](run_state.md). For the original
decision record, see
[ADR 0075](../adr/0075-event-sourced-run-state-and-terminal-writes.md).

## Durable Surfaces

The state machine is projected from several durable surfaces. They are not
equivalent:

| Surface | Role | Writer |
|---|---|---|
| `events.jsonl` | Append-only lifecycle history and projection source. | Runtime / phase orchestration |
| `meta.json` | Materialized current-state snapshot for cheap reads. | Project / cross lifecycle callers |
| `phase_handoff_decisions/*.json` | Operator decisions for active handoffs. | Handoff decision API / CLI / MCP |
| `checkpoint.json` | Project checkpoint and resume cursor. | Project checkpoint layer |
| `cross_checkpoint.json` | Cross-run pending handoff and child status surface. | Cross-project orchestration |
| `phase_log` / evidence artifacts | Per-phase outcomes, receipts, and review context. | Phase handlers / evidence layer |

`events.jsonl` is the source of truth for projected lifecycle history.
`meta.json` is a cache optimized for dashboards, resume gates, and status APIs.
The `pipeline.run_state` layer projects, validates, and repairs the relationship
between these surfaces; it does not own provider calls, prompt rendering, or
checkpoint persistence.

## Status Taxonomy

`pipeline.run_state.types.RunStatus` defines the known top-level statuses.

| Status | Class | Meaning | Normal resume behavior |
|---|---|---|---|
| `unknown` | seed / diagnostic | No projected lifecycle yet. | Not a runnable target by itself. |
| `running` | live | Pipeline is active or ready to continue. | Continue from checkpoint / phase loop. |
| `awaiting_phase_handoff` | operator pause | A phase handoff is active and needs a decision. | Require handoff decision before resume. |
| `awaiting_gate_decision` | operator pause | A gate decision is active. | Require gate decision before resume. |
| `awaiting_human_review` | operator pause | Human review is active. | Require review decision before resume. |
| `done` | settled terminal | Run completed successfully. | Do not resume as same run. Follow-up only. |
| `halted` | settled terminal | Operator or policy halted the run. | Do not resume as same run unless repaired by explicit policy. |
| `failed` | terminal / diagnostic | Run failed. Active handoff may be preserved for diagnosis. | No blind resume; inspect state. |
| `cancelled` | terminal / cross | Run was cancelled / aborted by contract. | Do not resume as same run. |
| `interrupted` | torn / diagnostic | Process ended before a clean terminal write. | Repair or operator decision required. |

Settled terminals (`done`, `halted`, `cancelled`, and cross terminal shapes)
must not carry an active handoff. Single-project `failed` and `interrupted`
intentionally preserve an active handoff because the operator may still need to
resolve it.

## Single-Project Transition Matrix

This table names the canonical transition, the writer that owns the flat
`meta.json` / session mutation, and the durable side effects the caller must
own. The writer never owns every side effect.

| From | Trigger | To | State writer | Active handoff | Caller-owned side effects |
|---|---|---|---|---|---|
| `unknown` | run starts | `running` | session setup / meta bootstrap | absent | `run.start`, checkpoint seed |
| `running` | phase starts / ends | `running` | phase orchestration | unchanged | `phase.start`, `phase.end`, checkpoint |
| `running` | phase requests handoff | `awaiting_phase_handoff` | `request_active_handoff` | set | `phase.handoff_requested`, checkpoint pause, `meta.json` save |
| `awaiting_phase_handoff` | operator `continue` | `running` | `continue_handoff` | cleared | decision artifact, override marker, checkpoint/resume dispatch |
| `awaiting_phase_handoff` | operator `continue_with_waiver` | `running` | `continue_with_waiver_handoff` | cleared | decision artifact, override + waiver markers, checkpoint/resume dispatch |
| `awaiting_phase_handoff` | operator `retry_feedback` | `running` | `retry_feedback_handoff` | cleared | decision artifact, human feedback marker, checkpoint invalidation, plan/repair loop dispatch |
| `awaiting_phase_handoff` | operator `halt` | `halted` | `mark_run_halted` | cleared | decision artifact, `run.end`, checkpoint terminal write |
| `running` | normal success | `done` | `mark_run_done` | cleared | `run.end`, checkpoint terminal write, final save |
| `running` | final acceptance no-diff halt | `halted` | `mark_run_halted` | cleared | `run.end`, checkpoint terminal write, no-diff outcome text |
| `running` | policy / delivery halt | `halted` | `mark_run_halted` | cleared | `run.end`, checkpoint terminal write, halt reason |
| `running` | phase failure | `failed` | `mark_run_failed` | preserved | `run.end`, checkpoint terminal write, failure evidence |
| any live status | process exit / atexit interruption | `interrupted` | `mark_run_interrupted` | preserved | best-effort save only |

Important boundaries:

- `handoff.py` never writes `halted`; halt is terminal and belongs to
  `terminal.py`.
- `terminal.py` never writes handoff override / waiver / human-feedback
  markers; it only mutates the flat top-level state mapping.
- Checkpoint writes, event emission, `meta.json` persistence, and retry loop
  dispatch stay with the lifecycle caller.

### Review-retry resume reads the retained worktree subject

The `awaiting_phase_handoff → running` (`retry_feedback`) transition for a
`review_changes` pause is **subject-aware** (ADR 0088). Before the transition
fires, the resume rehydrates the retained worktree from the durable
`meta.worktree` block instead of re-deriving a `wt_<run_id>` checkout, so
`repair_changes` re-runs against the rejected diff even when the resumed
run-dir name has drifted from the original worktree id. Two recoverable guards
sit **before** the payload-clearing transition, so neither produces a torn run:

- **Retained subject unavailable + active review-retry** — if the recorded
  worktree is missing/unregistered, resume aborts with a recoverable operator
  error naming the path, *before* any clean checkout is created. Scoped to the
  active review-retry branch only; generic resume keeps the existing resolver
  fallback.
- **Clean-HEAD repair guard** — if the repair cwd has no rejected diff (clean
  HEAD on the recorded base, or a cwd that does not match the retained path),
  resume raises `RepairSubjectUnproven` *before* dispatching the write phase.

Both guards are read-only and run before `retry_feedback_handoff` clears the
active payload, so `meta.phase_handoff` and the recorded decision survive: the
run stays decidable (`awaiting_phase_handoff` / torn-but-decidable
`interrupted`) and is resumable again once the retained worktree diff is
restored. See [run_state.md](run_state.md) → "Retained worktree-subject
resume".

## Cross-Run Transition Matrix

Cross runs share the same durable `meta.json` status field, but have an
additional `cross_checkpoint.json` surface for pending handoff kind, pending
gate, and child aliases.

| From | Trigger | To | State writer / classifier | Active handoff | Notes |
|---|---|---|---|---|---|
| live cross run | cross/project/CFA handoff pending | pending state | cross checkpoint writer | set in `meta.json` and checkpoint | `phase_handoff_kind` is dispatch authority. |
| cross terminal with stale handoff | finalization / repair | same terminal | `settle_cross_terminal` / `repair_cross_run_state` | cleared | Safe only when checkpoint is not still pending. |
| live cross run | child/project completion | live or terminal | cross finalization | checkpoint updated | Per-alias `sub_status` remains cross-owned. |
| live cross run | contract abort | `cancelled` | `settle_cross_terminal` | cleared | `cancelled` is an existing cross terminal. |
| live cross run | cross failure / halt / done | `failed` / `halted` / `done` | `settle_cross_terminal` | cleared | Cross terminal payloads never require an active operator handoff. |

Cross differs from single-project failure policy: a terminal cross run should
not preserve `phase_handoff`, even for `failed`. Cross pauses short-circuit the
run before terminal finalization; any handoff left after a cross terminal is
stale.

## Torn Shapes And Repair Policy

Single-project diagnosis is owned by `validate_run_state`; cross diagnosis is
owned by `validate_cross_run_state`. Repair consumes those diagnoses instead of
re-deriving them.

| Code | Severity | Surface contradiction | Safe repair |
|---|---|---|---|
| `halt_decision_without_halted_meta` | error | Halt decision exists but `meta.status` did not flip to `halted`. | `repair_run_state --apply` writes the post-halt shape. |
| `terminal_with_stale_handoff` | warning | `done` / `halted` carries active `phase_handoff`. | `repair_run_state --apply` clears the stale payload. |
| `meta_handoff_without_event` | error | `meta.phase_handoff` exists but no handoff event was projected. | No automatic repair. |
| `active_handoff_without_decision` | info | Active handoff exists and no decision artifact exists yet. | No repair; operator decision may be required. |
| `interrupted_with_active_handoff` | warning | Interrupted run still carries active handoff. | No automatic status flip; decide handoff first. |
| `cross_terminal_with_stale_handoff` | warning | Cross terminal carries stale `meta.phase_handoff`. | `repair_cross_run_state --apply` clears it only when checkpoint is not pending. |
| `checkpoint_pending_without_active_handoff` | error | Cross checkpoint says pending but `meta.phase_handoff` is absent. | No safe repair; requires operator/checkpoint decision. |
| `active_handoff_without_checkpoint_pending` | warning | Cross `meta.phase_handoff` exists but checkpoint is not pending. | Diagnostic only. |
| `checkpoint_kind_id_mismatch` | warning | Cross kind and id prefix disagree. | Diagnostic only; kind is authoritative. |
| `project_handoff_marker_incomplete` | warning | Cross project marker lacks alias/child data. | Diagnostic only. |
| `cfa_pending_without_paused_state` | warning | CFA handoff pending but paused state is missing. | Diagnostic only. |
| `pending_gate_and_handoff_active` | warning | Cross checkpoint carries both pending gate and handoff. | Diagnostic only. |

Repair commands are intentionally conservative:

```bash
orcho repair-state RUN_ID --workspace PATH
orcho repair-state RUN_ID --workspace PATH --apply
orcho repair-state RUN_ID --workspace PATH --json
```

The command delegates policy to `repair_run_state`. Cross repair is a separate
API, `repair_cross_run_state`, used by cross-safe tooling and tests. Neither
repair path mutates provider sessions, replays phases, or invents operator
decisions.

## No-Diff Verification Runs

No-diff is not a lifecycle status, but it affects finalization and review
state:

- If a task was expected to produce a diff, `review_changes` and
  `final_acceptance` need an uncommitted review target.
- If a task is verification-only, no diff can be a valid outcome only when the
  implement phase recorded complete evidence for the required checks.
- If implement attestation is incomplete, downstream review/final acceptance
  must not convert the situation into a misleading "no uncommitted changes"
  success path. The run should pause on the implement handoff with the
  recorded incomplete evidence.

This distinction is why no-diff handling lives in finalization / phase handler
logic, while terminal status writes still flow through `mark_run_halted` or
`mark_run_done`.

## Phase Checkpoint Completion = Success Outcome

A `phase.end` event records that a phase *ended*, not that it *completed*. A
phase is a **completed checkpoint only when its outcome is a success outcome**,
classified by the strict allowlist in
`pipeline/run_state/phase_outcome.py::is_phase_checkpoint_success`: `True` only
for `ok` and `skipped*` (case-insensitive); everything else — `halted: …`,
`failed`, `rejected`, `error`, `incomplete`, `no_verdict`, any
operator-handoff-required token, the synthetic `DONE`, unknown strings, `None`,
`''` — is **not** a completed checkpoint. The allowlist fails safe: a new
terminal/handoff outcome is not-completed until deliberately added. This
clarifies the projection invariant of
[ADR 0075](../adr/0075-event-sourced-run-state-and-terminal-writes.md).

The invariant gates three consumers consistently:

| Consumer | Site | Halted/incomplete/handoff/unknown outcome |
|---|---|---|
| Reducer projection | `reducer.py::_phase_end` | enters **neither** `completed_phases` nor `failed_phases` (stays in `seen_phases`). |
| Checkpoint / resume authority | `lifecycle.py::_persist_halted_step` | not written to `ckpt.completed`; `should_skip(name)` stays `False`. |
| DONE summary | `finalization.py::_render_done_summary` | chip renders `=halt`, not `=ok` (shared by `run.end` payload + `DONE` title). |

This is why the `running → running` "phase starts / ends" row above checkpoints
only genuinely-completed phases: a halted phase has a `phase.end` but is not a
completed checkpoint.

### Partial subtask-DAG resume is unsupported

A plain checkpoint-resume into a partially executed subtask DAG fails early with
an instructive `state.stop` instead of silently continuing to review. The
detector (`pipeline/run_state/subtask_progress.py`) is pure and event-sourced: a
subtask is unfinished when its last event is a `subtask.start` with no following
`subtask.end`, or a `subtask.end` with `ok` not `True` (no DONE/ATTESTATION).
`subtask_dag.py::_run_subtask_dag_implement` runs this after the dry-run
shortcut and before the DAG; off the supported ADR 0073 `retry_feedback`
(`state.extras['implement_retry']`) path it stops with `Cannot resume IMPLEMENT
from partial subtask DAG state: …`. A fresh run and the retry path are inert.

**No wire-format / profile / mode / gate change.** This invariant work touches
only run-state projection, the checkpoint seam, the DONE summary, and an
IMPLEMENT resume guard — no runtime schema, profile shape, mode flag, or gate
primitive changes — so no `orcho-mcp` alignment or E2E mock smoke is required.

## Executable Spec

These tests are the executable contract for this document:

| Contract area | Tests |
|---|---|
| State classification matrix | `tests/unit/pipeline/run_state/test_state_matrix.py` |
| Transition matrix | `tests/unit/pipeline/run_state/test_transition_matrix.py` |
| Terminal writers | `tests/unit/pipeline/run_state/test_terminal.py` |
| Active handoff writers | `tests/unit/pipeline/run_state/test_handoff.py` |
| Cross state and terminal cleanup | `tests/unit/pipeline/run_state/test_cross.py` |
| Cross safe repair | `tests/unit/pipeline/run_state/test_cross_repair.py` |
| Checkpoint-success classifier | `tests/unit/pipeline/run_state/test_phase_outcome.py` |
| Reducer completed/failed/neither branches | `tests/unit/pipeline/run_state/test_reducer.py` |
| Halt seam (no checkpoint on halt) | `tests/unit/pipeline/lifecycle/test_execute_step.py` |
| DONE summary halt chip | `tests/unit/pipeline/orchestrator/test_done_summary.py` |
| Partial subtask-DAG detector | `tests/unit/pipeline/run_state/test_subtask_progress.py` |
| Partial subtask-DAG resume diagnostic | `tests/unit/pipeline/phases/test_subtask_dag_resume_diagnostic.py` |
| Halted-phase checkpoint regression (run 20260609_125615 shape) | `tests/unit/pipeline/run_state/test_halted_phase_checkpoint.py` |
| Project finalization terminal order | `tests/unit/pipeline/project/test_finalize_done_order.py` |
| Phase no-diff / implement-incomplete guards | `tests/unit/pipeline/phases/test_builtin.py` |
| Runtime handoff trigger behavior | `tests/unit/pipeline/runtime/test_handoff_trigger.py` |

When changing a state writer or diagnosis code, update this document and the
matrix tests in the same change. When changing only lifecycle wiring, keep the
matrix tests as the fast contract and add or update the relevant integration
guard around the real caller.
