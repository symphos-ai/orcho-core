# ADR 0057 — Cross dispatch honors declared inter-project dependencies

- **Status:** Proposed
- **Date:** 2026-05-29
- **Deciders:** project owner
- **Relates to:** [ADR 0052](0052-structured-handoff-plan-slices.md),
  [ADR 0054](0054-typed-cross-plan-json.md)
- **Depends on:** ADR 0054 (typed cross-plan JSON) landing first

## Context

The cross architect produces an `## Implementation Order` section
("[api] first — schema is the dependency root; [web] second — consumes
the now-working endpoint"). The runtime does not honor it.

`run_project_dispatch` (`pipeline/cross_project/project_dispatch.py`)
iterates the projects in **input-alias order**:

```python
for alias, project_path in ctx.projects.items():   # ctx.projects: dict[str, Path]
    _dispatch_one_alias(...)
```

`ctx.projects` is a plain `dict[str, Path]` filled in the order aliases
were supplied on the CLI/SDK. The plan's implementation order never
feeds back into it. Two structural facts make this unfixable today:

1. **No typed channel.** `## Implementation Order` is extracted only as a
   free-text string (`ParsedCrossPlan.implementation_order: str`,
   `plan_parser.py`). It is prose for the implementer to read, never a
   machine-readable alias sequence. This is the same root cause ADR 0054
   names: the cross architect is the one planning surface with no typed
   response contract, so everything downstream is regex/heading scraping.

2. **Dependency vocabulary exists but is unwired.** `ProjectStep.depends_on`
   and a *pairwise* ordering helper `_all_ordered_by_depends_on` live in
   `pipeline/cross_project/types.py`, but there is **no topological sort**
   (the code comment marks the helper as a stub; the full Kahn check was
   planned for a later `plan_parser` integration that never landed), and the
   live dispatch path does not construct `ProjectStep`s or read
   `depends_on` at all. `ProjectStatus.BLOCKED` ("dependency failed →
   never started") is defined but never assigned.

### Observed consequence

In a two-alias run (`web, api`) whose plan ordered **api first** (the
dependency root) and **web second** (consumes `PUT /api/users`), dispatch
ran `web` then `api` (input order). `web`'s implement → review →
**final acceptance** all ran and returned `ship_ready=true` against an
endpoint that did not yet exist (`api` was still pending; the route
returned 405). The plan's ordering existed precisely to make web's
verification meaningful, and it was silently ignored. A failed/late
producer cannot block its consumer because there is no dependency edge to
block on.

ADR 0054 fixes the **channel** (typed JSON) but, as drafted, does not
fix ordering: its schema carries `implementation_order` as prose steps
and `subtasks[]` **without** `depends_on`. Necessary, not sufficient.

## Decision (proposed)

Make cross dispatch execute sub-pipelines in an order derived from
declared inter-project dependencies, and block dependents of a failed
producer. Three parts:

1. **Schema — declare dependencies (extends ADR 0054).** Add
   `subtasks[].depends_on: [<alias>, ...]` to `CrossPlanSchema`. Explicit
   `depends_on` is canonical. As ergonomic sugar, when `depends_on` is
   omitted the edge set MAY be inferred from `produces`/`consumes` (a
   consumer of a surface another alias produces depends on it); explicit
   always wins. Validated at plan-write time: unknown alias, self-edge,
   or cycle → hard reject (parity with the other cross JSON gates).

2. **Topological dispatch order.** `run_project_dispatch` orders
   `ctx.projects` by the dependency edge set before the loop, using a
   real topological sort (promote `types.py`'s pairwise helper to a Kahn
   sort — the previously stubbed work). **Stable tiebreak =
   original input order**, so the result is deterministic and degrades
   exactly to today's behavior when there are no edges.

3. **Producer-failure gating.** When a producer ends `failed`/`blocked`,
   its transitive dependents are recorded `ProjectStatus.BLOCKED` and
   skipped rather than run against a contract that was never delivered.
   Blocked aliases surface to `cross_final_acceptance` as release
   blockers (same shape ADR 0025 uses for a crashed child), so the run
   fails honestly instead of emitting `ship_ready` on a consumer whose
   producer never landed.

## Scope / non-goals

- **Ordering + gating only; not parallelism.** Cross dispatch is
  sequential today (`parallelism=1`). This ADR orders the existing
  sequence and gates on failure; it does not introduce parallel waves.
  Parallel execution of independent aliases is a separate, later ADR.
- **Depends on ADR 0054.** Inferring order from the free-text
  `implementation_order` prose is explicitly rejected (see Alternatives);
  this ADR consumes a typed `depends_on`/`produces`/`consumes`, which only
  exists once 0054 types the channel. If 0054 is deferred, this ADR is
  blocked, not worked around with prose parsing.
- Does not change the mono pipeline (mono already DAGs typed subtasks).
- Single-developer project, no install base: wire `depends_on` into the
  schema and dispatch **in place**, no `ORCHO_USE_*` flag or dual path.

## Consequences

- Dispatch sequence becomes plan-driven and deterministic; the architect's
  declared order is enforced, not advisory.
- A failed/late producer blocks its consumers instead of letting a
  consumer reach a false `ship_ready` against a contract that was never
  delivered — the concrete failure this ADR exists to close.
- Touches `CrossPlanSchema` (ADR 0054), `plan_parser` (carry `depends_on`
  into `ParsedCrossPlan`/the typed plan), `project_dispatch` (topo order
  + BLOCKED gating), `types.py` (pairwise helper → Kahn sort), and the
  cross checkpoint `sub_status` (handle `blocked`).
- **Wire-adjacent**: `depends_on` is part of the cross-plan contract that
  drives routing/ordering. Ships with an `orcho-mcp` E2E mock smoke in the
  same commit (`orcho-core/CLAUDE.md` MCP per-phase validation rule).

## Alternatives considered

- **Keep input-order; rely on the interface contract for independence.**
  Rejected: the interface contract lets each repo *build* against a shared
  shape, but it does not make verification order or producer-failure
  gating correct. The observed run proves a consumer can be accepted
  before its producer exists.
- **Parse the prose `## Implementation Order` into an alias sequence.**
  Rejected: brittle NLP over free text — exactly the heading/marker
  scraping ADR 0054 deletes. Order must be a typed field, not inferred
  from prose.
- **Full parallel DAG (waves) now.** Deferred: parallelism is an
  independent concern with its own worktree-isolation and
  shared-`project_dir` race questions (`types.py` already guards the
  shared-dir case). Order + gate first; parallelize later.

## Migration sketch (for the implementing phase)

1. `CrossPlanSchema` (ADR 0054) — add optional `subtasks[].depends_on`;
   validate alias refs + no self-edge + acyclic on write.
2. `plan_parser` — carry `depends_on` (and `produces`/`consumes` for the
   inference fallback) into the typed plan / `ParsedCrossPlan`.
3. `types.py` — promote `_all_ordered_by_depends_on` to a Kahn
   topological sort returning a stable order (input-order tiebreak);
   cycle → raise.
4. `project_dispatch` — sort `ctx.projects` by that order before the
   loop; when an alias's transitive producer is `failed`/`blocked`, set
   `sub_status[alias] = "blocked"` and skip, recording a
   `ProjectStatus.BLOCKED` sub-session.
5. `cross_final_acceptance` — treat `blocked` aliases as release blockers.
6. Update mock/scripted providers + tests to emit `depends_on`; add the
   `orcho-mcp` mock smoke.
