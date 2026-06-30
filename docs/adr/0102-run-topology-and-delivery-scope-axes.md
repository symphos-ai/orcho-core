# ADR 0102 — Run topology and delivery-scope as axes independent of the semantic profile

Status: Accepted

Extended by: [ADR 0107 — companion-repo delivery disclosure](0107-companion-repo-delivery-disclosure.md).
ADR 0107 widens the *disclosure* dimension of the delivery-scope gate decided
here — companion repositories are also detected from the durable plan scope,
each carries a typed `dirty` / `committed` / `planned_requirement` state, and a
multi-repo run whose primary shipped while a declared companion stayed dirty
finalizes with a caveat instead of a green DONE. The strict/expanded blocking
semantics and the `meta.auto_detect` form decided in this ADR are unchanged.

## Context

Auto-detect (ADR 0064 alignment work; the selector dispatch in
`pipeline/project/auto_detect.py`) resolves a run's *semantic profile* and
*operating mode* from the task text. But "what kind of work" is not the same
question as "how many repositories does this work span" or "where may the
finished diff land". A task can be a `feature` (one semantic profile) that
nonetheless touches a wire contract shared by a second repository — so a
mono-repo run would silently leave the sibling repository inconsistent, or
worse, deliver a half-change.

Two gaps followed from collapsing these questions onto the single profile axis:

1. There was no durable, typed signal that a run *probably* spans more than one
   repository, and no operator-facing way to act on that signal without
   silently turning a single-repo run into a multi-repo one.
2. Final delivery only ever looked at the run's own checkout. Changes a run
   made in a *sibling* repository (registered in the workspace, outside the
   primary checkout) were invisible to the delivery gate — neither disclosed
   nor guarded.

`SemanticProfile` is a closed enum and must stay that way: `cross` /
`auto-detect` are deliberately **not** members (constructing them raises
`ValueError`). Overloading it with a topology value would break that closure
and conflate three orthogonal decisions.

## Decision

Model three **independent axes** and enforce the third at delivery time.

### Axis 1 — semantic profile (unchanged)

The goal-shaped work kind (`feature`, `small_task`, `migration`, …) plus the
operating mode. Resolved by the detector as before. Closed enum; no new
members.

### Axis 2 — run topology (`RunTopology`)

A new closed enum in `pipeline/runtime/run_shape.py`:

- `mono` — a single-repository run (the default).
- `cross_recommended` — the deterministic topology heuristic
  (`pipeline/runtime/topology_detection.py`) found signals that the work likely
  spans more than one repository, and *recommends* a cross-project run.

The heuristic is provider-neutral and model-free: it lower-cases the task text
and substring-matches it against a data-driven signal table
(`auto_detect.topology_signals` in `config.defaults.json`, phrase → project
aliases). A match yields `cross_recommended` plus the ordered union of
implicated project aliases (the primary alias first) and a short neutral
reason; no match yields `mono`. The table is a workspace-overridable default,
not logic hard-coded in the engine.

### Axis 3 — delivery scope (`DeliveryScope`)

A second new closed enum, governing how the finished diff is collected and
validated at delivery:

- `strict_mono` — only the primary repository is in scope; changes in sibling
  repositories are a violation.
- `expanded_mono` — the run stays single-repo, but sibling-repository changes
  are *disclosed* (per alias) rather than treated as a violation.
- `cross` — a genuine multi-repository delivery (handled by the cross
  pipeline, not the mono delivery gate).

### The explicit-choice invariant

A `cross_recommended` topology is a **recommendation only**. It never changes
the resolved `actual_profile`, never starts a cross run, and never widens
delivery on its own. Across every auto-detect resolution branch — including
the trusted / non-interactive path — `delivery_scope` stays `strict_mono`
unless an operator makes an explicit choice.

That choice is a typed model (`TopologyChoice` /
`apply_topology_choice`): the three operator options map to
`cross` / `expanded_mono` / `strict_mono` respectively, changing only the
`delivery_scope` field. The CLI surfaces an `Auto-detect result` block and the
three choices for a high-confidence cross recommendation; choosing "start
cross" surfaces an explicit directive (a ready-to-run cross command) rather
than silently converting the current mono process into a cross run. A
non-interactive run never prompts, never starts cross, and never widens
delivery — it only records the recommendation in durable meta.

