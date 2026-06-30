# ADR 0073 — Implement-phase substance-repair → handoff fallback

- Status: Accepted
- Date: 2026-06-04
- Relates to: ADR 0067 (session-aware subtask_dag), ADR 0068 (subtask
  done-criteria attestation), ADR 0071 (attestation repair), ADR 0072
  (`continue_with_waiver` handoff action), ADR 0038 (cross phase-handoff
  lifecycle)
- Extends: ADR 0068. ADRs are append-only; this records the follow-on
  incomplete-delivery protocol rather than editing the original attestation gate.

## Context

ADR 0068 made a criteria-bearing `subtask_dag` subtask close with a typed
`subtask_attestation`. A subtask that returns but does not all-meet its
done-criteria is **INCOMPLETE**, and `subtask_dag` then **hard-halts** the whole
run (`state.stop("subtask_dag delivery blocked; ...")`). There is no middle path
between `done` and `[HALTED]`.

Observed pain: a subtask honestly failed an over-broad criterion ("full pytest
green") because of a **pre-existing, unrelated environment issue** (a stale
golden run dir), not its own code. The run dead-stopped with no recourse but a
manual restart, losing the thread. An honest INCOMPLETE attestation should be
recoverable, not fatal.

`implement` is a **bare top-level PhaseStep** — the first non-loop phase to carry
a handoff — so the existing loop-driven handoff machinery (validate_plan /
review_changes) does not apply directly; several seams need explicit non-loop
semantics.

## Decision

Add a **profile-configurable phase-handoff policy on the implement phase** that,
on an INCOMPLETE `subtask_dag` delivery, runs bounded auto-repair and then either
pauses for an operator or records an auto-waiver — instead of the hard stop.

### Policy
`PhaseHandoffPolicy` gains two fields (no new `PhaseHandoffType`):
- `repair_attempts: int = 0` — bounded automatic substance-repair rounds before
  the handoff fires.
- `on_exhausted: "halt" | "auto_waiver" = "halt"` — fallback once repair is spent.

Either non-default value requires an interactive `type` (`HUMAN_BYPASS` never
pauses). `advanced` enables `{human_feedback_on_reject, repair_attempts=1,
on_exhausted=auto_waiver}` on `implement`; `lite` and the other profiles keep no
handoff block → the legacy hard stop, unchanged.

### §1 — Non-loop handoff payload sentinels
The implement handoff raises a standard `PhaseHandoffRequested` with fixed
sentinels: `handoff_id="implement:implement_handoff:1"`,
`round_extras_key="implement_handoff"`, `round=1`, `loop_max_rounds=1`,
`trigger="incomplete"`, `verdict="INCOMPLETE"`, `approved=False`, full action set
`continue / retry_feedback / continue_with_waiver / halt`, and
`artifacts={findings, incomplete_subtasks, attestation_incomplete,
missing_subtask_receipts}`. The decide wire format is unchanged.

### §2 — Auto-waiver eligibility is an explicit, request-only flag
`ProjectRunRequest.auto_waiver_allowed` (request-only; NOT a `run_pipeline`
kwarg) is mirrored into `state.extras["auto_waiver_allowed"]` at state
construction so the handler (which sees only `state`) can read it. Auto-waiver
fires **iff** `on_exhausted=="auto_waiver"` AND the flag is True. MCP/headless
runs are `no_interactive=True` but the client is still the operator, so
eligibility is never inferred from TTY absence.

### §3 — `retry_feedback` resume re-runs only the unclosed subtasks
A `retry_feedback` decision seeds `state.extras["implement_retry"]` and
re-dispatches the implement handler, which narrows the executed DAG to the
**incomplete + missing-receipt** ids (a missing receipt is not "done", so it
re-runs rather than being treated as a satisfied dependency). The done subtasks
ride along as read-only context, **rebuilt from the persisted
`implementation_receipts` (attestation summary/error) on a fresh-process
resume** — the live agent output is gone, so the receipt-derived
`PriorSubtaskContext` is a degraded but real upstream view. The plan-rehydrate
helper was generalized (`rehydrate_parsed_plan_on_plan_loop_strip` →
`rehydrate_parsed_plan`) and is called on the implement retry branch so a
cold-process resume re-seeds `parsed_plan` from disk.

