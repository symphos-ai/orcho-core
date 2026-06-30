# ADR 0116: Dependency-scoped subtask advancement gate

- Status: Accepted
- Date: 2026-06-27
- Supersedes: none
- Related: 0068 (subtask done-criteria attestation), 0073 (implement handoff /
  substance repair), 0106↔0111 (terminal-state vs follow-up interaction)

## Context

`run_dag_sequential` (`pipeline/dag_runner.py`) walks a parsed plan in
topological waves and executes each subtask. A subtask is terminally `done`
only when it neither raised (`error`) nor left its done-criteria attestation
gate open (`attestation_error`); otherwise it is `failed` or `incomplete` and is
appended to the `failed` bucket (blocking).

Before this ADR the runner had **no dependency-scoped gate**. The only mechanism
that held a downstream subtask back was the coarse, *global*
`if failed and stop_on_failure` wave-skip: once any subtask failed, *all*
remaining waves were skipped. `topological_waves` correctly orders a declared
dependent into a strictly later wave, so wave *order* was never the problem —
what was missing was a per-dependency *block*.

The consequence (pinned by the T1 guard tests): at the `run_dag_sequential`
default `stop_on_failure=False` (the function's own contract surface), a declared
dependent of an incomplete/failed subtask was invoked and advanced into
`completed`, even though its dependency never closed its contract. Production
masked the dependent case by passing `stop_on_failure=True`, but that block is
over-broad (it also skips independent branches) and incidental (it rides on wave
ordering and the first-failure flag), not a dependency-scoped gate.

This was reproduced for both incompleteness shapes — an unparseable/unrecoverable
attestation and a confirmed-but-unmet mandatory criterion — and is a protocol
change to the dependent-advancement contract, not a parser bug: the parser and
attestation gate already correctly set `attestation_error`.

## Decision

A subtask satisfies a downstream `depends_on` edge **only when it finished
terminally `done`**. `run_dag_sequential` now consults each subtask's declared
dependencies before invoking it:

- `_is_blocking_outcome(res)` — a dependency is blocking when it carries a hard
  `error` or an `attestation_error` (incomplete). Mirrors the receipt `state`
  derivation and applies to both a live `SubTaskResult` and a degraded
  `PriorSubtaskContext`.
- `_unsatisfied_dependencies(sub, results_by_id)` — the declared `depends_on`
  ids that did not finish `done`. A dependency with **no recorded result** (it
  was itself skipped/held earlier in the pass) is unsatisfied too, so the block
  **cascades transitively** down a chain.

When a subtask has any unsatisfied dependency it is recorded as `skipped`
(receipt `state="skipped"`, reason names the offending dependency) and its agent
is never invoked — regardless of `stop_on_failure`.

Scope is deliberately narrow (reviewer note F1): only **declared `depends_on`
edges** are consulted. A subtask with no dependencies has no edge to fail and is
never held by this gate, so **independent branches keep their existing
semantics** — under `stop_on_failure=True` they are still skipped by the global
wave-skip (unchanged); under the default they still run in parallel. No new
restriction is added to independent branches and their parallelism is not
reduced.

The skip bookkeeping shared by the global wave-skip and the new gate is
extracted into `_record_subtask_skip`, so both skip paths emit one identical
receipt / marker / event shape (only the human reason differs) and the
`run_dag_sequential` body does not grow a second inline skip branch.

## Consequences

- A declared dependent of an `incomplete`/`failed` dependency is held
  (`skipped`, not invoked, not in `completed`) under any `stop_on_failure`
  value; the chain cascades transitively. `DagRunResult.ok` is `False` while any
  subtask is `skipped`.
- A legitimately recovered attestation (the one-shot repair turn succeeds) stays
  `done` and continues to advance its dependents — the gate keys on the final
  `done` state, not on whether a repair occurred.
- Production (`implement` subtask_dag path, `stop_on_failure=True`) is behaviorally
  unchanged for dependents (the global wave-skip already held them); the gate now
  makes the dependent contract correct at the `run_dag_sequential` level
  independent of the flag, which is the durable guarantee.
- Fixtures/tests encoding the old behavior were brought to the new contract:
  `test_dag_runner.py::test_failure_recorded_but_run_continues_by_default` was a
  declared dependent advancing behind a failed dependency; it is split into an
  independent-branch continuation test plus a dependent-held test. The T1 guard
  module (`test_subtask_dag_advancement_gate.py`) now locks the delivered
  contract for both incompleteness shapes plus the transitive cascade and the F1
  independent-branch guarantee.

No new feature flags or parallel legacy paths were introduced (No Backcompat
Ceremony): the change is in place.
