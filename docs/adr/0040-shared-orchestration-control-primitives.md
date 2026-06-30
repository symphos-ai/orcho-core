# ADR 0040: Shared Orchestration Control Primitives

Status: Accepted (Phases A–F shipped; E subsumed by B; G deferred)

| Phase | Status   | Commit    | Notes |
|-------|----------|-----------|-------|
| A     | Shipped  | `1939645` | Inventory + ADR note (this file). |
| B     | Shipped  | `d956a3e` | `pipeline.control.handoff_decisions`. Both callers (single resume + cross-plan resume) migrated in the same commit. |
| C     | Shipped  | `ea04482` | `pipeline.control.reviewed_loop`. Primitive only; not yet load-bearing. |
| D     | Shipped  | `860a2c2` | `planning_loop._run_initial_loop` consumes `run_reviewed_loop` via produce/validate closures. Primitive now load-bearing. |
| E     | Subsumed | —         | Single-project resume migration to `handoff_decisions` happened inside Phase B (commit `d956a3e`). No separate Phase E commit. |
| F     | Shipped  | `43b7a97` | Cross project-handoff resume in `run_cross_pipeline` consumes the engine. Three distinct callers now share `load_handoff_decision`. |
| G     | Deferred | —         | Cross terminal-finalize and single-project halt path share name only, not shape. Building a shared "terminal ports" abstraction here would be the exact forbidden shape this ADR rejects. Revisit if and when a second identical caller appears. |

## Context

The orchestrator refactor (commits `cffa57c` … `ee285a8`) split the cross
orchestrator into scoped sibling modules (checkpoint, terminal,
handoff_payloads, planning_loop, project_dispatch). What it did NOT do is
prevent two parallel control-flow shapes from hardening side by side:

- `pipeline.project_orchestrator` (single-project) keeps its own phase
  handoff resume lifecycle in `_apply_phase_handoff_resume`,
  `_load_phase_handoff_decision_validated`,
  `_apply_review_repair_handoff_retry`.
- `pipeline.cross_project.planning_loop` reimplements the same lifecycle
  in `_resume_handoff_decision` + `_retry_feedback_round`.
- `pipeline.cross_project.handoff_payloads` mirrors the shape
  `_apply_phase_handoff_pause` builds inside `project_orchestrator`.

Continuing to extract more files (Phases 6–10 of the prior plan) would
freeze that duplication. ADR 0040 stops that path and switches to
abstraction-first: introduce small **shared control primitives** that
both surfaces consume, then continue extraction on top of them.

## Decision

### Shared primitives (extract to `pipeline.control.*`)

1. **`handoff_decisions.py`** — the decision lifecycle: load the
   artifact via `sdk.phase_handoff.load_phase_handoff_decision`,
   translate `InvalidPhaseHandoffState` to `RuntimeError`, fail-fast
   when an active handoff has no matching decision, and classify the
   action into `Literal["halt", "continue", "retry_feedback"]`. Typed
   `HandoffDecisionContext` (run_id, handoff_id, runs_dir) →
   `HandoffDecisionResult` (action, feedback, note, decided_at).
   No knowledge of checkpoints, profiles, sessions, or cross.

2. **`reviewed_loop.py`** — the round budget + retry-on-reject +
   pause-on-exhaust control flow. Typed `ReviewedLoopPolicy`
   (max_rounds, pause_on_exhausted_reject, bypass_on_exhausted_reject)
   + `ReviewedRound` (round_n, is_retry, output, approved, critique,
   review). Signature roughly:

   ```python
   def run_reviewed_loop(
       *,
       policy: ReviewedLoopPolicy,
       produce: Callable[[int, bool, str], str],     # (round, is_retry, prior_critique) -> output
       validate: Callable[[int, bool, str], ReviewOutcome],  # (round, is_retry, output) -> ReviewOutcome
   ) -> ReviewedLoopResult
   ```

   No knowledge of agents, prompts, checkpoint paths, run dirs,
   project aliases.

### Domain-specific pieces (stay local)

These must NOT be pulled into the shared layer. Each has a single
caller and a domain-specific contract:

| Concern | Single | Cross |
|---|---|---|
| Active handoff storage | `run.session["phase_handoff"]` + meta | `cross_ckpt["phase_handoff_id"]` + meta |
| Halt finalization | Heal torn meta, raise `PhaseHandoffHaltedError` | `finalize_cross_terminal` + return session |
| Continue path | `phase_handoff_override` extra + `_strip_plan_loop` / `_strip_repair_loop` | Read cached `cross_plan.md` + set `phase0_done` |
| Retry-feedback dispatch | One extra `plan→validate_plan` round via the runtime FSM (`_dispatch_via_fsm`) | Direct `cross_plan_prompt` + `validate_cross_plan` invocation |
| Round-key constant | `LoopStep.round_extras_key` (`plan_round`, `repair_round`) | `cross_plan_round` (`CROSS_PLAN_ROUND_KEY`) |
| Bypass semantics | n/a (runtime exits the loop, no bypass) | `bypass_on_exhausted_reject` — proceed with last plan |
| Pause payload builder | `_apply_phase_handoff_pause` from `PhaseHandoffSignal` | `build_cross_plan_handoff_payload` / `build_project_phase_handoff_payload` |

Both `apply_phase_handoff_pause` shapes already converge on the **same
persisted payload contract** (ADR 0031); the builders are domain-local
because they read from different in-memory sources (runtime signal vs.
review-dict). That convergence is the reason a shared decision engine
is enough — we do not also need a shared pause-and-persist primitive.

