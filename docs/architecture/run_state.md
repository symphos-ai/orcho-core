# Run State

> Architecture reference for the `pipeline/run_state` layer. See
> [ADR 0075](../adr/0075-event-sourced-run-state-and-terminal-writes.md)
> for the decision record and
> [ADR 0035](../adr/0035-terminal-status-and-resume-observability.md) for the
> terminal-status / resume-observability invariant this layer enforces. For
> the full status and transition contract, see
> [run_state_machine.md](run_state_machine.md). For how a fresh-process resume
> restores durable phase outputs into runtime state, see
> [Resume-artifact bootstrap](#resume-artifact-bootstrap) and
> [ADR 0079](../adr/0079-resume-artifact-bootstrap.md).

A run records its lifecycle in two complementary stores, and `run_state` is
the layer that keeps them coherent.

## Events are the source; `meta.json` is a cache

1. **Event stream (`events.jsonl`)** — the durable, append-only record of
   what happened: phases started and ended, the run paused on a phase
   handoff, it halted, failed, or was interrupted. This is the **source of
   truth** for a run's lifecycle.
2. **`meta.json`** — a flat, materialised **snapshot** of that truth, mutated
   in place as the run progresses so readers (dashboards, resume gates,
   status APIs) get the current state in one read without folding the whole
   stream.

Because the snapshot is mutated live while the stream is the authority, the
two can drift — for example when a process is killed after a halt decision is
recorded but before the snapshot's `status` flips. The `run_state` layer
exists to project, check, and (when asked) repair that relationship.

## Three responsibilities

The layer is split into isolated parts, each with a single job:

- **Projection + consistency (read-only).** A pure reducer folds the event
  stream into a typed snapshot; a projector runs it over a run directory; a
  consistency checker names known torn shapes by stable problem codes. This
  part never writes and is never imported by runtime / resume / finalization
  paths.
- **Repair (opt-in, off-line).** `repair_run_state` consumes the consistency
  diagnosis and, for a strictly limited set of self-healable shapes, proposes
  (dry-run by default) or applies a minimal, crash-safe `meta.json` mutation
  that brings the snapshot back in line with the event-derived projection.
  Repairs are idempotent and add no durable schema beyond a repair-audit
  artifact. An interrupted run that still carries an undecided handoff is
  **refused** — that needs an operator decision, not an automatic flip.
- **State-transition helpers.** Focused writers own active handoff transitions,
  single-project terminal transitions, and cross terminal settlement in a run's
  flat state mapping (see below).
- **Terminal-outcome reduction (ADR 0115).** A pure reducer,
  `pipeline/run_state/terminal_outcome.py`, is the single home for the
  status-flip *decisions* finalization applies: `resolve_terminal_outcome(...)`
  (pre-delivery: state-halt → `halted`, plan-only work kind →
  `awaiting_human_review`, else `done`) and `apply_no_diff_terminal(...)`
  (post-delivery no-diff reconcile: rejected-no-diff → `halted`,
  approved-no-diff → `done` with a marker). Like the writers, it does no I/O
  and emits no events; finalization routes through it instead of encoding
  flips inline.

## Checkpoint completeness = a successful outcome

> Clarifies the projection invariant of
> [ADR 0075](../adr/0075-event-sourced-run-state-and-terminal-writes.md): a
> `phase.end` event records that a phase *ended*, not that it *completed*.

A phase is a **completed checkpoint only when its outcome is a success
outcome** — never on the mere presence of a `phase.end` event. The single
authority is the pure, strict-allowlist classifier in
[`pipeline/run_state/phase_outcome.py`](../../pipeline/run_state/phase_outcome.py):

```
is_phase_checkpoint_success(outcome) is True  ⟺  outcome (normalized,
    case-insensitive) == 'ok'  OR  starts with 'skipped'
```

Everything else is **not** a completed checkpoint: `halted: …`, `failed`,
`rejected`, `error`, `incomplete`, `no_verdict`, any operator-handoff-required
token, the synthetic `DONE`, any unknown string, `None`, and `''`. The
allowlist fails safe by design — a *new* terminal/handoff outcome is treated as
not-completed until it is deliberately added, rather than silently counted as a
completion. (The audit behind the allowlist: every project checkpoint phase
emits its `phase.end` outcome through
`pipeline/project/profile_dispatch.py::emit_phase_log_end`, which produces only
`ok` / `skipped: …` / `halted: …`; the `HYPOTHESIS` prelude and the `cross_*`
writers use free-form phrases but are not project checkpoint phases.)

This one predicate gates three consumers, so a halted phase can never be
mistaken for a finished one:

| Consumer | Module | Effect of the invariant |
|---|---|---|
| **Reducer projection** | `pipeline/run_state/reducer.py::_phase_end` | Three branches: failed token → `failed_phases`; success outcome → `completed_phases`; anything else (halted/incomplete/handoff/unknown) → **neither** (the phase stays only in `seen_phases`). |
| **Checkpoint / resume authority** | `pipeline/lifecycle.py::_persist_halted_step` | The halt seam fires the session adapter + metrics but **not** the checkpoint, so a halted phase never enters `ckpt.completed`; `should_skip(name)` stays `False` and resume re-enters it instead of skipping ahead. |
| **DONE summary** | `pipeline/project/finalization.py::_render_done_summary` | The halted phase's chip renders `=halt`, never `=ok`; the same `summary_text` feeds the `run.end` payload and the `DONE` `phase.end` title. |

The bug this closes: a halted IMPLEMENT was projected/checkpointed as completed
and resume silently skipped it straight to review against partial work.

## Partial subtask-DAG resume is unsupported

A plain checkpoint-resume into a **partially executed** subtask DAG (some
subtasks ran, the DAG halted mid-flight) is **not supported** and fails early
with an instructive diagnostic rather than continuing to review. The detection
is event-sourced and pure
([`pipeline/run_state/subtask_progress.py`](../../pipeline/run_state/subtask_progress.py)):
folding `events.jsonl`, a subtask is **unfinished** when its last event is a
`subtask.start` with no following `subtask.end`, or a `subtask.end` whose `ok`
is not `True` (i.e. no DONE/ATTESTATION close). Classification is
last-write-wins per `subtask_id`, so a later successful retry clears it and a
fresh run (no subtask events) is inert.

On resume, `pipeline/phases/builtin/subtask_dag.py::_run_subtask_dag_implement`
checks this **after the dry-run shortcut and before the DAG runs**. When the
run is not on the supported retry path and the detector returns a non-empty
set, it `state.stop`s with:

> `Cannot resume IMPLEMENT from partial subtask DAG state: subtask <id> started
> but has no DONE/ATTESTATION event. Start a follow-up or rerun implement after
> repair.`

**The only supported partial-resume is the ADR 0073 `retry_feedback` path**
(`state.extras['implement_retry']`), which deliberately re-runs the incomplete
ids with the prior partial state as read-only context. That path — and a fresh
run, and `dry_run` — never trigger the diagnostic.

The executable contract for both invariants lives in
`tests/unit/pipeline/run_state/test_phase_outcome.py` (classifier),
`test_reducer.py` (projection branches),
`tests/unit/pipeline/lifecycle/test_execute_step.py` (halt seam),
`tests/unit/pipeline/orchestrator/test_done_summary.py` (halt chip),
`test_subtask_progress.py` + `tests/unit/pipeline/phases/test_subtask_dag_resume_diagnostic.py`
(detector + resume guard), and the
`test_halted_phase_checkpoint.py` regression for the run `20260609_125615`
shape. This work touches only run-state projection, the checkpoint seam, the
DONE summary, and the IMPLEMENT resume guard — **no** wire-format, profile,
mode, or gate primitive changes — so no `orcho-mcp` update or E2E mock smoke is
required.

## Terminal-write helpers

`pipeline/run_state/terminal.py` provides pure, in-place helpers that mutate an
arbitrary mapping — the same flat top-level shape whether it is the in-memory
session dict or a `meta.json` body loaded off disk:

| Helper | Sets | `phase_handoff` |
|---|---|---|
| `mark_run_done(state)` | `status='done'` | cleared |
| `mark_run_halted(state, *, halt_reason, halted_at=None)` | `status='halted'`, `halt_reason`, optional `halted_at` | cleared |
| `mark_run_awaiting_review(state)` | `status='awaiting_human_review'` — the plan-only (planning/research) pause-for-review terminal; writes no `halt_reason` | preserved |
| `mark_run_failed(state, *, halt_reason)` | `status='failed'`, `halt_reason` | preserved |
| `mark_run_stalled(state, *, halt_reason)` | `status='failed'`, `halt_reason` — the stalled-command escalation (ADR 0103); distinct name gives the stall path a greppable home | preserved |
| `mark_run_interrupted(state, *, interrupted_at, halt_reason='interrupted')` | `status='interrupted'`, `interrupted_at`, `halt_reason` | preserved |
| `settle_cross_terminal(state, *, status, halt_reason=None, halted_at=None)` | cross terminal `status`, optional `halt_reason` / `halted_at` | cleared |

They do **no** file IO, emit **no** events, and touch **no** checkpoint:
persistence, the `run.end` event, and checkpoint status stay with the caller,
so a helper can never double-write or reorder the run-end boundary. The
module depends on nothing and never imports runtime / resume / finalization
code.

The same module owns settle-time residue eviction (ADR 0115): a settled
terminal (`done` / `halted`) evicts the canonical `TRANSIENT_SETTLE_KEYS`
tuple (`phase_handoff`, `halt`, `halt_reason`, `halted_at`,
`rejected_outcome`, `delivery_override`, `no_op_outcome`,
`correction_fixed_point`) via `evict_transient_settle_keys(...)`, and cross
runs have two disjoint canonical sets — `evict_cross_settle_residue(...)`
(settle-only) and `evict_cross_handoff_markers(...)` (handoff-marker keys) —
so "which residue is stale when" is a named contract, not per-call-site
judgment.

### Stale-handoff policy

This is the load-bearing rule the helpers encode:

- **`done` and `halted` are settled terminals.** Any lingering active
  `phase_handoff` is stale and is cleared.
- **`failed` and `interrupted` preserve an active `phase_handoff`.** A run
  that failed or was interrupted while carrying an undecided handoff still
  needs an operator decision; the repair layer deliberately refuses to flip
  it, so the helpers must not erase the state the operator has to act on.
- **`awaiting_human_review` is a pause, not a settled terminal.** The
  plan-only tail produced an artifact for a human to sign off; the operator
  decision is still ahead, so the active `phase_handoff` must survive —
  clearing it would erase exactly the state the reviewer acts on.

The shape `mark_run_halted` writes for `halt_reason='phase_handoff_halt'`
matches byte-for-byte what `repair_run_state` heals a torn halt to. The live
halt writer and the off-line repair are therefore **one** source of the
post-halt shape, not two that can drift.

`mark_run_halted` is also the writer for the correction non-convergence outcome
([ADR 0098](../adr/0098-correction-fixed-point-guard.md)): when a correction
follow-up round repeats the same `final_acceptance` blockers with no relevant
progress, the driver re-marks the child with
`halt_reason='correction_not_converging'` and records a durable
`session['correction_fixed_point']` block
(`{repeated, parent_run_id, child_run_id, suggested_actions, reason}`). The run
stays a terminal `halted` — it is **not** a `done`/approved delivery — so the
`reducer.py` status mapping is unchanged; only the `halt_reason` string and the
additive session block are new. See
[phase_lifecycle.md](./phase_lifecycle.md) for the firing condition.

### Where the helpers are called

The helpers are wired into the minimal safe set of terminal lifecycle sites
for this stage: finalize status resolution (`done` / `halted`), the
phase-handoff halt paths (torn-halt heal and the in-process halt sync), the
halt transition in the run-control API (mutating the loaded `meta` before it
is written back), the phase-failure path, the four delivery-halt branches,
and the atexit-interrupted hook. The pre-phase isolation-setup halts in
`pipeline/project/isolation_setup.py` — the pre-run-dirty intake/seed and
worktree-bootstrap failure paths (`pre_run_dirty_halt`,
`pre_run_dirty_seed_failed`, `worktree_bootstrap_failed`) — now flow through
`mark_run_halted` as well, so a stale active `phase_handoff` is cleared on
those settled halts too.

Active phase-handoff writes — pause, continue, retry-feedback, and
continue-with-waiver, which set `status='awaiting_phase_handoff'` /
`status='running'` and manage the active handoff payload — are **not**
terminal transitions, so the terminal helpers never touch them. They now
flow through a focused sibling, `pipeline/run_state/handoff.py` (see below),
rather than being open-coded in the orchestration. The terminal helpers own
terminal transitions only.

## Durable failure classification: `session['failure'].failure_kind`

When a run terminates `failed`, the phase-failure handler
(`pipeline/project/run.py::_record_phase_failure`) records a structured
`session['failure']` block. Beyond the always-present `phase` / `error` /
`type` / `ts`, the handler classifies the failure into a stable, provider-neutral
`failure_kind` (spread into both `session['failure']` and the `run.end` event)
so a captain / MCP client picks a **safe next action** instead of guessing — and
so a recoverable provider/runtime hiccup is never confused with a code, test, or
review rejection.

Classification is driven **only** by `isinstance` over the already-typed
exception taxonomy in `core/io/retry.py` — never by re-parsing provider strings.
Branch precedence in `_failure_metadata_for_exception` is load-bearing:
stalled-command → `provider_access` → `provider_runtime` → generic excerpt → `{}`
(`AgentAccessError` is a subclass of `AgentCallError`, so it must resolve to
`provider_access` before the `provider_runtime` branch).

| `failure_kind` | `recoverable` | `recommended_action` | Operator-message source | Exception set / trigger | ADR |
|---|---|---|---|---|---|
| `provider_access` | `False` | `switch_runtime_or_restore_access` | `provider_access_detail()` (sanitized provider-access channel) | `AgentAccessError` — runtime cannot reach the provider surface | [0101](../adr/0101-provider-access-recovery-and-runtime-override.md) |
| `provider_runtime` | `True` | `resume_or_retry_phase` | `provider_message` from `sanitized_failure_excerpt()` (omitted when empty) | `RateLimitError`, `ApiConnectionError`, `ApiTimeoutError`, `SystemResourceError` — transient usage/session/transport/local-resource condition past the retry budget | [0118](../adr/0118-provider-runtime-failure-classification.md) |
| `stalled_command` | `True` | `interrupt_resume_or_halt` | bounded `command_preview` / `output_tail` / `reason` (sanitized carrier) | `AgentCommandStalledError` — idle-timeout escalation of a hung child command | [0103](../adr/0103-stalled-command-diagnostics-and-recovery.md) |

A generic `AgentCallError` (and `AgentAuthenticationError` /
`ContextOverflowError` — auth/prompt forms, **not** usage/session/transport)
carries no `failure_kind`; it persists only a sanitized `stderr_excerpt`.

**`provider_runtime` is recoverable, not a verdict rejection.** It marks a
terminal `failed` run (status stays `failed`; `mark_run_failed` is unchanged),
but `recoverable=True` / `recommended_action='resume_or_retry_phase'` are
declarative metadata only — they add no retry loop or MCP tool and do not change
run control flow. Its terminal presentation is the red FAILED banner in
`run.py`; it never flows through the DONE / correction summaries in
`pipeline/project/finalization.py`, and the `final_acceptance` / `validate_plan`
**verdict**-`reject` chip stays reserved for a genuine code/test/review
rejection. The sanitary boundary is [ADR 0101](../adr/0101-provider-access-recovery-and-runtime-override.md):
`provider_message` is taken strictly from `sanitized_failure_excerpt()`, so no
raw JSONL / secrets / prompt text reaches an operator-visible or durable field.

A related but distinct value is the **evidence-synthesized** `setup_failed`
breadcrumb ([ADR 0104](../adr/0104-setup-preflight-terminal-state-projection.md)):
it is a `kind` in the evidence `errors` slice for a run that died during
setup/preflight before any phase ran, not a `session['failure'].failure_kind`
written by the phase-failure handler.

The typed SDK projection of these records lives in `sdk/evidence_slices.py`
(`ErrorsAndHalt.recovery` for `provider_access`, `ErrorsAndHalt.provider_runtime`
for `provider_runtime`, `list_stall_recovery()` for `stalled_command`).

## Active phase-handoff transition writers

`pipeline/run_state/handoff.py` is the active-transition counterpart to
`terminal.py`: the field-level mutation for the transitions that keep a run
**alive** across a phase-handoff pause. It owns the canonical transitions an
operator decision resolves to — and explicitly **not** halt.

| Helper | `status` | `phase_handoff` payload | Returns |
|---|---|---|---|
| `request_active_handoff(state, *, payload)` | `awaiting_phase_handoff` | set to `payload` | `None` |
| `clear_active_handoff(state)` | `running` | cleared | `None` |
| `continue_handoff(...)` | `running` | cleared | `HandoffTransition` (override) |
| `continue_with_waiver_handoff(...)` | `running` | cleared | `HandoffTransition` (override + waiver) |
| `retry_feedback_handoff(...)` | `running` | cleared | `HandoffTransition` (override + human_feedback + typed `retry_mode`) |

Two shapes of state, two return contracts. The `status` field and the active
`phase_handoff` payload live on the flat top-level mapping and are mutated
**in place**, exactly like `terminal.py`. The derived
`phase_handoff_override` / `phase_handoff_waiver` / `human_feedback` markers
live on a *separate* object (`state.extras` / the session under other keys),
which a mutator here cannot reach — so the transition functions **return**
those dicts wrapped in a `HandoffTransition` for the caller to place. The
pure builders (`build_handoff_payload`, `build_phase_handoff_override`,
`build_phase_handoff_waiver`, `build_human_feedback`) are the single home for
each marker's byte-equivalent shape and key order.

A `retry_feedback` transition carries a typed `HandoffRetryMode`
(`PLAN` vs `REPAIR`) so the caller dispatches the correct loop **without**
parsing the paused phase string.

Like `terminal.py`, this module does **no** file IO, spawns **no**
subprocess, calls **no** provider, renders **no** prompt, and prints nothing
— persistence, events, checkpoint status, and round dispatch stay with the
caller. It depends only on `pipeline/run_state/types.py`.

**Halt stays terminal.** `handoff.py` never writes `status='halted'`. Every
phase-handoff halt path — the torn-halt heal and the in-process halt sync in
the handoff orchestration, and `sdk/phase_handoff.py` — continues to go
through `mark_run_halted` in `terminal.py`, the single source of the
post-halt shape. Folding halt into the active-transition module would create
a second writer that could drift from what `repair_run_state` heals to.

New terminal lifecycle paths should call these helpers. If a future caller
needs a shape not covered here, add a focused helper instead of open-coding a
new terminal mutation.

## Advisory handoff artifacts: `phase_handoff_advice/`

The interactive TTY handoff menu can offer two **UI pseudo-actions** —
`advice` and `retry_with_advice` — on top of the four canonical actions when
the pause is rejected/incomplete-eligible (see
[ADR 0124](../adr/0124-handoff-advice-stage0.md)). These are not canonical
actions: they never enter `available_actions`, are never accepted by
`phase_handoff_decide`, and never appear in a decision artifact's `action`
field.

The read-only advisor persists its recommendation to a **separate** run-state
directory, **never** `phase_handoff_decisions/`:

- `<run_dir>/phase_handoff_advice/<safe_handoff_id>.json` — one directory per
  run, one file per handoff, keyed by the same collision-resistant
  `safe_handoff_id` slug the decision artifacts use. Fields: `run_id`,
  `handoff_id`, `phase`, `created_at`, `advice` (`recommended_action`,
  `confidence`, `rationale`, `retry_feedback`, `risks`, `expected_files`,
  `operator_note`, `parse_warnings`), `raw_output`, `usage`.
- **Divergent versions never overwrite.** A repeat write with identical advice
  returns the same path; a different advice for the same handoff (a second
  advisory pass, or operator-edited feedback) is written to an attempt-suffixed
  file (`<safe_id>_2.json`, `<safe_id>_3.json`, …). Earlier advisor and
  human-authored artifacts are left intact.

**Provenance lives on the decision `note`, not in a new decision field.** When
an advisory recommendation is applied, the resulting durable `retry_feedback`
decision flows through the unchanged `phase_handoff_decide` path and records its
provenance in the free-text `note`:

```
feedback_source=agent_advice; advice_artifact=<actual relative path>
```

The path is always the one the advice-artifact write returned — never a
recomputed name — so the decision references the exact advice object its
feedback came from, including the divergent/edited cases. Encoding provenance in
`note` keeps the decision artifact's wire shape (and the strict
`_read_existing_strict` reader) unchanged and requires no `orcho-mcp` update in
this stage.

