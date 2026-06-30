# ADR 0064: Semantic Profiles & Operating Modes

- **Status:** Accepted (design locked 2026-05-29; implementation P0→P7)
- **Date:** 2026-06-01
- **Phase:** P0 (terminology + contract only — no runtime change)
- **Supersedes:** the *variant-as-depth* axis of ADR 0003 (see below)
- **Master plan:** the semantic-profiles / operating-modes planning record (internal, not shipped with this repo)
- **Companion:** ADR 0065 (policy-derived acceptance), `docs/architecture/automation_principle.md`

## Context

ADR 0003 modelled a profile as a two-axis typology: `ProfileKind`
(`full_cycle`/`scoped`/`custom`) × a *variant* (`FullCycleDepth`:
`lite`/`advanced`/`enterprise`, or `ScopedTarget`: `plan`/`review`/`task`).

That conflated **two genuinely different questions** under one name:

1. **What kind of work** — fix / feature / migration / research / plan / review.
2. **How strictly** Orcho behaves doing it — how much planning, review, evidence,
   where it pauses.

`FullCycleDepth` (lite/advanced/enterprise) is a *strictness* axis; the
profile name is a *semantics* axis. Binding them loses valid combinations: a
narrow but sensitive edit wants high strictness; a big prototype wants low;
research wants no delivery at all. The symptom we hit in a cross-run: the
reviewer's binary verdict *was* the gate decision — any P3 → REJECTED → repair
loop → pause. Strictness was hardcoded in the recipe, not chosen.

## Decision

Model the two questions as **two orthogonal axes** plus a typed policy between
them:

```
SemanticProfile  ×  OperatingMode  →  resolve_run_shape()  →  OperatingModePolicy + RunShape
   (what work)        (how strict)
```

### Axes

```python
class SemanticProfile(StrEnum):
    SMALL_TASK = "small_task"; FEATURE = "feature"; HEAVY_FEATURE = "heavy_feature"
    RESEARCH = "research"; PLANNING = "planning"; MIGRATION = "migration"
    CODE_REVIEW = "code_review"; DELIVERY_AUDIT = "delivery_audit"
    # follow-up (only with real tasks+tests): bug_investigation, refactor,
    # incident_fix

class OperatingMode(StrEnum):
    FAST = "fast"; TEAM = "team"; GOVERNED = "governed"
```

### Naming rules (durable — prevent the collision class)

1. **Phases are verbs (engine steps); SemanticProfiles are nouns (kinds of
   work). A profile is never a bare phase-verb.** Phases: `plan`,
   `validate_plan`, `implement`, `review_changes`, `repair_changes`,
   `final_acceptance`, `compliance_check`, `release_readiness`. This is why the
   old bare `plan`/`review` profiles are renamed/split: `plan` (profile) →
   `planning`; `review` (profile) → `code_review` + `delivery_audit`.
2. **Phase (gate-step) vs audit-profile.** A *phase* verifies the **current
   run's own** output against the **current run's own** plan/criteria — in-flight,
   no external spec (`review_changes`, `final_acceptance`, `compliance_check`,
   `release_readiness`). An *audit-profile* is a **standalone run** whose **input
   is a spec of what to verify** + a target (committed/external work), output =
   findings (`code_review` on a commit/range/PR; `delivery_audit` on work-vs-its-
   intent). Test: "is there an external spec of *what* to check?" yes → profile;
   checks its own run's plan → phase. (This is why `release_readiness` is a
   **phase**, not a profile.)
3. **Execution scope is an orthogonal axis, not a profile.** Single-project vs
   cross-project is *where/how* a run executes (the `orcho cross` orchestrator),
   not *what kind* of work it is. Any `SemanticProfile` runs cross via
   `orcho cross --profile <semantic> ...`. So there is **no** `cross_project_*`
   profile — that would fold the execution axis into the semantic axis (the same
   conflation this ADR removes for depth/strictness).

### Policy (the materialised posture — ~11 typed knobs)

```python
@dataclass(frozen=True)
class OperatingModePolicy:
    planning_depth:               # none | adaptive | standard | deep
    decomposition_depth:          # none | light | standard | explicit
    risk_depth:                   # implicit | light | standard | explicit
    review_depth:                 # skip | light | standard | strict
    review_blocking_min_severity: # P0 | P1 | P2 | P3 | none
    test_expectation:             # situational | targeted | broad | verification_matrix
    evidence_level:               # summary | standard | detailed | audit_ready
    implementation_execution:     # whole_plan | subtask_dag
    handoff_aggressiveness:       # low | on_reject | on_uncertainty | always
    compliance_posture:           # off | light | explicit | required
    user_output_verbosity:        # brief | structured | detailed | evidence_first
    prompt_method_intensity:      # minimal | normal | expert | controlled
```

Mode sets the default policy row (see master plan §4); SemanticProfile may nudge
individual fields over those defaults (e.g. `research+fast → review_depth=skip`;
`migration+team → risk_depth=explicit`).

### Implementation execution policy

`implementation_execution` controls how the implement phase consumes a parsed
plan:

- `whole_plan` — the current mode: one implement invoke receives the whole plan.
- `subtask_dag` — the implement phase executes `ParsedPlan.subtasks` as delivery
  units in stable topological order, recording a per-task implementation receipt.

There is deliberately no separate `sequential_subtasks` mode. Sequential
execution is `subtask_dag` with internal concurrency `1`; later parallel
execution can raise that internal concurrency for independent ready nodes without
changing the public policy value. Likewise, receipt granularity and delivery
gate scope are runtime consequences of `subtask_dag`, not extra public knobs in
the first design.

Default posture:

- `small_task + fast` normally resolves to `whole_plan`.
- `feature + team`, `migration + team`, and governed delivery work normally
  resolve to `subtask_dag`.

