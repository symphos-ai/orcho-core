# ADR 0002: Frozen Dataclasses with Construction-Time Invariants

- **Status:** Accepted
- **Date:** 2026-05-06
- **Phase:** 1
- **Deciders:** project owner

## Context

The redesign introduces ~30 new types (StrEnums, dataclasses) covering
profiles, phases, loops, gates, reviews, attachments, skills, artifacts,
cross-project orchestration, and lifecycle outcomes. These types form
the contract between orcho-core and ~10 plugin extension surfaces.

Two design choices were available:

1. **Mutable dataclasses + runtime validation**: types are open;
   downstream code defends with `try`/`except` and "is this the right
   shape?" checks before consumption.
2. **Frozen dataclasses + `__post_init__` invariants**: types validate
   themselves at construction; broken inputs raise `ValueError` /
   `TypeError` immediately; downstream code trusts the contract.

## Decision

**Frozen dataclasses with `__post_init__` invariants.** Every new type
gates broken state at the boundary.

```python
@dataclass(frozen=True)
class HumanReview:
    timing: ReviewTiming = ReviewTiming.AFTER
    actions: tuple[HumanAction, ...] = (...)

    def __post_init__(self) -> None:
        terminal = {HumanAction.APPROVE, HumanAction.HALT, HumanAction.SKIP}
        if not (set(self.actions) & terminal):
            raise ValueError(
                "HumanReview.actions must include at least one terminal action"
            )
```

## Drivers

- **Eric Lippert's "invariants over assertions"**: an assertion checks
  that the world is sane at one point; an invariant guarantees it stays
  sane through every constructor call.
- **Concurrent dispatch safety** (Phase 2 ExecutionMode dispatcher,
  Milestone 9 wave executor, Milestone 13 cross-project parallelism):
  frozen instances are immutable → safe to share across threads / async
  tasks without defensive copies.
- **Hashability**: frozen dataclasses with frozen-tuple fields are
  hashable, can serve as cache keys / dict keys / multi-process safe
  values.
- **Self-documenting failures**: `ValueError(f"feed_target required when
  on_fail=FEED_INTO_NEXT")` at construction surfaces in stack traces
  with source line, much faster to debug than "validator returned False
  somewhere".
- **Plugin author UX**: a profile author writing a profile in JSON gets a
  loud error at load time if they forgot a required field — not a
  silent partial-state run that fails 20 minutes later.

## Consequences

### Positive

- 100% of "is this struct valid?" logic lives in one obvious place
  (the dataclass) instead of scattered across consumers.
- Tests for invariants are tight (one test = one invariant).
- Construction sites become self-documenting examples.
- Frozen instances are safe to publish in event streams / session dicts
  / checkpoint JSON without defensive deep-copies.

### Negative / Costs

- Verbose constructors on the call site (every required field passed
  by name).
- Mutation requires `dataclasses.replace(instance, field=new_value)`
  rather than `instance.field = new_value`. This is intentional — most
  "I want to mutate" cases reveal a missing concept that should be a
  separate type (e.g. `StepOutcome` instead of `state.halt = True`).

### Neutral

- Python 3.10+ pattern matching (`match self.kind: case ProfileKind.X`)
  pairs naturally with discriminated unions in the validation logic.

## Implementation

Per-invariant test coverage (Phase 1):

- `tests/unit/pipeline/runtime/test_runtime_redesign_types.py` — Profile / PhaseStep /
  LoopStep / QualityGate / HumanReview / Attachment
- `tests/unit/pipeline/skills/test_types.py` — SkillPackage / SkillBinding /
  SkillTrustPolicy
- `tests/unit/pipeline/cross_project/test_cross_project_types.py` — ProjectStep /
  ContractValidation / CrossProjectProfile (N6 canonical-dir check)
- `tests/unit/pipeline/lifecycle/test_lifecycle_types.py` — StepOutcome
- `tests/unit/pipeline/artifacts/test_artifacts_types.py` — ArtifactRecord
- `tests/unit/agents/test_attachment.py` — Attachment edge cases
- `tests/unit/pipeline/cross_project/test_subtask_owned_files.py` — SubTask extensions

## Alternatives Considered

### A. Pydantic models

Rejected: heavy dependency for a runtime engine that already keeps deps
minimal. Frozen dataclasses + `__post_init__` give 90% of the value at
zero added imports.

### B. Mutable dataclasses + caller-side validation

Rejected: violates DRY. Every consumer of a `HumanReview` would have to
re-check "does this have a terminal action?" defensively. The whole
point of typed dataclasses is to eliminate that ceremony.

### C. Frozen instances but no `__post_init__`

Rejected: type system catches "wrong field type" but not "wrong field
value combination" (e.g. `feed_target` required when `on_fail` is one
specific enum). `__post_init__` covers the gap.

## References

- ADR 0001: pipeline architecture redesign
- Architecture overview: `docs/architecture/overview.md`
