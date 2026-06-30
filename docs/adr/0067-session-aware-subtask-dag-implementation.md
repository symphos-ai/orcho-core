# ADR 0067 — Session-aware subtask_dag implementation

- Status: Accepted
- Date: 2026-06-02
- Relates to: ADR 0026 (session-aware prompt parts), ADR 0027 (execution
  surfaces / `session_split`), ADR 0060 (PromptTurn canonical render surface),
  ADR 0064 (semantic profiles & operating modes), ADR 0066 (repair receipts)
- Supersedes: ADR 0005 (DAG as a profile-step execution mode). The
  profile-step DAG execution mode and its executor are **removed**, not merely
  rejected.

## Context

Built-in subtask delivery (`implementation_execution=subtask_dag`) lets the
architect decompose a plan into `ParsedPlan.subtasks` and the implement phase
execute them as tracked units. The first cut had three defects:

1. `session_split=common` never physically continued one provider session
   across phases — the implement phase invoked without `continue_session`, and
   each phase binds a distinct agent instance, so cross-phase continuity was a
   no-op.
2. `subtask_dag` bypassed the canonical render/invoke seam: it called the
   runtime directly on a raw string and emitted a fabricated `prompt_render`
   trace (`runtime="subtask_dag"`).
3. Each subtask prompt shipped the whole plan — every sibling's spec/files/
   done-criteria — so the developer treated the entire plan as executable.

A parallel, half-built `PhaseStep(execution="dag")` profile-step executor
shadowed this path, inviting a second, divergent subtask executor.

## Decision

Make subtask delivery session-aware, current-only, and observable, and remove
the legacy profile-step DAG surface entirely.

1. **Real session continuity.** Continuation is derived from session policy and
   committed state (`_should_continue_prompt_session` + a shared
   `_compute_session_key`), never from a subtask index. When continuing across
   distinct phase agent instances, `_session_aware_invoke` reconciles the
   agent's provider `session_id` to the stored session (the
   `apply_followup_session_seeds` primitive). `session_split=common` /
   shared-`per_role` physically continue one session when runtime+model match.

2. **Canonical render/invoke seam.** `build_subtask_prompt` returns a
   `PromptTurn`; `run_dag_sequential` takes an injected invoke strategy and the
   implement handler injects the session-aware adapter (dag_runner stays
   `PipelineState`-free — no import cycle). The fabricated trace is gone; the
   phase-level `prompt_render` is an honest aggregate (real `session_key`,
   `execution_mode="subtask_dag"`, `surface_count`, rolled-up `render_mode`),
   with real per-subtask records in an in-memory sibling.

3. **Current-only subtask prompt.** The plan contract (byte-identical to the
   validate_plan surface) and a **compact DAG map** (id / goal / depends_on
   only) are background; the **current subtask** is the only executable block
   (classified DECISION_BEARING); code-owned execution rules state that
   sibling/downstream subtasks and plan-level acceptance are not this subtask's
   work.

4. **Upstream receipts.** A subtask receives bounded, sandboxed, XML-escaped
   quoted output from its declared dependencies — continuity hints, not proof —
   so continuity survives even when the session does not chain (stateless).

5. **Observability.** Live `ORCHO subtask` markers show the static current-only
   facts at START and the real session/render facts at DONE; the direct
   (non-session) path is labelled honestly. Durable evidence remains the
   single honest aggregate; durable per-subtask fanout traces are deferred to a
   future consumer-driven pass.

6. **Canonical surface + removal.** The only way to request subtask delivery is
   `implementation_execution=subtask_dag` /
   `OperatingModePolicy.implementation_execution`. The profile-step DAG
   execution mode (`ExecutionMode.DAG`), its `DagBuildPhaseStepExecutor`, and
   all bespoke "retired execution mode" handling are removed. `PhaseStep.execution`
   ships only `linear` (plus plugin-registered modes); any unregistered mode is
   rejected generically by execution-mode validation.

## Consequences

- One implementation path for subtask delivery; no shadow executor to diverge.
- `whole_plan` implement and reviewer phases are unchanged; the durable session
  shape is unchanged (markers are live-log only; the render rollup is
  print-only).
- A profile can no longer express subtask delivery as a per-phase execution
  mode — by design. Plugins authoring real alternative per-phase run strategies
  still register modes via `orcho.execution_modes`.

## Implementation

Shipped across P0–P6 on `main`: P0 cross-phase `common` continuation; P1
PromptTurn/session-aware routing + honest aggregate; P2 current-only prompt
(compact map, execution rules); P3 upstream receipts (bounded/sandboxed/
escaped); P4 observability markers; P5 test consolidation; P6 (this ADR) the
hard removal of the legacy execution mode.
