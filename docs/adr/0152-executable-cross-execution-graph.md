# ADR 0152 — Executable cross execution graph

- **Status:** Accepted
- **Date:** 2026-07-22
- **Supersedes:** [ADR 0057](0057-cross-dispatch-honors-declared-dependencies.md)
- **Related:** [ADR 0054](0054-typed-cross-plan-json.md), [ADR 0146](0146-cross-child-outcome-and-gate-admission.md), and [ADR 0148](0148-canonical-cross-parent-state-reduction.md)

## Context

The validated cross plan already carries typed per-alias `depends_on` edges.
It rejects missing aliases, dangling dependencies, self-edges, and cycles, and
the normalized `CrossTaskPlan` preserves those edges. Runtime dispatch does not
consume them: it iterates projects in input order inside a fixed coordinator
sequence. Resume similarly knows which durable children are complete but does
not derive the first unfinished ready unit from the plan dependencies.

ADR 0057 proposed topological child ordering before the typed plan and
canonical parent-state reducer were complete. ADR 0148 then established one
pure reduction for child execution, active operations, decisions, blockers,
and parent state, while explicitly leaving executable scheduling to a later
decision. This ADR closes that remaining gap without replacing the child
project pipeline or moving scheduled-gate authority into the cross parent.

## Decision

### Immutable structural graph

Core compiles each approved cross plan and effective cross profile projection
into one immutable `CrossExecutionGraph`. Compilation happens after typed plan
validation and before project dispatch. The persisted graph snapshot contains
structure and compile identity only; it is not a mutable execution ledger.

Structural nodes use one typed vocabulary with an opaque stable identity,
kind, dependency identities, owner, and executor. The initial structural kinds
are:

- `global` for cross-level planning and validation steps;
- `project` for one child pipeline per declared alias;
- `cross_gate` for runner-owned `contract_check` and
  `cross_final_acceptance` gates.

Global planning nodes remain in the graph as already-satisfied predecessors
when compilation occurs. This makes the complete execution shape explainable
without rerunning planning. Project edges come only from the validated
`CrossTaskUnit.depends_on` values. Narrative `implementation_order` never
controls execution. Independent nodes use declared alias/input order as the
stable topological tiebreak.

`contract_check` depends on every required project node.
`cross_final_acceptance` depends on `contract_check`. A disabled runner-owned
gate remains structurally visible with a derived skipped/satisfied disposition
rather than disappearing and changing the graph shape.

Compilation rejects duplicate identities, unknown owners, dangling or
self-referential edges, cycles, and profile steps that cannot be assigned a
valid structural owner. It does not infer dependencies from prose or from
`produces` and `consumes` descriptions.

### Derived graph state

Graph state is a pure projection of the immutable graph and canonical durable
facts. It classifies structural nodes as `pending`, `ready`, `running`,
`blocked`, `completed`, or `skipped` and supplies stable reason codes. The
projection reuses ADR 0148 child and parent state; it does not duplicate child
status precedence or trust checkpoint `sub_status` as completion evidence.

The graph snapshot is persisted once as `cross_execution_graph.json`. Mutable
node status is not persisted as a second ledger. Replaying the same graph and
durable fact snapshot must return an equal graph-state value.

`cross_checkpoint.json` remains a best-effort resume and decision-routing
artifact. It can locate a continuation cursor but cannot make a node complete,
ready, or successful.

### Nested child operations and execution ownership

Normal child phases and engine-owned scheduled gates are exposed through the
same typed operation projection used by graph readers. They are nested under
their owning project node and carry an exact owner alias and executor identity.
They are not parent-schedulable structural nodes.

The child project engine remains the sole authority for its profile flow and
scheduled-gate declaration, selection, execution, retry, receipts, and
disposition. The cross parent observes those operations through canonical
child facts. It must never select a child gate, execute its command, or write
its receipt.

This hierarchical boundary provides one vocabulary for CLI, SDK, MCP, and
other readers without flattening two execution engines into competing owners.

### Serial scheduler and resume

The first scheduler strategy is serial. At every structural transition it
selects the first unfinished ready node in stable topological order. A terminal
non-success dependency blocks its transitive dependents with an explicit
reason; a blocked consumer is not dispatched against an unavailable producer.

Resume loads the immutable graph, reconstructs canonical durable facts, and
performs the same pure readiness reduction. It continues from the first
unfinished ready node. A completed child is not rerun merely because a
checkpoint is missing or stale, and a checkpoint cannot skip an incomplete
child.

Parallel waves are not part of this decision. Multiple active operations
remain representable, as required by ADR 0148, but serial dispatch is the only
initial scheduling strategy.

### Consumer boundary

Core owns graph compilation, readiness reduction, scheduling, persistence, and
the read-only SDK projection. Consumers project the core result and do not
reconstruct dependencies or readiness from events, prose, or checkpoint
flags. A downstream wire projection ships only after the core contract has
been promoted and validated.

## Rollout

1. **C1 — graph contract and compiler.** Add focused immutable graph types,
   compile the validated plan/profile shape, persist the structural snapshot,
   and test invalid graphs plus stable topological order. Do not alter live
   dispatch in this increment.
2. **C2 — graph-state reducer and serial scheduler.** Derive readiness from
   canonical facts, drive project order and failure blocking from the graph,
   and make resume select the first unfinished ready node. Expose the read-only
   core SDK state.
3. **M1 — consumer projection and stable journey.** After core promotion,
   project the canonical graph state through MCP when required and run one
   stable cross journey proving reverse-input dependencies, nested gate
   visibility, resume, and terminal reduction.

Each increment must preserve the existing child verification ownership and
must not add graph policy directly to the coordinator facade.

## Consequences

- The approved typed plan becomes executable rather than advisory.
- Child ordering and producer-failure blocking are deterministic and
  explainable.
- Resume and live readers use the same readiness semantics as fresh dispatch.
- The persisted artifact is stable structure, while changing state remains a
  derivation from existing durable facts.
- Child scheduled gates become visible in the graph journey without creating
  a second executor or receipt writer.
- The initial implementation remains serial, so this ADR does not introduce
  concurrency races or parallel worktree policy.

## Rejected alternatives

### Sort the existing project loop only

Rejected because it fixes the happy-path order but leaves resume and readers
to recreate readiness and blocking independently.

### Flatten child phases and scheduled gates into a parent-owned DAG

Rejected because it duplicates the project profile runner and scheduled-gate
engine. Shared presentation vocabulary does not imply shared execution
authority.

### Persist mutable graph status

Rejected because child runs, gate ledgers, handoffs, and cross-gate artifacts
already contain the authoritative facts. Another mutable ledger would create a
new synchronization problem.

### Parse narrative implementation order or infer edges from descriptions

Rejected because execution dependencies already have a typed field. Prose and
descriptive producer/consumer text cannot define a deterministic scheduler.

### Introduce parallel scheduling in XF2

Rejected because dependency correctness, failure propagation, and resume can
be proven with the existing serial execution strategy. Parallelism requires a
separate concurrency and isolation decision.

## Out of scope

- parallel execution waves;
- cross-contract evidence completeness and semantic admission;
- blocker-specific handoff action policy;
- new verification or delivery policy;
- a second checkpoint, state ledger, or compatibility graph shape;
- presentation redesign beyond projecting the canonical core result.