### §4 — Auto-waiver lifecycle (in-process, durable, idempotent)
The public `phase_handoff_decide` requires `status=="awaiting_phase_handoff"`;
the auto-waiver fires mid-implement while the run is still `running`, so it MUST
NOT call it. Instead a focused `pipeline/project/handoff_waiver.py` owns
`apply_waiver_to_state` (requires a non-empty rationale; `decided_by` is
applier-set) and a conflict-aware `sync_waiver_to_session`. Because the handler
sees only `state` (no run handle), the in-process auto-waiver is mirrored from
`state.extras` into the session at the **implement phase-end**
(`_PipelineRun._on_phase_end`), so it persists to `meta.phase_handoff_waiver` +
evidence (the operator-resume accept path syncs directly and skips implement, so
the two never collide). The synthetic decision artifact is written through a new
internal
`sdk.phase_handoff.record_decision_artifact(skip_status_guard=True)` that keeps
the public path's exact-payload idempotency/conflict checks. `decided_by` is
provenance (`operator` / `auto:on_exhausted`) carried on the waiver payload — it
does NOT travel through the decision-artifact bridge or its idempotency
comparison, so the artifact wire format is unchanged.

A bare `continue` carries no operator verdict; the resume arm **synthesizes** a
non-empty waiver rationale from the active findings (+ note) and records
`action="continue"`, so an unexplained "proceed" is never possible.

### §5 — `completed_phases` is unioned on resume
`profile_dispatch` now UNIONs the checkpoint's completed phases with the resume
outcome's, rather than overwriting. An accept on the (non-stripped) implement
profile marks `implement` completed via the resume outcome; the checkpoint
(written before the pause) does not list it, so a plain overwrite would
re-execute it.

### §6 — Filtered repair DAG + degraded upstream context
`topological_waves(satisfied_ids=...)` treats out-of-set deps as satisfied, and
`run_dag_sequential(prior_results=...)` pre-fills the receipt map. Because the
durable `ImplementationReceipt` carries no agent output, a new
`PriorSubtaskContext` value object (`subtask_id / summary / attestation_summary /
criteria_report / attestation_error`, no output) provides a **degraded** upstream
view on a cold-process repair; `_render_upstream_receipts` accepts
`SubTaskResult | PriorSubtaskContext`. Done subtasks are never re-invoked or
re-mutated.

### §7 — Waived delivery is not clean
The implement entry carries `delivery_status ∈ {clean, repaired, waived,
incomplete}` + `delivery_waived` + `waiver_id` + `action`, persisted by
`BuildAdapter` into `meta.phases.implement`. On an accept resume the persisted
entry is rewritten `incomplete → waived`. Evidence reads `delivery_status` /
`waiver_id` / `action` from `meta.phases.implement` and `decided_by` from
`meta.phase_handoff_waiver`.

### §8 — Support widened to a non-loop phase
`implement` is added to `_SUPPORTED_HANDOFF_PHASES`; `_validate_handoff_support`
accepts it as a bare top-level step (no enclosing loop, no `until`) and only with
`human_feedback_on_reject`.

### §9 — Cross projection downgrades the implement handoff (mono-only for now)
The cross orchestrator does not yet honour an implement-phase pause inside a
child project (a Part-B / ADR 0038 follow-up; `continue_with_waiver` is already
single-project only). Projecting a profile for cross **downgrades a non-bypass
`implement` handoff to bypass**, so the mono-default `advanced` profile stays
cross-projectable and cross runs keep their pre-ADR-0073 behaviour (an incomplete
`subtask_dag` delivery hard-stops the child). Mono runs are unaffected.

## Consequences

- An honest INCOMPLETE delivery is recoverable: bounded auto-repair, then a
  resumable operator handoff (full verb set) or an audited auto-waiver — no more
  dead-stop on an unrelated environmental failure.
- Auto-waivers are auditable, never silent: a synthetic decision artifact +
  durable `phase_handoff_waiver` with `decided_by="auto:on_exhausted"` + a
  distinct `delivery_status="waived"` breadcrumb in evidence.
- The public `phase_handoff_decide`, its status guard, the decision-artifact read
  schema, and the decide wire format are all untouched.
- Cross runs are behaviourally unchanged by this ADR.

## Out of scope (Part B / follow-ups)

- The MCP surface: typed evidence projection of the new delivery/waiver fields
  and implement/incomplete operator-prompt phrasing (raw `RunStatus.meta` already
  passes the new fields through). Tracked separately in `orcho-mcp`.
- Cross-level honouring of the implement handoff (§9).
- Authoring guidance against over-broad done-criteria such as "full pytest
  green" (couples a subtask to repo-wide health) — a planning concern, not a
  gate change.