Invariant: if Orcho planned required subtasks, Orcho must either execute each
required subtask or record why it was blocked, failed, skipped, or waived.

### Relation to the older `PhaseStep.execution="dag"` surface

This policy **supersedes** the older profile-authored
`PhaseStep(phase="implement", execution="dag")` direction from ADR 0005 /
Phase 5e-5. That older surface exposed an implementation detail on each profile
step and was never promoted into a complete delivery contract. It must not become
a second executor beside `implementation_execution=subtask_dag`.

Implementation rule: the first `subtask_dag` runtime slice retires the
profile-authored `execution="dag"` authoring path for implement delivery and
moves any useful lower-level primitives (`ParsedPlan.subtasks`,
`depends_on` validation, topological ordering, subtask events) behind the
policy-owned executor. No shipped profile should express subtask delivery with
both `PhaseStep.execution` and `OperatingModePolicy.implementation_execution`.

### Resolver & RunShape

```python
def resolve_run_shape(semantic_profile, operating_mode, policy_overrides,
                      task, project_dir, config) -> RunShape: ...

@dataclass(frozen=True)
class RunShape:
    semantic_profile: SemanticProfile
    operating_mode:   OperatingMode
    policy:           OperatingModePolicy
    reason:           str
```

Resolution order: normalise explicit values → mode defaults for missing →
SemanticProfile overlay → user/config overrides → validate combinations → typed
`RunShape` + short reason. Pure and unit-testable; **no LLM classification** in
stage 1.

### Locked decisions (2026-05-29)

1. **Full implementation, not a slice** — the strictness threshold lives inside
   the policy, not in a one-off review tweak.
2. **Default work mode lives in `AppConfig`.**
3. **Profile and task pin the work mode; precedence `task > profile > config`.**
4. **No behaviour-preservation defaults** — single-developer project, no install
   base; current profiles are re-expressed via the axes and behaviour changes are
   accepted. Cut legacy in place.

### Coexistence with declarative profiles

The low-level `Profile` / `PhaseStep` / `LoopStep` / quality-gate remain the
**executable recipe**. `SemanticProfile` / `OperatingMode` are high-level inputs;
the resolver emits a concrete `Profile` (+ `RunShape` metadata). Plugin profiles
keep using the current loader; a later phase adds an extension point for
third-party resolvers/overlays.

## What this supersedes in ADR 0003

ADR 0003's `lite/advanced/enterprise` ladder secretly conflated **two** things in
one name: *what kind/size of work* and *how strict*. This ADR **decomposes** it —
the work component becomes `SemanticProfile`, the strictness component becomes the
**separate, orthogonal** `OperatingMode`. The old ladder maps to neither axis 1:1.

- The **work component** of the depth ladder is re-expressed as `SemanticProfile`:
  `lite → small_task`, `advanced → feature`, `enterprise → heavy_feature`. The
  **kind/target** values (`full_cycle`/`scoped`, `plan`/`review`/`task`) likewise
  become `SemanticProfile` nouns: `plan → planning`, `review → code_review`
  (committed-change review) / `delivery_audit` (work-vs-intent), `task →
  small_task`. New semantics (`research`/`migration`/`refactor`) have no old flat
  equivalent.
- The **strictness component** becomes `OperatingMode` (`fast`/`team`/`governed`)
  — a genuinely **new, independent** axis. It is **not** a rename of the old
  names. The user chooses it freely; a `SemanticProfile` only supplies an
  **overridable default** mode via precedence `task > profile > config`. Any
  `SemanticProfile × OperatingMode` combination is valid.
- ADR 0003's "one Profile type, flat `--profile` namespace" **remains** as the
  executable recipe layer; this ADR adds the high-level axes above it. `--profile`
  still works (back-compat call path), alongside `--semantic-profile` + `--mode`.

### Why the orthogonality matters (the motivating value)

The whole point is the previously-**impossible** combinations the conflated
ladder forbade:

- `small_task + governed` — a narrow but sensitive edit at high strictness
  (impossible before: `lite` always dragged low strictness).
- `heavy_feature + fast` — a big prototype with no ceremony (impossible before:
  the "heavy" rung forced heavy review/evidence).

Strictness is chosen, not implied by the size/kind of the work.

ADRs are append-only: ADR 0003 is not edited; this ADR records the supersession.

## Consequences

- Strictness becomes **chosen and explainable**, not hardcoded.
- Every run records requested/resolved profile+mode, overrides, resolved policy,
  resolver version, and reason (observability — master plan §10).
- `RunShape` in SDK status / evidence is a **public wire change** — lands in the
  wire milestone (P6) with a matching `orcho-mcp` schema + mock smoke.
- Migration changes behaviour of existing profiles by design (no compat shims).

## Phase map

P0 (this ADR + ADR 0065 + `automation_principle.md`) → P1 (types) →
P2 (`verification-gate-and-waiver.md`) → P3 (resolver + full matrix) →
P4 (apply rest) → P5 (prompt method) → P6 (wire) → P7 (CLI + recommendation).

## Terminology update (Stage A alignment)

Append-only clarification; the decision and the body above are unchanged.

The middle `OperatingMode` named `team` in the axes sketch above is
**retired**. The shipped strictness value is `pro`, and the live runtime
`WORK_MODES` tuple is `("", "fast", "pro", "governed")` (`""` = unset). Read any
`team` reference in this ADR as the historical name for the current `pro` mode;
new docs and tests must not reintroduce `team` as a live runtime value.

This is terminology only. The resolver and `RunShape` remain unbuilt; see
[semantic_profiles_alignment.md](../architecture/semantic_profiles_alignment.md)
for the current-state mapping between the shipped flat profiles and this
target.
