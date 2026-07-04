# Quality Gates

> Phase 4 reference. <!-- TODO(orcho-phase-7): expand once
> orcho.quality_gates entry_points group is wired and inferential gates
> ship as third-party plugins. -->

A *quality gate* is a registered post-phase check that produces a typed
`QualityGateResult` and applies a declarative fail policy to the
pipeline state. Gates replace the ad-hoc test-runner callback the
legacy orchestrator embedded in `_on_phase_end`.

## Two kinds (per Fowler)

| Kind | Examples | Cost model | Scheduling |
|------|----------|------------|------------|
| **computational** | shell exit-code: tests, lint, mypy, compile, format-check | wall-clock + CPU; cost ~ $0 | inline (synchronous); blocks the phase |
| **inferential** | LLM judges: security review, spec compliance, code-review-by-LLM | wall-clock + tokens; cost > $0 | wave-batched (Phase 8.5 RunBudget aware) |

The distinction matters for cost accounting (`cost_usd` only meaningful
for inferential), and for scheduling: inferential gates can be
parallelised (Milestone 9 wave executor), computational gates often
share a project-level lock (e.g. Unity test runner can't run twice
concurrently in the same project).

## Four fail strategies

`QualityGate.on_fail` is a `FailStrategy` StrEnum:

| Strategy | State mutation | Run flow |
|----------|----------------|----------|
| `HALT` | `state.stop("quality gate X failed (on_fail=HALT)")` | Loop / profile breaks immediately |
| `FEED_INTO_NEXT` | `state.extras[gate.feed_target] = result.output` | Next phase consumes the failure as input (e.g. test failures → FIX prompt) |
| `TRIGGER_REPLAN` | `state.last_critique = result.output` | Outer LoopStep treats the gate failure as critique; round counter advances; replan prompt fires next iteration |
| `INFORMATIONAL` | none | Logged + persisted in `state.phase_log[name]["quality_gates"][gate.name]` for audit; run continues unchanged |

Strategy is **data**, not branching code: `apply_fail_strategy(gate,
result, state)` reads the enum and mutates state accordingly.

## Result shape

```python
@dataclass(frozen=True)
class QualityGateResult:
    name: str
    passed: bool
    output: str
    duration_s: float
    kind: GateKind = GateKind.COMPUTATIONAL
    cost_usd: float | None = None
    error: str | None = None       # populated on handler exception
```

Persisted to `state.phase_log[phase]["quality_gates"][gate_name]`. Phase
5+ exposes this through a SessionAdapter (built or fix adapters extend
to include the gate sub-dict).

## Built-in registry

Phase 4 ships **one** built-in gate:

| Name | Class | Kind | Behaviour |
|------|-------|------|-----------|
| `tests` | `TestsGate` | computational | Wraps the legacy `pipeline.project_orchestrator.run_tests` body (single + multi-suite per `plugin.quality_gates["tests"]`). Skipped suites yield `passed=True, output=""`. Handler exceptions captured as `passed=False, error=...`. |

`default_quality_gate_registry()` returns a singleton with `tests`
pre-registered. Phase 7 grows this through `orcho.quality_gates`
entry_points.

## Active wiring

Phase 5e routes `PhaseStep.quality_gates` through runtime dispatch and
the lifecycle FSM. Gates run after the phase handler, before
adapter/checkpoint/metrics, and the result is persisted in
`state.phase_log[phase]["quality_gates"][gate_name]`. A `HALT` gate
short-circuits remaining gates and stops the run after the adapter and
checkpoint persist the halted phase.

The built-in `tests` gate still reuses the internal `TestingConfig` /
`TestSuiteConfig` dataclasses as an implementation detail, but the public
plugin surface is `PluginConfig.quality_gates["tests"]`.

## What Phase 4/5e does NOT do

- **No inferential gates shipped.** Phase 4 ships only the
  infrastructure + computational `tests`. Inferential gates
  (`security_review`, `spec_compliance`) are an extension surface for
  third-party plugins; Phase 7 wires their discovery via entry_points.

## Done-criteria attestation gate (subtask_dag)

The `subtask_dag` executor adds one more, **distinct** gate per subtask
([ADR 0068](../adr/0068-subtask-done-criteria-attestation.md)). It is **not**
a `QualityGate` and **not** an LLM judge — it is a deterministic
*shape/completeness* gate over the developer's self-attestation:

- The developer agent must append exactly one machine-readable
  `subtask_attestation` JSON object reporting, per `done_criteria` index,
  whether it was met plus a one-sentence evidence claim.
- Orcho checks **shape + completeness only**: the object parses, the
  `subtask_id` matches, there is exactly one entry per criterion **index**
  (1-based; criterion *text* may drift), and every entry is `met=true`.