### Forbidden abstraction shape

The following anti-patterns are explicitly rejected by this ADR and
must not be introduced in Phases B–G:

1. **No `BaseOrchestrator` class.** Single uses the runtime FSM
   (`_run_loop_step` + PhaseStep + PipelineState); cross has no FSM at
   its level. A common ancestor would force one to subclass the other
   and infect both directions.

2. **No "universal runner".** `_run_loop_step` already IS the
   universal loop runner for single-project — coupling it to cross
   would require teaching it about cross checkpoint markers, project
   aliases, the `cross_plan.md` side-channel. Wrong direction; the
   runtime FSM should stay strictly inside the single-project domain.

3. **No generic `IOrchestratorPhase` Strategy** at the orchestration
   level. Cross-level phases (`cross_plan`, `cross_validate_plan`,
   `contract_check`, `cross_final_acceptance`) are bespoke gates;
   single's phase taxonomy is a different shape. A union interface
   would clutter the wire with optionals.

4. **No "Context everything" dataclass.** `CrossPlanningContext` and
   `ProjectDispatchContext` already carry ~20 fields each; merging
   them into a single `OrchestratorContext` would make the shared
   primitives leak domain fields. Each primitive takes only what it
   needs.

5. **No primitive that knows about `runs_dir` layout.** The decision
   engine takes paths as inputs; it does not compute them. The same
   applies to `cross_plan.md` / `meta.json` paths.

### What we want

> Shared control primitives, not a grand framework.
> That's the line between architecture and ceremonial abstraction.

A shared primitive is in scope iff it has **two distinct callers** in
the existing code (single + cross), the same shape under both, and
removing the duplication does not require either caller to leak its
domain into the primitive's signature. Anything else stays local.

## Consequences

### Realised — Phases A–F

- **Phase B** (commit `d956a3e`) delivered `handoff_decisions.py`. Two
  callers migrated in the same commit:
  `project_orchestrator._load_phase_handoff_decision_validated` and
  `planning_loop._resume_handoff_decision` shrank to "build a context,
  call the engine, branch on the narrow Literal". Halt / continue /
  retry_feedback bodies stay local. The outer
  `_apply_phase_handoff_resume` dropped its redundant
  `if decision is None: raise` and its trailing
  "unrecognised action" `RuntimeError` because the engine fail-fasts
  on absence and narrows the action on return.

- **Phase C** (commit `ea04482`) delivered `reviewed_loop.py`.
  Signature: `(policy, produce, validate)`, keyword-only. No
  display / persistence / event / payload ports. The
  signature-stability test
  (`test_primitive_has_no_observer_hooks`) locks that surface so a
  future addition trips a test and forces ADR re-review.

- **Phase D** (commit `860a2c2`) ported
  `planning_loop._run_initial_loop` onto the primitive via
  `_produce` / `_validate` closures. Every cross-domain side-effect
  (banners, `vdump`, `cross_plan.md` write, verdict event,
  review-block render, `log_phase` END, per-round success/warn)
  stayed inside the closures. Stop-and-simplify clause held: zero
  changes to the primitive's signature were needed.

- **Phase E** is **subsumed by Phase B**. Single-project handoff
  resume already migrated to `handoff_decisions` in commit
  `d956a3e`. There is no separate Phase E commit; opening one would
  re-touch the same code path for no architectural gain.

- **Phase F** (commit `43b7a97`) migrated the third call site —
  `run_cross_pipeline`'s child-project handoff resume — onto the
  engine. The shared `load_handoff_decision` is now load-bearing for
  three distinct callers: single-project resume, cross-plan resume,
  cross project-handoff resume. The cross-specific bits (checkpoint
  marker bookkeeping, `_finalize_cross_terminal` on halt,
  `sub_status` flip, parent→child handoff-id translation) stay local
  per the "two distinct callers, same shape" rule.

### Deferred — Phase G

Phase G as originally framed (extract `run_terminal.py` /
`persistence.py` if the shape repeats) is **deferred indefinitely**.
After Phases A–F shipped, the cross and single terminal paths were
re-audited and found to share name only, not shape:

* `pipeline.cross_project.terminal.finalize_cross_terminal` writes a
  parent ``meta.json`` + a placeholder ``evidence.json`` and has no
  knowledge of `_PipelineRun`, `PipelineState`, or per-phase metrics.
* The single-project halt path is wired into `_PipelineRun`,
  `PipelineState`, the checkpoint close protocol, run-diff /
  artifact-mirror emission, and a distinct `PhaseHandoffHaltedError`
  torn-write audit path.

A shared "terminal ports" abstraction would have to admit both shapes
under one signature — exactly the kind of generic-Strategy /
universal-runner anti-pattern this ADR rejects. Phase G is
re-classified as: **revisit only when a second concrete caller
appears with the same shape; until then, leave terminal logic local
to each domain.**

### Bar for future `pipeline.control.*` modules

Same as before, restated for the steady state:

A shared primitive is in scope iff it has **two distinct callers** in
the existing code, the same shape under both, and removing the
duplication does not require either caller to leak its domain into the
primitive's signature. Anything else stays local — even when the names
look similar.

After Phases A–F, `pipeline.control` houses three established
multi-caller primitives that meet that bar: `operator_decisions`,
`resume_context`, and `handoff_decisions`. `reviewed_loop` is the
intentional narrow exception recorded by this ADR: it is load-bearing
for the cross planning loop today, and its signature-stability test
prevents it from growing domain ports while we wait for a second clean
caller. The next addition needs to clear the same test.
