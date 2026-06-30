# Authoring an Execution Mode

> Skeleton — full content arrives in Phase 7 once
> `orcho.execution_modes` entry_points group is wired.
> <!-- TODO(orcho-phase-7): expand with end-to-end pip plugin example,
> registration through pyproject.toml, and discovery semantics. -->

An execution mode is a strategy for HOW a profile entry runs, distinct
from WHAT it does (the phase name).

## Built-in modes

- `LinearPhaseStepExecutor` — one handler call per `PhaseStep`. This is the
  only built-in `PhaseStep.execution` mode; plugins register additional ones.

See [docs/architecture/execution_modes.md](../architecture/execution_modes.md)
for behaviour reference.

## Why you might write one

Do not write an implement-subtask executor as a `PhaseStep.execution` mode.
Subtask delivery is policy-owned: use `implementation_execution=subtask_dag`
(see ADR 0067). Execution modes are for genuinely different per-phase run
strategies (e.g. a future parallel-review mode).

- **Parallel review fan-out**: run two reviewer agents
  simultaneously, aggregate verdicts.
- **Human-in-the-loop DAG**: insert a confirmation gate between every
  DAG sub-task.
- **Speculative execution**: kick off REVIEW and FIX in parallel,
  cancel REVIEW if FIX produces clean output first.

## Contract (as of Phase 5e-5)

```python
from typing import Protocol, runtime_checkable
from pipeline.lifecycle import LifecycleContext
from pipeline.runtime import PhaseStep, PipelineState

@runtime_checkable
class PhaseStepExecutor(Protocol):
    def execute(
        self,
        step: PhaseStep,
        state: PipelineState,
        ctx: LifecycleContext,
    ) -> PipelineState: ...
```

Implementations must:

1. Mutate `state.phase_log[step.phase]` in the same shape the linear
   phase handler would have produced, or a documented additive superset.
2. Use `ctx.phase_registry`, `ctx.provider`, and helper Protocols
   instead of importing orchestrator internals.
3. Return the same `PipelineState` instance or a replacement
   `PipelineState`. The lifecycle FSM handles gates, adapter,
   checkpoint, metrics, and `state.halt` translation after the executor
   returns.

## Phase 7 will add

- `orcho.execution_modes` entry_points discovery (registration via
  `pyproject.toml`).
- Snapshot test fixture demonstrating a third-party-shipped mode.
- Documented priority order for plugin discovery.