## Operator CLI: `orcho repair-state`

The repair API has a first-class operator verb:

```
orcho repair-state RUN_ID [--apply] [--json] [--workspace PATH]
```

It resolves `RUN_ID` through the same workspace / run-directory model as
`orcho status`, `orcho diff`, and `orcho evidence` (explicit `--workspace`,
else `$ORCHO_WORKSPACE` / cwd walk-up), then delegates to `repair_run_state`
with the only supported policy, `action='safe'`. The CLI owns no repair
policy of its own — what is repairable is decided entirely by the repair
layer.

- **Dry-run is the default.** Without `--apply` the command diagnoses the run
  and prints the current status, the run directory, the diagnosed issue
  codes, and the proposed `meta.json` changes — and writes nothing (no
  `run_state_repairs/` directory is created).
- **`--apply`** applies only the already-supported safe repairs, crash-safely
  (backup → atomic `meta.json` replace → audit artifact), and prints the
  backup/audit paths. Repairs are idempotent: a second `--apply` is a no-op
  that writes nothing.
- **`--json`** emits a single stable machine-readable object on stdout (run
  id, run dir, action, apply-requested/applied flags, issue codes, proposed
  changes, `needs_operator_decision`, repair hint, backup/audit paths,
  repaired-at), with errors kept on stderr.

