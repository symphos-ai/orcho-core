# ADR 0003: Two-Axis Profile Typology (kind × variant)

- **Status:** Accepted
- **Date:** 2026-05-06
- **Phase:** 1
- **Deciders:** project owner

## Context

The pre-redesign codebase modeled pipeline shape via two parallel
selectors:

1. `PipelineMode` enum (LINEAR / DAG / PLAN_ONLY / REVIEW_ONLY / TASK)
   — semantic intent encoded in code.
2. `Profile` (a flat list of phase names) — execution shape encoded in
   JSON.

The two were inconsistent: a `dag` profile and `PipelineMode.DAG` were
two ways to say the same thing; a `lite` profile didn't have a
corresponding `PipelineMode`. CLI users had to guess whether to set
`--mode` or `--pipeline`.

## Decision

Replace both selectors with **one Profile type** carrying a two-axis
typology:

```python
class ProfileKind(StrEnum):
    FULL_CYCLE = "full_cycle"   # complete dev cycle, varies by depth
    SCOPED     = "scoped"       # partial workflow, varies by target
    CUSTOM     = "custom"       # plugin-defined, no further constraints

class FullCycleDepth(StrEnum):  # variant for kind=FULL_CYCLE
    LITE       = "lite"
    ADVANCED   = "advanced"
    ENTERPRISE = "enterprise"

class ScopedTarget(StrEnum):    # variant for kind=SCOPED
    PLAN   = "plan"
    REVIEW = "review"
    TASK   = "task"
```

`Profile.__post_init__` enforces variant validity per kind via
structural pattern matching (PEP 634).

CLI exposes a **flat namespace**: `orcho run --profile <name>` resolves
to any profile by name regardless of kind/variant. Discoverability
comes from `orcho profiles list` showing kind + variant columns.

## Drivers

- **Eliminate duplicate selectors**: one mode of expression, one source
  of truth.
- **Capture the semantic distinction we kept hand-waving about**:
  "full cycle" vs "scoped" workflow really is a different category, not
  just a different list of phases.
- **Allow varying along the right axis**: enterprise compliance is a
  *depth* concern (still full cycle, just heavier); a "plan-only" run
  is a *target* concern (only plan, scoped). Conflating the two — as
  the legacy enum did — produces awkward names like `PLAN_ONLY` that
  don't cleanly extend.
- **Plugin extensibility**: `kind=CUSTOM` gives plugin authors a clear
  "this is your namespace" without polluting built-in semantics.

## Consequences

### Positive

- `Profile.kind` and `.variant` make filtering / categorization
  obvious in the UI: orcho-web can show three categories with their
  own descriptions; orcho-mcp's `profiles_list` resource includes the
  axis values for client classification.
- Adding a new full-cycle depth (e.g. `MINIMAL`, between `LITE` and
  `ADVANCED`) extends `FullCycleDepth` enum without touching CLI.
- Adding a new scope target (e.g. `migrate`, `audit`) extends
  `ScopedTarget` similarly.
- `kind=CUSTOM` carries no schema constraints on `variant` — plugin
  authors can use whatever taxonomy makes sense for their domain.

### Negative / Costs

- Two-axis typology is one extra concept for users to learn vs. flat
  list of names. Mitigated by flat CLI: `--profile lite` "just works"
  without thinking about kind.
- `PipelineMode` removal in Phase 6 breaks any external scripts using
  `--mode plan`. Acceptable per no-backcompat policy.

## Alternatives Considered

### A. Keep PipelineMode, scrap Profile

Rejected: profiles are configurable; modes are a fixed enum. Users
need to author their own pipelines without forking core.

### B. Keep flat profile list, scrap PipelineMode

Rejected: loses the FULL_CYCLE vs SCOPED semantic distinction. Adding
`plan_only` / `review_only` / `task` as flat profile names creates
naming ceremony (`_only` suffix) and confusion ("is `plan` the same as
`plan_only`?").

### C. Free-form tags instead of typed kind+variant

Rejected: tags are a string-soup that resists discovery. Typed kind +
StrEnum variant gives auto-completion and type-checked validation.

## Validation

`Profile.__post_init__` invariants pinned in
`tests/unit/pipeline/runtime/test_runtime_redesign_types.py::TestProfile`:

- `test_full_cycle_lite` — valid kind+variant combination
- `test_scoped_review` — valid kind+variant combination
- `test_custom_arbitrary_variant` — CUSTOM accepts any variant or None
- `test_full_cycle_with_invalid_variant` — kind=FULL_CYCLE rejects
  unknown variant
- `test_scoped_with_invalid_variant` — kind=SCOPED rejects unknown
  variant

## References

- ADR 0001: pipeline architecture redesign
- ADR 0002: frozen dataclasses + invariants
