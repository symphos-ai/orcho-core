# Execution Modes

A **phase** answers what work to do. An **execution mode** answers HOW that
phase runs internally.

## Built-in modes

| Mode | Executor (`pipeline.lifecycle`) | Behaviour |
|------|---------------------------------|-----------|
| `linear` | `LinearPhaseStepExecutor` | One handler call from `ctx.phase_registry`. The default — and only built-in — `PhaseStep.execution` mode. |

`PhaseStep.execution` is an open string at the type level so plugins can ship
additional execution modes; only registered modes dispatch (see Registry
routing). The built-in registry contains `linear` only.

Subtask delivery (running `ParsedPlan.subtasks` as tracked units) is **not**
an execution mode — it is policy-owned implement delivery
(`ImplementationExecution`: `whole_plan` | `subtask_dag`) handled inside the
implement phase. `_implementation_execution_for(state)`
(`pipeline/phases/builtin/lifecycle.py`) resolves it in order:

1. profile-level `Profile.implementation_execution` (seeded into run-state
   extras by the runner);
2. the global `pipeline.implementation_execution` config key;
3. the `whole_plan` default.

Unknown values fail fast against the enum. See ADR 0067 (session-aware
subtask delivery) and ADR 0116 (dependency-scoped advancement gate).

## Registry routing

```
PhaseStep(phase="implement", execution="linear")
                 │
                 ▼
   LifecycleContext.execution_mode_registry.get("linear")
                 │
                 ▼
   LinearPhaseStepExecutor.execute(step, state, ctx)
                 │
                 ▼
   FSM stage: gates → adapter → checkpoint → metrics
```

`_validate_v2_entries(profile, ctx=ctx)` rejects unknown execution strings
(top-level and inside loops). Customer plugins shipping additional executors
must register them on the lifecycle registry before `run_profile(..., ctx=ctx)`
is called (Phase 7 `orcho.execution_modes` entry_points discovery wires this
automatically).

## See also

- `pipeline/lifecycle.py` — `ExecutionModeRegistry`, `LinearPhaseStepExecutor`,
  `default_execution_mode_registry()`
- `tests/unit/pipeline/profiles/test_execution_validation.py` — execution-mode
  validation contract (only registered modes dispatch)
- `tests/unit/pipeline/lifecycle/test_execute_step.py` — registry +
  `PhaseLifecycle.execute_step` FSM transitions
- ADR 0067 — session-aware subtask delivery (`implementation_execution=subtask_dag`)
- ADR 0116 — dependency-scoped subtask advancement gate
  (`run_dag_sequential` topological waves; `subtask_dag` is sequential today)