**It refuses to flip an active, undecided handoff.** An interrupted run still
carrying an active `phase_handoff` with no recorded decision is reported as
`needs_operator_decision` (no mutation, even with `--apply`); the operator
must resolve the handoff through the decision API (halt/continue) before
resuming, rather than have status flipped automatically.

## Resume-artifact bootstrap

> Decision record: [ADR 0079](../adr/0079-resume-artifact-bootstrap.md).

**Invariant.** If a resume leaves a phase behind, that phase's durable outputs
are restored into the runtime `PipelineState` **before any dependent phase
runs** — without re-running the producing phase, and without degrading to a
non-round-trip-safe source.

A fresh-process resume (MCP / Web, or a checkpoint resume launched as a new
subprocess) has none of the original launch's in-memory state. The producing
phase already wrote its durable artifact to the run directory and is **not**
re-run, so a dependent phase would otherwise see an empty value.

```
  normal run:   phase executes ──▶ writes artifact ──▶ projects onto state
  resume:       phase skipped  ──▶ bootstrap loads  ──▶ projects onto state
```

### Persisted source: `completed`-only

[`pipeline/checkpoint.py`](../../pipeline/checkpoint.py) persists an
**append-only `completed` list** of phase names. There is **no** separate
`skipped` set — `should_skip(name) == name in completed`. So the durable,
observable required-signal is simply **`'plan' in completed`**: it means a prior
process already finished PLAN, so the durable `parsed_plan.json` must be
recoverable.

