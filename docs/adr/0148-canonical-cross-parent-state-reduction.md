# ADR 0148 — Canonical cross-parent state reduction

- **Status:** Accepted
- **Date:** 2026-07-21
- **Related:** ADR 0038, ADR 0050, ADR 0145, ADR 0146

## Context

A cross run currently exposes several individually useful but differently
shaped state sources:

- the parent `meta.json` session and its embedded child-session snapshots;
- each physical child run's `meta.json`, events, and verification ledger;
- `cross_checkpoint.json`, which records resume cursors and decision-routing
  hints;
- parent and child phase, gate, and handoff events;
- SDK status and run-control readers that project only subsets of those facts.

No single reduction defines what the parent is doing, whether every child has
produced evaluable contract input, which engine operation is active, or which
child owns a pending decision. Consumers therefore combine coarse parent
status, checkpoint sub-status, and child status independently. A locally
plausible projection can hide an active verification gate, treat a terminal
child as still running, or let a checkpoint hint stand in for child readiness.

ADR 0146 made dispatch outcome and contract-check admission fail closed. It did
not define the durable parent read model used after dispatch, across resume, or
by SDK consumers. Executable dependency scheduling is a separate decision and
must not be introduced as a side effect of fixing the read model.

## Decision

Orcho will define one typed, pure cross-parent reducer. The reducer accepts a
snapshot of durable facts and returns an immutable `CrossParentState`. It does
not read files, invoke providers, write checkpoints, or advance execution.
Runtime and SDK adapters assemble facts from their respective sources and call
the same reducer.

The reduced state has these dimensions:

- an ordered child projection for every declared alias;
- zero or more active operation identities;
- at most one pending operator decision owned by a concrete node or child;
- contract-input admission and release-readiness as separate facts;
- blockers and consistency violations with stable machine-readable reasons;
- a parent state class and, when terminal, its terminal disposition.

Each child projection keeps the following concerns separate:

| Dimension | Meaning |
| --- | --- |
| execution | `pending`, `running`, `paused`, `terminal`, or `inconsistent` |
| active operation | Current phase or engine-owned gate identity, when observed |
| contract input | `evaluable` or `not_evaluable` |
| release disposition | `approved`, `rejected`, `unavailable`, or `not_applicable` |
| pending decision | Exact handoff identity and available actions, when present |

A child with a complete rejected release is contract-evaluable but is not
release-ready. A failed, interrupted, missing, malformed, or non-decision-ready
paused child is not contract-evaluable. No generic `ready` boolean may collapse
these distinctions.

The parent state class is one of:

- `running` — at least one phase or engine operation is active;
- `awaiting_operator` — an exact pending decision is the next progress-making
  boundary;
- `blocked` — no operation is active and durable blockers prevent the next
  admission;
- `ready` — the current canonical stage's deterministic prerequisites hold;
- `terminal_success`, `terminal_failure`, or `terminal_halted`;
- `inconsistent` — durable facts contradict one another and no safe state can
  be inferred.

`ready` is a read-model classification, not permission to deliver. Contract
admission, cross-final admission, and delivery each retain their own explicit
prerequisites.

## Source authority and precedence

The reducer applies these rules:

1. Declared aliases and their order come from the parent run manifest.
2. Exact child execution and terminal facts come from the physical child run
   and its canonical status vocabulary. An embedded child snapshot may supply
   the same facts during the live call but may not contradict the physical
   child silently.
3. Engine-owned gate state comes from typed gate events and the scheduled-gate
   ledger, never terminal prose or process-name inspection.
4. The active handoff payload supplies decision identity and available actions.
   The checkpoint supplies the cross routing kind and child alias used by
   resume. Both must agree when both are present.
5. `cross_checkpoint.json` remains a resume optimization and decision-routing
   artifact. `sub_status=done` is not evidence that a child is terminal,
   contract-evaluable, or release-ready.
6. Terminal parent metadata is accepted only when it is compatible with the
   reduced child and gate facts. Contradictions produce `inconsistent`; they are
   not resolved by last-writer-wins precedence.

Multiple active operations are represented as an ordered tuple. The initial
scheduler may remain serial, but the read model must not make serial execution
an invariant that a later scheduler has to break.

## Runtime and read-side use

The cross coordinator uses the reducer at transition boundaries before
contract admission, cross-final admission, and terminal finalization. It does
not duplicate the reduction with local status flags.

Core exposes a read-only SDK loader for the same `CrossParentState`. Existing
coarse `RunStatus.status` and checkpoint fields remain persisted for lifecycle
and resume compatibility, but they are inputs or derived summaries rather than
parallel business truth. CLI, MCP, and other consumers can adopt the canonical
projection without reimplementing it; consumer projection is a later bounded
slice.

The reducer output itself is derived and is not persisted as a second ledger.
Replaying the same durable facts must produce an equal value.

## Consequences

- Parent status, child readiness, active gates, and pending decisions can be
  explained from one typed value.
- A running engine gate no longer has to masquerade as an empty interval
  between phases.
- Checkpoint corruption or staleness cannot manufacture child success.
- Contract compatibility remains distinct from missing or incomplete child
  evidence.
- SDK consumers gain a stable core source without parsing logs or parent prose.
- Contradictions become explicit invariant failures instead of presentation
  guesses.

The reducer adds a focused cross-state module and small fact-assembly adapters.
It must not add another responsibility to the cross coordinator or finalizer.

## Rejected alternatives

### Treat the checkpoint as the canonical parent state

Rejected because the checkpoint is intentionally small, best-effort, and
resume-oriented. Its `sub_status` cannot express active engine operations,
contract evaluability, release disposition, or contradictions with child
artifacts.

### Persist a second cross-state ledger

Rejected because the required facts already exist. A second mutable ledger
would introduce another synchronization problem and could disagree with the
child runs it summarizes.

### Let each consumer reduce state independently

Rejected because this is the current failure mode. Presentation layers must
project a canonical result, not reconstruct lifecycle semantics.

### Build the executable cross DAG in the same change

Rejected because dependency compilation and scheduling are separate from
state reduction. The reducer must first describe the current fixed topology
correctly; executable DAG scheduling can then consume the same node vocabulary.

## Out of scope

- compiling or scheduling an executable cross DAG;
- parallel dispatch changes;
- cross-contract evidence completeness semantics;
- blocker-specific handoff action policy;
- MCP or Web presentation changes;
- generic child exception recovery policy;
- new delivery or verification policy.
