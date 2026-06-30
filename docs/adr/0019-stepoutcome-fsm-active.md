# ADR 0019: StepOutcome FSM is the active phase engine

## Status

Accepted in Phase 5e-5.

## Context

Phase 1.5 introduced `StepStatus`, `StepOutcome`, and the intended
phase lifecycle ordering, but the production pipeline still ran through
imperative `_PipelineRun.run_*_loop` methods and callback-side session
ceremony. By Phase 5e the v2 `Profile` schema was active, but several
control-flow signals still crossed module boundaries as `state.extras`
keys or `state.halt` checks.

## Decision

Every v2 `Profile` / `PhaseStep` dispatch now runs through
`PhaseLifecycle.execute_step(step, state, ctx)`.

The lifecycle owns this order:

1. reserved before-review seat
2. execution-mode dispatch
3. quality gates
4. reserved after-review seat
5. session adapter
6. checkpoint
7. metrics

`PipelineState.lifecycle_ctx` is the typed handler access point for
registries, provider, run config, and helper Protocols. Production code
does not use `state.extras["_lifecycle_ctx"]` or
`state.extras["_v2_dispatch_active"]`.

`state.stop(reason)` remains a convenience API for handlers and gates.
The lifecycle translates it into `StepOutcome(status=HALTED, reason=...)`
and persists adapter/checkpoint data before returning.

## Consequences

- `PhaseStep.execution` is no longer schema-only. Built-ins are
  `linear` and `dag`, resolved from `LifecycleContext.execution_mode_registry`.
- `PhaseStep.quality_gates` fire inside the lifecycle after execution
  and before adapter/checkpoint/metrics.
- Session shape writes are centralized through `SessionAdapterRegistry`.
- Handler exceptions surface as `StepOutcome(status=FAILED)` and then
  re-raise through the profile walker so the orchestrator records the
  phase failure.
- `RETRY_REQUESTED` and human-review before/after seats remain reserved
  for Phase 8; Phase 5e-5 does not produce retry outcomes yet.

## Follow-ups

- Phase 6 can remove `PipelineMode` on top of profile-driven dispatch.
- Phase 7 wires entry-point discovery for custom execution modes,
  session adapters, quality gates, and profiles.
- Phase 8 must define the concrete `HumanReview` mutation contract for
  RETRY / REPROMPT / EDIT / SKIP actions before enabling those hooks.