### Durable form (`meta.auto_detect`)

The persisted auto-detect block (written only for runs started through the
auto-detect selector) gains four additive fields alongside the existing
profile/mode decision: `recommended_topology`, `delivery_projects` (the
implicated alias list), `topology_reason`, and `delivery_scope`. The whole
serialized resolution payload is persisted and round-trips back through the
typed resolution, so `delivery_scope` + `delivery_projects` are the durable
input the delivery gate reads. Runs that did not use auto-detect carry no
block — unchanged.

### Enforcement semantics (multi-repo, delivery time)

Enforcement lives in a focused module (`pipeline/engine/delivery_scope.py`),
called by a thin hook in the commit-delivery executor right after it computes
the run-owned changed paths. It has two clearly separated parts:

- **Collection (I/O).** For each alias in `delivery_projects`, resolve the
  repository path through the workspace alias config, **skip the primary**
  (its diff is already what the delivery gate ships), and collect each
  remaining *sibling* repository's dirty files. Paths are normalised per alias
  as `[alias]/relative/path`. Collection degrades softly: an unregistered or
  missing alias yields no entry and never crashes delivery.
- **Classification (pure).** Given the resolved scope and the per-alias sibling
  changes:
  - `expanded_mono` with sibling changes → delivery **proceeds** and the
    multi-project delivery is **disclosed** (the per-alias sibling files ride
    on the decision);
  - `strict_mono` with sibling changes → a typed, **reversible** pause: the
    decision is parked as a decidable gate carrying
    `blocker='delivery_scope_violation'` and the per-alias disclosure, never an
    exception or an unstructured crash. The operator can expand the scope or
    skip / halt; shipping actions are refused while the gate is blocked.
  - `cross` → not applicable to the mono gate (delivered by the cross
    pipeline);
  - no sibling changes, or **no `delivery_scope` recorded at all** → no-op: an
    existing explicit-mono run delivers exactly as before. This is the
    no-regression guarantee.

### MCP wire extension

The companion MCP projection is extended in lockstep (a same-change wire
update, per the SDK-wire discipline):

- the auto-detect projection surfaces `recommended_topology`,
  `delivery_scope`, `projects`, `topology_reason`, and the three typed topology
  next-action choices (preserving the "never an empty profile argument"
  invariant);
- the delivery-gate projection surfaces the typed
  `delivery_scope_violation` blocker and the per-alias sibling disclosure, with
  shipping actions reported as blocked while skip / halt stay available.

The MCP schema snapshot is regenerated additively and an end-to-end mock smoke
covers both surfaces.

## Consequences

- Topology and delivery scope are first-class, durable, provider-neutral
  axes that any embedder can read and act on; the semantic-profile enum stays
  closed (`SemanticProfile('cross')` / `('auto-detect')` still raise).
- A cross recommendation is advisory: nothing converts a mono run into a cross
  run, or widens its delivery, without an explicit operator choice.
- Delivery now sees sibling-repository changes. `expanded_mono` discloses them;
  `strict_mono` parks a reversible, typed blocker instead of silently shipping
  a partial change or crashing. Runs with no recorded delivery scope are
  byte-identical to prior delivery behaviour.
- `meta.auto_detect` and the MCP wire carry additive fields only; absence of
  the new fields (a run predating this change, or a non-auto-detect run) reads
  as `mono` / `strict_mono` / empty and triggers no enforcement.

A full operator authoring guide for choosing and overriding delivery scope is
deferred. <!-- TODO(orcho-phase-topology): expand delivery-scope authoring guide -->

## References

- [ADR 0107 — companion-repo delivery disclosure](0107-companion-repo-delivery-disclosure.md) (extends the disclosure dimension of this ADR)
- [ADR 0064 — semantic profiles and operating modes](0064-semantic-profiles-and-operating-modes.md)
- [ADR 0099 — deferred delivery decision gate and out-of-band decide surface](0099-deferred-delivery-decision-gate.md)
- [ADR 0032 — commit-decision gate](0032-commit-decision-gate.md)
- `pipeline/runtime/run_shape.py` — `RunTopology` / `DeliveryScope` enums
- `pipeline/runtime/topology_detection.py` — deterministic topology heuristic
- `pipeline/engine/delivery_scope.py` — multi-repo collection + classification
- `docs/architecture/overview.md` — the three-axis typology