`session_run.py` builds `resume_completed_phases` from
`ckpt.load(session_ts).completed` **only for an actual resume**
(`request.resume_from` truthy); a fresh run carries the empty set, which makes
the bootstrap a strict no-op (no marker, no provenance, no mutation).

For an **in-process handoff resume** the plan / validate_plan phases are marked
completed *later* in the same process — after `state` was built — so
`checkpoint.completed` does not yet carry them at state-build time. That path
carries the same required-signal in memory via the owned marker instead (set at
the three handoff strip/retry sites).

### Marker contract: `RESUME_PLAN_REQUIRED_KEY`

`state.extras['resume_plan_required']` (a bool) is the single in-memory
required-signal that the implement guard reads.

| Role | Module |
|---|---|
| **Owner of the name** | [`pipeline/project/resume_artifacts.py`](../../pipeline/project/resume_artifacts.py) exports `RESUME_PLAN_REQUIRED_KEY` |
| **Writers** | `pipeline/project/state_setup.py` (via the bootstrap runner) and `pipeline/project/handoff.py` (the three in-process strip/retry sites) |
| **Reader** | `pipeline/phases/builtin/subtask_dag.py` (the empty-plan guard) |

The bootstrap also records **provenance** under
`state.extras['resume_artifacts'][name]`: `{'source': 'artifact'}` on a
successful load, and `{'status': 'missing' | 'corrupt'}` on a *required*
failure. Optional failures write nothing, so non-resume paths stay
byte-identical. When `state.parsed_plan is None` **and** the marker is set, the
`subtask_dag` guard is the authoritative place for the operator error: it names
`<run_dir>/parsed_plan.json` and distinguishes *missing* from *unreadable
(corrupt)* via that provenance; without the marker it keeps the prior generic
message.

