# ADR 0004: Typed StepOutcome over `state.halt: bool`

- **Status:** Accepted
- **Date:** 2026-05-06
- **Phase:** 1.5
- **Deciders:** project owner

## Context

The legacy runtime threaded a single `halt: bool` field on
`PipelineState` plus a `halt_reason: str`. Any handler could call
`state.stop("reason")`; the `run_profile` walker checked `state.halt`
between phase entries and bailed.

This worked for two cases:

1. PLAN_QA gate-blocked (`block_on_qa_reject` → "halt the run").
2. Test failure during BUILD (in some plugins).

It did not work for:

1. **Distinguishing halt vs skip vs retry.** A REPROMPT after a human
   review is "re-execute this step with critique," not "stop the run."
2. **Carrying retry payload.** Critique text + trigger origin
   (human / agent_ask) needed somewhere to live; ad-hoc storage in
   `state.extras` was untyped and silent on missing fields.
3. **Failure classification.** "Handler raised an exception" vs
   "quality gate failed with on_fail=HALT" should appear differently
   in the run summary. A boolean flag flattens them.
4. **Lifecycle FSM ordering.** Phase 1.5's FSM (before_review → execute →
   gates → after_review → adapter) needs each stage to return a typed
   verdict that the caller can pattern-match on.

## Decision

Replace the `halt: bool` channel with a typed `StepOutcome`:

```python
class StepStatus(StrEnum):
    COMPLETED        = "completed"
    SKIPPED          = "skipped"
    RETRY_REQUESTED  = "retry_requested"
    HALTED           = "halted"
    FAILED           = "failed"

@dataclass(frozen=True)
class StepOutcome:
    status: StepStatus
    state: PipelineState
    reason: str | None = None
    retry_payload: dict | None = None
```

Each lifecycle stage returns a `StepOutcome`. Construction-time
invariants enforce: HALTED / FAILED / SKIPPED require `reason`,
RETRY_REQUESTED requires `retry_payload` (per ADR 0002).

The legacy `state.halt` / `state.stop()` API stays as a convenience for
transitional handlers; runtime walker reads it and produces a
`StepOutcome(status=HALTED, reason=state.halt_reason)` on its behalf
until Phase 5 migrates handlers to return outcomes directly.

## Drivers

- **Discriminated union via StrEnum + match statement**: Phase 5 can
  pattern-match cleanly:

  ```python
  match outcome.status:
      case StepStatus.RETRY_REQUESTED:
          # advance loop_round, inject critique
      case StepStatus.HALTED:
          break
      case StepStatus.SKIPPED:
          continue
      case StepStatus.FAILED:
          self._record_phase_failure(outcome.reason)
          break
      case StepStatus.COMPLETED:
          state = outcome.state
  ```

- **Retry payload carries semantics**: `loop_round_delta`, `critique`,
  `trigger ∈ {"human", "agent_ask"}`. The outer LoopStep dispatcher
  doesn't need to know whether retry came from a human REPROMPT, an
  agent `ask_human` YAML block, or an exhausted-rounds replan — it just
  honours the payload.

- **Failure vs halt distinction matters in run summary**: meta.json
  reports "halted: user requested" vs "failed: handler raised X" — two
  different categories for `--resume` decisions and post-mortem
  triage.

- **Frozen dataclass safety**: outcomes can be persisted in
  `events.jsonl` / `metrics.json` / session dict without defensive
  copies (per ADR 0002).

## Consequences

### Positive

- Phase 8 HumanReview backend has a typed channel for all 6 actions
  (APPROVE → COMPLETED, HALT → HALTED, RETRY/REPROMPT → RETRY_REQUESTED,
  EDIT → COMPLETED with mutated state, SKIP → SKIPPED).
- Phase 4 QualityGate `on_fail` strategies map directly:
  HALT → HALTED, FEED_INTO_NEXT → COMPLETED + state mutation,
  TRIGGER_REPLAN → RETRY_REQUESTED, INFORMATIONAL → COMPLETED.
- Phase 1.5 FSM transition matrix becomes deterministic: each stage's
  return type is `StepOutcome`, no surprise side-channel via
  `state.halt`.

### Negative / Costs

- Two control channels coexist during Phases 2-4 (`state.halt` for
  legacy handlers + `StepOutcome` for new lifecycle stages). Phase 5
  consolidates by retiring `state.halt`. Acceptable transitional cost.
- Verbose construction at every short-circuit site
  (`StepOutcome(status=StepStatus.HALTED, state=state, reason="...")`).
  Typed clarity outweighs the keystrokes.

## Validation

`tests/unit/pipeline/lifecycle/test_lifecycle_types.py` (Phase 1):

- HALTED / FAILED / SKIPPED without `reason` → ValueError
- RETRY_REQUESTED without `retry_payload` → ValueError
- Complete StrEnum value coverage check
- Successful construction with retry payload

## Alternatives Considered

### A. Keep `state.halt: bool`, add `state.skip: bool` and `state.retry: dict`

Rejected: more flat boolean channels = same problem at higher coupling.
Each new lifecycle concern would add another flag. Discriminated union
captures "exactly one of these N outcomes" cleanly.

### B. Exceptions for control flow (`raise HaltRequested("...")`)

Rejected: exceptions are for errors, not normal control flow. Mixing
`HaltRequested` / `RetryRequested` exceptions with genuine handler
errors makes the catch sites brittle.

### C. Plain `Optional[StepOutcome]` (None == COMPLETED)

Rejected: COMPLETED needs to carry the (mutated) state forward;
returning None loses that. Explicit `status=COMPLETED` outcome is
self-documenting.

## References

- ADR 0001: pipeline architecture redesign
- ADR 0002: frozen dataclasses + invariants
- `docs/architecture/phase_lifecycle.md` — FSM transition matrix
- `pipeline/lifecycle.py` — `StepStatus`, `StepOutcome`,
  `LifecycleContext`, `PhaseLifecycle`
