# ADR 0160 — Same-run cross resume of an interrupted child

- **Status:** Accepted
- **Date:** 2026-07-27
- **Related:** [ADR 0146](0146-cross-child-outcome-and-gate-admission.md), [ADR 0148](0148-canonical-cross-parent-state-reduction.md), [ADR 0152](0152-executable-cross-execution-graph.md), and [ADR 0159](0159-cross-resume-continuation-and-cfa-rearming.md)

## Context

An interrupted same-run cross resume can find a child whose exact physical
`meta.json` and typed lifecycle operations still reduce to `running`.  The
normal graph projection correctly treats that fact as live work and therefore
does not select it.  After the parent process has stopped, however, an explicit
resume needs one in-place attempt of that child without rerunning completed
siblings or admitting `contract_check` / CFA from incomplete child facts.

## Decision

For one explicit same-run parent resume only, dispatch derives an
invocation-scoped set of aliases whose canonical physical child projection is
`running`.  It passes that set to the existing graph-state reduction.  A listed
running project re-enters the normal dependency reduction as pending and is
selected only when its predecessors are satisfied in stable graph order.

The scheduler consumes an alias from that transient set before invoking the
child.  The selected child uses its existing in-place resume lifecycle with
`ProjectRunRequest.resume_from=<alias>`.  A nonterminal return cannot select
the alias again during the same invocation.  Dispatch then re-reduces exact
physical facts normally; runner gates remain pending until all required child
facts are terminal and evaluable.

Physical child artifacts remain authoritative under ADR 0148.  The checkpoint
is still a routing cursor only, and the transient set is neither persisted nor
exposed through SDK, MCP, or another wire shape.  Completed siblings are not
invoked and their artifacts are not rewritten.

## Consequences

Fresh invocation with a live running child has no transient eligibility and
continues to report that child as `running`; it is never duplicate-dispatched.
For an interrupted same-run resume, completion of the rearmed child returns
control to the ordinary graph order: `contract_check`, then
`cross_final_acceptance`.

`cross.midchild-interrupt-redispatch` and the default `orcho-qa`
qualification are post-promotion, operator-owned deferred evidence.  They are
not substituted by this source-level change or its targeted tests.

## Rejected alternatives

1. **Persist a retry or rearm flag.** Rejected: it would create a second
   lifecycle authority and state synchronization problem.
2. **Infer an interrupted process from events, transcripts, PID ownership, or
   a process ledger.** Rejected: canonical child facts already provide the
   bounded X2 seam; process recovery is a separate design.
3. **Add a separate resume selector or generic recovery state machine.**
   Rejected: it would bypass dependency ordering and duplicate the graph
   scheduler.
4. **Treat every running child as retryable.** Rejected: a fresh live child
   could receive duplicate work.

## Out of scope

- generic recovery, PID/process ownership, or a recovery ledger;
- mono-pipeline runtime changes;
- parallel scheduling;
- persisted checkpoint/graph changes; and
- public SDK, MCP, CLI, or status/reason vocabulary changes.