**`parsed_plan.json` stays the machine source of truth; the markdown fallback
is forbidden.** A corrupt `parsed_plan.json` is a `corrupt_*` category and
surfaces the guard error — it is **never** silently reconstructed from the human
`plan_<run>_r<n>.md` projection, which is not round-trip-safe (see
[`pipeline/plan_artifacts.py`](../../pipeline/plan_artifacts.py)).

### Future candidate specs

The runner is generic over a registry of `ResumeArtifactSpec` entries;
`parsed_plan` is the first. A future phase that produces a typed durable output
registers its own spec (a `required_when` predicate over the resume's
`completed_phases`) rather than re-deriving "required but absent" from a bare
`None` check.

| Candidate | Producer phase | Durable source | Runtime projection | Dependent phases | Spec registration |
|---|---|---|---|---|---|
| Parsed plan *(shipped)* | `plan` | `parsed_plan.json` | `state.parsed_plan` + `state.plan_markdown` | `implement` (subtask DAG) | `REGISTRY` in `resume_artifacts.py` |
| Verification receipts | verification gates | verification receipt artifact | `state.extras` receipt cache | later gates / delivery | new spec + `required_when` |
| Implement evidence | `implement` | implementation receipts | `state.extras` implement evidence | review / repair / acceptance | new spec + `required_when` |
| No-diff outcome | `implement` | persisted no-diff marker | `state` delivery flags | review / final acceptance | new spec + `required_when` |
| Handoff decision context | handoff resume | decision artifact + active payload | `state.extras` override / waiver | post-handoff dispatch | new spec + `required_when` |
| Prompt / session receipts | any phase | prompt-render / agent-session rows | `state.extras` session seeds | any resumed phase | new spec + `required_when` |

