# ADR 0175 — Decidable means decidable now

- **Status:** Accepted
- **Date:** 2026-07-29
- **Supersedes in part:** [ADR 0099](0099-deferred-delivery-decision-gate.md)

## Context

A deferred delivery or correction gate is durable context on
`meta.commit_delivery`. Earlier projections treated the presence of a pending
gate as sufficient to offer `decide_delivery`, even when the run lifecycle was
already stopped. That made a historical gate appear actionable after `done`,
`halted`, `failed`, `interrupted`, or `cancelled`, although the engine cannot
safely apply a decision until it has resumed the run's checkpoint.

The same lifecycle fact must govern the SDK read projection and the SDK writer.
Otherwise a client can render a decision that the writer rejects, or a writer
can mutate durable context after its lifecycle has stopped.

## Decision

`decidable` means **decidable now**, not merely that a durable delivery record
exists.

The shared delivery-gate eligibility predicate uses the canonical lifecycle
vocabulary from `pipeline.run_state.status_vocab`:

- `TERMINAL_SUCCESS_STATUSES`;
- `RESUMABLE_TERMINAL_STATUSES`;
- `TERMINAL_CROSS_STATUSES`.

The predicate applies only to a decision-shaped gate block (`pending`,
`fix_requested`, or a rejected-release gate). For such a gate on any of those
stopped statuses, including `cancelled`, `delivery_decision_state` preserves
its `delivery` or `correction` kind and core explanation, but returns
`decidable=false` with no available or blocked actions and no default action.
A gate-less run remains `kind="none"`, and a completed, skipped, or malformed
block on a stopped run reads "no pending delivery gate" — never resume-first.

`decide_delivery` calls the same predicate before guards or executors. It
returns a typed resume-required blocker and exactly the projection reason; it
does not rewrite `meta.json` or the durable gate.

The operator journey is resume-first:

1. Resume the stopped checkpoint.
2. The lifecycle re-parks the unchanged gate in a live pause such as
   `awaiting_commit_decision`.
3. The same gate becomes decidable again and existing ordered actions are
   offered.

`commit_delivery_pending` and `commit_delivery_scope_blocked` remain durable
diagnostic reasons, but do not make a parent checkpoint-inert. A
`fix_requested` correction remains a retained-change follow-up subject; this
decision does not introduce a new durable field or a parallel gate record.

## Consequences

- SDK, CLI, and MCP clients direct stopped delivery gates to resume rather than
  offering a same-place decision.
- The durable gate's kind, timestamp, release context, and evidence remain
  inspectable without a mutation.
- A live gate retains the established action ordering, hard guards, and writer
  behavior.

## Supersession note

ADR 0099 remains authoritative for deferred delivery persistence and the
out-of-band decision surface. This ADR supersedes only its implication that a
`halted` `commit_delivery_pending` record can be decided directly or must be
excluded from checkpoint resume selection.
