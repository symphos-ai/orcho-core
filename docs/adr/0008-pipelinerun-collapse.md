# ADR 0008: Two-Step `_PipelineRun` Collapse (Phase 5a + 5b)

- **Status:** Accepted (Phase 5a + 5b shipped; Phase 5c planned)
- **Date:** 2026-05-06
- **Phase:** 5
- **Deciders:** project owner

## Context

`_PipelineRun` (in `pipeline/project_orchestrator.py`) used to be a
~671-LoC god-object holding the entire imperative pipeline:
`run_hypothesis_loop`, `run_plan_qa_loop`, `run_build_phase`,
`run_review_fix_loop`, `run_final_qa`. Phases 2-4 + 4.5 already
extracted the per-phase concerns into registries:

- Phase 2 → `ExecutionModeRegistry` (linear vs dag dispatch).
- Phase 3 → `SessionAdapterRegistry` (per-phase session shape).
- Phase 4 → `QualityGateRegistry` (post-phase verification + fail
  policy).
- Phase 4.5 → attachment loader / inject helpers + state.attachments.

The `_PipelineRun.run_*_loop` methods now mostly orchestrate
**registered components**: stuff per-round data into `phase_log`,
invoke a registered adapter, fire a gate, advance the loop counter.
The remaining imperative bits are:

1. Round counter management (`plan_round`, fix escalation model swap).
2. Mock provider plan-file write (`plan_*.md` parity).
3. PLAN_QA bridge: locating the latest `plan_*.md` to pass to
   reviewer.
4. Skip-no-plan-file path (synthesised approved entry).
5. Critique-empty short-circuit on REVIEW (legacy 1083-1088 contract).
6. FIX agent escalation + CHAIN/HYBRID swap with `try/finally`.
7. Mid-loop `save_session(...)` checkpoint.
8. Gate-blocked `block_on_qa_reject` path → halt with
   `awaiting_qa_approval`.

These are real but **declaratively expressible** through `LoopStep`
+ `PhaseStep.quality_gates` + `HumanReview` (Phase 8). The collapse is
how the runtime walker becomes the single dispatch entry point.

## Decision

**Two-step migration**:

### Phase 5a (this commit) — additive infrastructure

Ship the new authoring surface without touching the existing
dispatcher:

- `pipeline/profile_loader.py` parses the new two-axis Profile JSON
  schema (`kind` × `variant` × `steps` with PhaseStep / LoopStep
  nested objects). Reuses `Profile.__post_init__` invariants from
  Phase 1.
- `_config/pipeline_profiles_v2.json` ships six profiles
  (`lite` / `advanced` / `enterprise` / `plan` / `review` / `task`)
  in the new schema.
- 30 parser tests pin shape contracts. v2 file parses cleanly.
- Legacy `_config/pipeline_profiles.json` + `_PipelineRun.run_*_loop`
  methods stay untouched — they're still the active dispatch path.
- Documentation: `docs/reference/profile_schema.md`.

Risk: ZERO. Pure additive; nothing the runtime walker uses changes.

### Phase 5b (this commit) — runtime walker accepts v2 Profile shape

`pipeline/runtime.py:run_profile` now dispatches both legacy
``PipelineProfile`` (str + LoopStep entries) AND v2 ``Profile``
(top-level PhaseStep + LoopStep) through the same code path. v2's
top-level PhaseStep is dispatched by ``step.phase`` name through the
existing ``_dispatch_one`` → ``PhaseRegistry`` path; LoopStep iterates
inner PhaseSteps as before. Validation walks the v2 entries through a
new ``_validate_v2_entries`` helper that consults both ``PhaseRegistry``
and ``modes_registry`` (composite execution modes) — same semantics as
``PipelineProfile.validate``.

What 5b does NOT yet do:
- Per-step ``quality_gates`` consultation at dispatch time. The
  PhaseStep field exists but ``run_profile`` doesn't yet read it; gates
  fire through ``_PipelineRun._on_phase_end`` for build/fix only (Phase 4).
- Per-step ``human_review`` hooks. Phase 8 wires that.
- Inter-step ``until`` evaluation in LoopStep. Today the loop runs all
  inner steps before checking ``until``; the legacy
  ``run_review_fix_loop`` critique-empty short-circuit (skip fix when
  review is clean) needs either inter-step until or a fix handler that
  no-ops on empty critique. Phase 5c picks one.

10 new tests pin the v2 dispatch behaviour
(``tests/unit/pipeline/runtime/test_runtime_v2_dispatch.py``):
- Top-level PhaseStep dispatch + callbacks fire + halt short-circuits.
- Mixed top-level (LoopStep + PhaseStep) — canonical advanced shape.
- Validation catches unknown phases at top level + nested in LoopStep.
- Composite mode names (``"dag"``) recognised through ``modes_registry``.
- Legacy ``PipelineProfile`` path still works.
- Shipped advanced profile from ``pipeline_profiles_v2.json``
  dispatches end-to-end.

### Phase 5c (next commit) — actual orchestrator collapse

Do the surgery once Phase 5b is stable + reviewed:

- Wire `run_profile` in `pipeline/runtime.py` to dispatch the new
  `Profile` shape (top-level PhaseStep instances, not just LoopStep
  containers).
- Migrate `_PipelineRun.run_*_loop` callers to `run_profile(profile,
  state, registry, callbacks)`. Each loop method's imperative body
  becomes a declarative `LoopStep` entry in the shipped profile.