A future spec that needs a distinct "required but absent" operator signal
extends the marker contract above rather than spelling a new string literal
across modules.

## Retained worktree-subject resume

> Decision record:
> [ADR 0088](../adr/0088-review-retry-worktree-subject-continuity.md).

**Invariant.** A checkpoint-resume picks up the change it was reviewing from
the durable `meta.worktree` block, **not** from a worktree path re-derived from
the resumed run id. A run paused after `review_changes` rejected its change and
decided `retry_feedback` must re-run `repair_changes` against the *same*
retained worktree that holds the rejected diff — even when the resumed run-dir
name has drifted from the original `wt_<id>` (incident `20260612_213530`).

Before `init_run_session` overwrites `meta.json`,
[`pipeline/project/resume_worktree.py`](../../pipeline/project/resume_worktree.py)
reads the prior `meta.worktree` block and classifies the resume:

| Class | Condition | Resume behaviour |
|---|---|---|
| passthrough | no block, or `isolation=off` | resolver behaves as before |
| reuse retained subject | recorded isolated worktree exists + registered | reuse that exact path via `resolve_worktree_for_run(resume_prior_worktree=…)`, never `wt_<run_id>` |
| unavailable + active review-retry | recorded worktree missing/unregistered **and** an active `review_changes` handoff / recorded `retry_feedback` decision | **recoverable** error naming the missing path, before any clean checkout |
| unavailable, generic resume | recorded worktree missing, no review-retry | passthrough — existing resolver fallback |

