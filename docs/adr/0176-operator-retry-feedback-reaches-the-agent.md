# ADR 0176 — Operator retry feedback reaches the agent that redoes the work

- **Status:** Accepted
- **Date:** 2026-08-06
- **Extends:** [ADR 0073](0073-implement-phase-substance-repair-handoff.md)

## Context

`phase_handoff_decide(retry_feedback)` is the operator's only way to steer a run
that is going the wrong way: it re-runs the incomplete subtasks *and* carries a
free-text instruction explaining what to do differently. The re-run half worked.
The instruction half did not reach anyone who could act on it.

The resume arm (`pipeline/project/handoff.py`) seeded
`state.extras['implement_retry']` with `incomplete_ids`, `prior_context`, and
`feedback`. `subtask_dag` read the first two to narrow and seed the DAG, and
read `feedback` only to print an operator-facing redispatch banner. The prompt
the developer agent actually received was assembled by `build_subtask_prompt`
from the PLAN-phase plan, which has no notion of an operator decision — so the
retried subtask was re-dispatched with byte-identical instructions to the ones
that had just produced the wrong result.

The practical consequence is worse than a missing feature: a run stuck on a
wrong plan assumption could not be unblocked by feedback **at all**. The only
forward motion was to abandon the run and restart from a corrected source,
which is exactly the cost the handoff exists to avoid. Observed in dogfood on
2026-08-05 across two burned runs.

This is the same family as the resume-gate-arming and resume-identity re-probe
defects: a resume path reconstructs its inputs and silently drops the operator's
decision context on the way.

## Decision

The operator's `retry_feedback` text is authoritative guidance for the retried
work and must reach the prompt of the agent redoing it.

`build_subtask_prompt` accepts `operator_feedback` and, when it is non-empty,
emits two parts immediately before the executable block:

1. a code-owned `execution_scope_notice:operator_feedback_notice` framing the
   instruction as authoritative, overriding both the agent's earlier approach
   and any conflicting reviewer critique, while explicitly denying scope
   widening;
2. the canonical `human_feedback:operator_feedback` part built by the same
   factory the plan and repair surfaces use, so the operator's own words reach
   the wire byte-identical across every surface.

The split is deliberate: framing is code-owned and may evolve, the operator's
text is data and is never paraphrased, truncated, or re-framed.

`run_dag_sequential` threads the value into **every** subtask it executes in
that pass, including substance-repair passes. The retry set is the work the
operator was speaking about, and each subtask is a separate invocation — often
a deliberately fresh session under ADR 0113 — so sending it once would leave
the rest of the retry blind to the instruction.

`state.extras['implement_retry']['feedback']` remains the single carrier for
this path; the reader does not consult `state.human_feedback`, which is the
plan/repair-phase carrier and is cleared by `plan_artifact` once a plan attempt
consumes it. One fact, one owner, no second copy.

An ordinary pass passes the empty default and renders no part, so a subtask
nobody asked to redo is prompted byte-identically to before.

## Consequences

- Operator feedback is now load-bearing: a run heading the wrong way can be
  corrected in place instead of restarted.
- The redispatch banner keeps printing the feedback for the operator; it is no
  longer the *only* place the text lands.
- `build_subtask_prompt` and `run_dag_sequential` each gain one optional
  keyword with an inert default; no caller is required to change.
- A writer-to-reader contract test drives the real resume arm and the real
  implement phase, so a future regression that reintroduces the seam fails
  loudly rather than silently degrading the operator's only steering wheel.
