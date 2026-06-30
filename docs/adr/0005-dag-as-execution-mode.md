# ADR 0005: DAG is an Execution Mode, Not a Profile

- **Status:** Superseded by [ADR 0067](0067-session-aware-subtask-dag-implementation.md)
  (2026-06-02). The profile-step DAG execution mode (`PhaseStep(execution="dag")`)
  and its executor were **hard-removed** in P6. Subtask delivery is now a
  pipeline/operating-mode policy — `implementation_execution=subtask_dag` — not a
  `PhaseStep.execution` mode. The rationale below is retained for history.
- **Date:** 2026-05-06
- **Phase:** 2
- **Deciders:** project owner

## Context

Pre-redesign, the `dag` pipeline was modelled as a *profile* —
specifically, a flat list of five phase names registered through the
public `orcho.phases` entry_points group:

```json
"dag": ["decompose", "decompose_qa", "execute_dag", "integrate_qa", "final_qa"]
```

This treatment was structurally wrong:

1. **DAG is not an alternative pipeline.** A linear pipeline does
   `PLAN → BUILD → REVIEW → FIX → FINAL_QA`. A DAG pipeline does the
   *same conceptual work*, just with `BUILD` decomposed into multiple
   parallelisable sub-tasks. The first three sub-handlers
   (`decompose` / `decompose_qa` / `execute_dag`) are really one logical
   "BUILD via DAG" step plus a cross-task reviewer (`integrate_qa`).
2. **`decompose` / `decompose_qa` / `execute_dag` / `integrate_qa`
   pollute the plugin extension surface.** Plugin authors browsing
   `orcho.phases` would see these and reasonably wonder "should I write
   my own decompose plugin?". They can't — the four are tightly
   coupled to one specific composite flow.
3. **Profile authoring becomes confusing.** A profile author writing a
   custom profile that mixes DAG with linear phases (e.g. plan →
   plan_qa → execute_dag → review → fix) has to know the four DAG
   handlers' contracts, not just "I want BUILD to run as a DAG".

## Decision

Introduce `ExecutionModeRegistry` between `run_profile` and
`PhaseRegistry`. Composite execution modes register here:

```python
default_execution_modes_registry().register("dag", DagExecutionMode())
```

- The `dag` profile collapses to `["dag", "final_qa"]`. The runtime
  walker recognises `"dag"` in `ExecutionModeRegistry` first and routes
  to `DagExecutionMode.execute()` instead of `PhaseRegistry.get("dag")`.
- `DagExecutionMode` keeps the four sub-handlers as private methods
  (`_dag_decompose`, etc., in `pipeline/execution_modes.py`). They are
  **byte-identical ports** of the legacy `_phase_*` bodies — Phase 2 is
  pure dispatch refactor, no DAG semantic changes.
- The composite fires `on_phase_start("decompose")`,
  `on_phase_end("decompose")`, etc. for each sub-step → events,
  metrics, and session shape stay indistinguishable from the legacy
  profile. Snapshot tests pass.
- `decompose` / `decompose_qa` / `execute_dag` / `integrate_qa` are
  **removed** from `orcho.phases` entry_points (in `pyproject.toml`).
  The 4 names no longer appear in `default_registry().names()`.

## Drivers

- **Q4 resolution from the redesign plan**: "DAG-internal phases —
  top-level? — Internal helpers in `DagExecutionMode`, not in
  `orcho.phases`."
- **Composability for Phase 5**: `PhaseStep(phase="build", execution="dag")`
  becomes the canonical way to express "BUILD via DAG" once Phase 5
  wires PhaseStep dispatch.
- **Plugin extension surface clarity**: future
  `orcho.execution_modes` entry_points group (Phase 7) is the right
  home for plugin-shipped modes (`parallel_review`,
  `human_in_the_loop_dag`); separating it from `orcho.phases` prevents
  conceptual mixing.

## Consequences

### Positive

- Cleaner `orcho.phases` surface (7 entries: plan / plan_qa / build /
  review / fix / final_qa / compliance_check; was 11).
- DAG implementation centralised in one class; sub-handler unit tests
  reach internals via the `get_sub_handler` test seam.
- Phase 5 PhaseStep dispatch via `ExecutionMode` becomes a clean
  drop-in.

### Negative / Costs

- Legacy `tests/unit/test_dag_phases.py` coverage migrated from
  `default_registry().get("decompose")` to
  `DagExecutionMode().get_sub_handler("decompose")`. ~10 test calls
  rewritten mechanically.
- The shipped `dag` profile shape is now `["dag", "final_qa"]` instead
  of the old five-phase list. Users running their own
  `--profile dag` would see no observable change (composite preserves
  callback / event / session shape), but custom profiles that explicitly
  named the four DAG sub-handlers no longer work without restructuring.
  Acceptable per no-backcompat policy.

### Neutral

- Validation now consults `ExecutionModeRegistry` in addition to
  `PhaseRegistry` (`Profile.validate(registry, modes_registry=...)`).

## Acceptance bar

Phase 2 acceptance: snapshot test pins zero diff in:

- `events.jsonl` ordering across linear / lite / dag e2e runs.
- `session["phases"]` keys + nested shape across the same.
- Per-phase `on_phase_start` / `on_phase_end` callback counts.

Historical Phase 2 coverage lived in
`tests/unit/test_dag_phases.py::TestDagProfileEndToEnd::test_full_dag_profile_through_run_profile`.
Current DAG build execution is pinned by
`tests/unit/pipeline/runtime/test_dag_build_executor.py`; linear profile
dispatch is covered by `tests/unit/pipeline/runtime/test_pipeline_runtime.py`.

## Alternatives Considered

### A. Keep DAG as a profile

Rejected — Q4 resolution. Continues the structural conflation that
made it hard to author mixed profiles ("I want a DAG BUILD inside an
otherwise-linear flow").

### B. Make `dag` a single phase handler that internally invokes the four sub-handlers

Rejected — would require the single handler to emit four
`session["phases"]` entries internally (since the runtime
`_on_phase_end` only fires once per top-level entry). The composite
ExecutionMode pattern fires the per-substep callbacks naturally.

### C. Move sub-handlers into a separate `pipeline.dag` module but keep them in entry_points

Rejected — same surface-area pollution problem. The four entries on
`orcho.phases` would still mislead plugin authors.

## References

- ADR 0001: pipeline architecture redesign
- `docs/architecture/execution_modes.md`
- Historical `pipeline/execution_modes.py` / `tests/unit/test_execution_modes.py`
  surfaces; current execution-mode dispatch lives in `pipeline/lifecycle.py`
  and `tests/unit/pipeline/runtime/test_dag_build_executor.py`