- Imperative bits 1-8 above migrate to:
  - **Round counter** → `LoopStep.round_extras_key` already does this.
  - **Mock plan-file write** → `MockAgentProvider` extension (mock
    parity hook moves into the provider, where it belongs).
  - **PLAN_QA bridge** → `_phase_plan_qa` handler reads
    `state.extras["plan_artifact_path"]` set by the `_phase_plan`
    handler itself.
  - **Skip-no-plan-file path** → handler emits
    `state.phase_log["plan_qa"] = {"approved": True, ...}`; LoopStep's
    `until` predicate exits.
  - **Critique-empty short-circuit** → already expressible as
    `until="review.clean"`.
  - **FIX agent escalation** → `PhaseStep.overrides` per loop round
    (handler reads `extras["loop_round"]`).
  - **Mid-loop save_session** → `PhaseLifecycleHooks` (Phase 1.5
    skeleton) gains a per-round checkpoint hook.
  - **Gate-blocked path** → `HumanReview` with `auto_halt` backend +
    custom `qa_gate` payload (Phase 8 wires this; Phase 5b uses
    `block_on_qa_reject` flag as transitional bridge until Phase 8
    ships).
- `_PipelineRun` shrinks from ~671 LoC to ~200 LoC — pure state
  container + callbacks.
- Legacy `pipeline_profiles.json` removed; `_PipelineRun.run_*_loop`
  methods deleted.
- `TestingConfig` migration deferred from Phase 4 happens in the same
  Phase 5b sweep: `PluginConfig.testing` → `PluginConfig.quality_gates: dict`.

Risk: HIGH. Mitigated by:
- Phase 0.5 side-effect matrix (every method's side-effects
  inventoried + assigned to a new owner).
- Snapshot tests on session shape parity (Phase 3 contract).
- Snapshot tests on events.jsonl ordering (Phase 4 contract).
- Phase 5a's parser already validated the v2 profiles; Phase 5b just
  flips the dispatcher.

## Drivers

- **Avoid one HIGH-risk commit.** Splitting into 5a + 5b lets the
  parser + shipped profiles get reviewed and bedded down before the
  dispatcher swap. If 5b uncovers a side-effect not captured in the
  matrix, 5a remains stable; we can iterate.
- **Plugin authors get authoring surface today.** Phase 5a lets
  third-party plugins ship their own profile JSON files via
  `orcho.profiles` entry_points (Phase 7) before the dispatcher
  collapse lands. They author against the typed `Profile` shape
  immediately; the runtime catches up in 5b.
- **No-encyclopedia guardrail.** Phase 5b is the largest single
  refactor in the roadmap; the project's phase completion bar says
  "code/tests green + ADR + schema documented enough for next phase".
  Phase 5a satisfies the bar; Phase 5b inherits a stable schema
  reference.

## Consequences

### Positive

- Phase 5a delivers value alone (plugin authoring surface, declarative
  profile expression).
- Phase 5b is bounded and testable: same six shipped profiles, just a
  different dispatcher driving them.
- Third-party plugins can roll out v2 profiles ahead of orcho-core's
  dispatcher swap.
- Two smaller commits → reviewable, revertable; one HIGH-risk commit
  is replaced by one ZERO-risk + one HIGH-risk.

### Negative / Costs

- Two profile JSON files coexist between 5a and 5b
  (`pipeline_profiles.json` for the legacy walker,
  `pipeline_profiles_v2.json` for the new parser). Tracking matrix
  in `docs/reference/profile_schema.md` calls this out.
- Third-party plugins authoring v2 profiles ahead of 5b can't actually
  *run* them yet. Documented loudly.

### Neutral

- ADR 0008 (this) carries Phase 5a's rationale + Phase 5b's plan.
  Phase 5b commits will reference back.

## Validation (Phase 5a)

- 30 parser tests pin malformed-input rejection + shape contracts.
- 7 integration tests load each shipped profile and assert structure
  (lite has 3 PhaseSteps, advanced has 2 LoopSteps + DAG build,
  enterprise includes compliance_check, plan stops after qa, etc.).
- Full unit suite: 967 passed, 1 skipped (was 937, +30).
- `Profile.__post_init__` invariants from Phase 1 reused for free —
  parser doesn't duplicate kind+variant enforcement.

## Validation (Phase 5b — planned)

- Snapshot test: end-to-end `advanced` run with mock providers
  produces identical `session.json` shape pre-vs-post collapse.
- Snapshot test: `events.jsonl` ordering identical.
- Side-effect parity test (Phase 0.5 matrix): each row diff = empty.
- All 967 tests still pass after the v1 file is removed.

## Alternatives Considered

### A. Single Phase 5 commit doing parser + profiles + collapse atomically

Rejected — the plan estimated 8-10 hours; this is the largest single
refactor in the roadmap. Splitting reduces blast radius and lets each
half be independently reviewed. Post-implementation reviews of Phase 1
and Phase 4 already caught drifts after the fact; same risk applies
here at higher cost.

### B. Defer Phase 5a authoring surface until 5b is ready

Rejected — plugin authors and orcho-mcp clients (Milestone 12) want
the typed Profile schema published. Phase 5a unblocks that work
without committing to the dispatcher swap.

### C. Keep both v1 and v2 indefinitely

Rejected — two dispatchers means double the maintenance burden, and
the v1 flat-shape can't express PhaseStep richness (skill / role /
quality_gates / human_review per step). Phase 5b has to land
eventually; ADR 0008 commits to the timeline.

## References

- ADR 0001: pipeline architecture redesign (overall)
- ADR 0003: two-axis profile typology (kind+variant)
- ADR 0006: SessionAdapter scope invariant
- ADR 0007: quality-gate fail strategies as data
- `docs/architecture/phase_lifecycle.md` — owner-stage model replacing
  imperative run-loop side effects
- `pipeline/profile_loader.py` — Phase 5a implementation
- `_config/pipeline_profiles_v2.json` — Phase 5a shipped profiles
- `tests/unit/pipeline/profiles/test_profile_loader.py` — Phase 5a invariants