The hard error is scoped narrowly to the active review-retry branch; generic
resume, follow-up continuity, and cross-children paths are unchanged. A reuse
decision is recorded additively on the session worktree block as
`meta.worktree.resume_continuity` (`mode_label` / `path` / `source`) for
inspectability — existing `meta.worktree` consumers are unaffected.

**Clean-HEAD repair guard is recoverable.** Immediately before the review-retry
write phase,
[`pipeline/project/retry_subject.py`](../../pipeline/project/retry_subject.py)
proves the repair subject is present: for an isolated run the repair cwd must
match the recorded retained path **and** the tree must carry the diff (dirty
working tree, or `HEAD` moved off the recorded `source_start_head` / `base_ref`
— a committed diff counts); for an isolation-off run only the dirty/HEAD-shift
check applies. An unproven subject raises `RepairSubjectUnproven` with the
operator text *"Cannot run repair_changes against clean HEAD: review retry
requires the retained rejected diff subject. Resume/apply the retained worktree
diff or halt this run."* The guard is **read-only** and runs **before**
`retry_feedback_handoff` clears the active payload, so the abort is **not** a
torn write: `meta.phase_handoff` and the recorded decision survive, the run
stays decidable, and it can be resumed again once the retained worktree diff is
restored.

## Deferred

- **Verification environment provenance.** The state machine can now represent
  no-diff verification outcomes, implement attestation pauses, and terminal
  cleanup consistently. It does not yet own the richer verification environment
  policy (`CORE_UNDER_TEST`, scheduled gates, and receipt freshness). That
  belongs to the verification-contract work and should be documented beside the
  quality-gate policy when it lands.