- It does **not** judge whether the evidence is true — that stays with the
  quality gates (`tests`), `review_changes`, and `final_acceptance`.

The framing: *the attestation gate checks "did the implementer explicitly claim
every criterion?"; the quality gates check "is that claim believable?"*

A missing / malformed / mismatched / not-all-met attestation marks the subtask
`incomplete` (a terminal receipt state distinct from a hard `failed`), which
blocks delivery (`delivery_clean=false`, surfaced as `incomplete_subtasks` /
`attestation_incomplete` on the implement entry) and, under `stop_on_failure`,
skips downstream subtasks. A criteria-less subtask gets no contract and no gate.

## Quality gates vs verification environment / receipt

A quality gate is one corner of a triad that the
[verification contract](verification_contract.md) keeps deliberately distinct:

```text
verification_env    = where and against what
quality_gate        = what must be true
verification_receipt = durable proof that the gate ran in the right env
```

These three are **not** collapsed into one concept:

- A **verification environment** names the subject under test and command
  context — which interpreter, cwd, paths, and dependency checkouts make a
  result meaningful (e.g. canonical `orcho-core` vs a stable install).
- A **quality gate** is the pass/fail condition itself (the `tests` gate, a
  future inferential judge) — independent of where it ran.
- A **verification receipt** is the durable, machine-readable artifact proving
  the native command actually ran in that environment, so a reviewer reads
  proof instead of re-running an ad-hoc host command against a different
  subject. A correction follow-up may **inherit** a valid parent run's receipt
  for the same diff — the readiness/delivery classifier searches the current run
  then the parent run(s), so re-used proof is never falsely reported missing
  ([ADR 0089](../adr/0089-delivery-receipt-continuity.md)).

Only `worktree_bootstrap` ([ADR 0074](../adr/0074-worktree-bootstrap.md)) and
the verification-environment receipt
([ADR 0076](../adr/0076-durable-verification-environment-receipt.md)) exist in
core today; the rest of the contract is proposed. The same Orcho UI can show
gate results, environments, and receipts together, but each has a single owner.
See [verification_contract.md](verification_contract.md) for the full model.

## Quality gates vs phase handoff

Quality gates and phase handoff are **independent concepts**:

| Concept | Owner | Fires when | Outcome |
|---------|-------|-----------|---------|
| **Quality gate** | the FSM gates stage — `_fire_step_quality_gates` in `pipeline/runtime/runner.py`, dispatching handlers from `pipeline.quality_gates.QualityGateRegistry` (post-handler, pre-adapter) | A registered post-phase check (`tests`, future inferential gates) produces a `QualityGateResult`. | Mutates state per `FailStrategy` (`HALT` / `FEED_INTO_NEXT` / `TRIGGER_REPLAN` / `INFORMATIONAL`); the FSM advances to the next stage. |
| **Phase handoff** | Loop dispatcher (post-FSM, end-of-round) | A `PhaseStep.handoff` policy declares a trigger condition the verdict + loop state satisfies (see [phase_lifecycle.md § Phase handoff](phase_lifecycle.md#phase-handoff--declarative-pause-point)). | Persists `meta.phase_handoff`, status flips to `awaiting_phase_handoff`, subprocess exits rc=4. Resumed via `phase_handoff_decide` + `orcho_run_resume`. |

A `HALT` gate ends the run with `status="halted"`; a phase handoff
**pauses** with `status="awaiting_phase_handoff"`, and resumes via a
decision artifact. The two surfaces do not overlap — a phase can
declare both a `quality_gates` list and a `handoff` policy; the
gates fire first inside the FSM, and only when the FSM returns
`COMPLETED` does the loop dispatcher get a chance to evaluate the
handoff trigger.

The legacy `validate_plan_gate` mechanism (ADR 0022) overloaded
`block_on_plan_reject` to do something resembling phase handoff
inside the gate machinery; the phase-handoff slice
([ADR 0031](../adr/0031-generic-phase-handoff-contract.md)) cleanly
separates the two so each concept has one owner.

## See also

- [Verification contract](verification_contract.md) — the gate / environment /
  receipt triad and the proposed project-level verification model
- `pipeline/quality_gates.py` — implementation
- `tests/unit/pipeline/quality_gates/` — pinned strategy mutations + gate
  parity
- [Phase lifecycle](phase_lifecycle.md) — where gates fire in the FSM,
  plus the phase-handoff layer that sits above the per-step FSM
- [ADR 0031](../adr/0031-generic-phase-handoff-contract.md) —
  phase-handoff contract that decouples pause semantics from gate
  fail strategies
