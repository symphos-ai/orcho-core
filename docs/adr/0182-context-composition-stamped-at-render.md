# ADR 0182 — Context composition is stamped at render time from wire segments

- **Status:** Proposed (lands with the first stamping slice)
- **Date:** 2026-08-23
- **Extends:** [ADR 0029](0029-context-lifecycle.md)
- **Related:** [ADR 0026](0026-session-aware-prompt-parts.md),
  [ADR 0028](0028-cache-first-physical-wire-layout.md)

## Context

The ADR 0029 evidence family answers *how much* context an invocation used
(`context_growth`), *how full* the window is and how trustworthy that reading
is (`context_pressure` and its source hierarchy `runtime_reported →
provider_usage → orcho_estimated → config_static → unknown`), *what could be
cleared* (`output_class`, `context_clearing`), and *whether the runtime
compacted itself* (`runtime_compaction`). None of it answers **what the
context was made of**: how many of an invocation's wire bytes were engine
instructions versus task contract versus operator input versus run artifacts
versus material the runtime gathered on its own.

Without that split there is no honest cost attribution below the invocation
level, no way to see history carryover growing across a long session before
it consumes the budget, and no baseline against which any future
context-shaping mechanism could be measured.

Three facts about the existing surfaces constrain the design:

1. **The persisted prompt-render trace cannot reconstruct composition.** It
   records the ordered part ids and the total wire size, but not the size of
   each part. A post-hoc join is impossible; composition must be measured
   while the rendered prompt object is still alive.
2. **The source envelope is the wrong measurement base.** It includes parts
   that were omitted or dropped by delta selection — it describes what
   *could* have been sent (an audit surface), not what went on the wire.
   The effective turn's segments are the wire truth: each segment's text is
   the exact bytes it contributed, separator glue included, and the turn's
   text is wire-identical to what the runtime receives.
3. **Only the engine's own rendering is exactly measurable today.** Sizes of
   material the runtime gathers itself (file reads, tool results, session
   carryover) are not exposed by any current runtime; call parameters do not
   reveal result sizes, and simple arithmetic over provider totals breaks
   under cache-read/cache-create accounting. Pretending otherwise would
   manufacture precision.

## Decision

### Stamp at render, measure segments

A `context_composition` record is written per invocation at the point of
rendering, while the effective turn is available. Buckets are computed from
the turn's segments; each segment is attributed to exactly one bucket by the
identity/type of its owning prompt part. The record is durable evidence in
the ADR 0029 family style: a pure extractor, identity + attribution +
correlation fields, no session mutation.

Every record must satisfy the conservation invariant, enforced as a contract
test:

```
sum(bucket chars) == len(effective_turn.text) == persisted wire size
```

### Mutually exclusive buckets

```
orcho_instructions   # engine-rendered phase contract and instructions
task_contract        # task file / plan / subtask criteria
operator_input       # operator feedback / handoff input
artifact_context     # run artifacts embedded into the prompt
runtime_managed      # everything the runtime carries or gathers itself
unknown              # the honest remainder
```

The taxonomy is a small closed set. Sub-detail of `runtime_managed` (file
reads, tool results, carryover) may only appear when a runtime reports it
itself, under namespaced `x_<runtime>_*` keys, recorded verbatim.

### Per-unit measurement with per-value sourcing

Each bucket carries independent measurements per unit, so exact character
counts and later token readings coexist without a schema change:

```
bucket:
  chars:  {value: int | null, source: str}
  tokens: {value: int | null, source: str}
```

The ADR 0029 source hierarchy gains one label: **`orcho_rendered`** — a
writer-exact measurement taken at render time. Rules:

- `chars` for engine-rendered buckets: `orcho_rendered` (exact).
- `tokens`: `orcho_estimated` at best, `unknown` otherwise, until a runtime
  reports a breakdown (`runtime_reported`).
- `runtime_managed` starts as `{value: null, source: unknown}` in both
  units. A runtime that cannot say is recorded as not saying; `unknown` is a
  legal, honest value, never a failure.

Explicitly rejected as false precision: estimating tool-result sizes from
tool-call parameters, and deriving carryover as `previous total − new input`
(incorrect under provider cache accounting).

### `invocation_id` correlates the evidence family

The render boundary stamps a writer-generated `invocation_id`:

```
one successful logical invocation
  → one invocation_id
  → the same id on the prompt-render trace, context_growth,
    context_composition, and the invocation usage outcome
  → the per-subtask usage capture links it to the subtask id
```

A provider session id is not a correlation key — one session serves many
invocations — and one subtask legitimately owns several invocation ids
(repair turns included); none may be lost. The existing per-subtask usage
records (direct capture with resume-safe merge) are extended with the
composition summary through this link; no parallel aggregation path is
introduced.

### Observe-only

The engine measures and records; it does not shape, trim, or veto runtime
context. The boundary stands: the engine owns phase contracts, the runtime
owns its context. Any future context-shaping mechanism plugs in against this
record as its baseline and is measured by the same fields.

`schema_version=1` identifies the initial durable shape. It is not a
backward-compatibility promise: no legacy shape, no dual-read, no migrator;
before publication the shape changes in place, and an unsupported version
fails explicitly.

## Consequences

- Per-invocation composition becomes durable evidence with an enforced
  conservation invariant, so cost attribution below the invocation level and
  carryover-growth diagnosis stop requiring guesswork.
- The render boundary acquires one new obligation (measure segments, stamp
  the record, mint `invocation_id`); runtimes acquire none — a runtime that
  reports nothing is recorded honestly as `unknown`.
- `invocation_id` gives the whole ADR 0029 evidence family a single
  correlation key, replacing ad-hoc joins.
- Exactness is bounded and labeled: chars are exact where the engine
  rendered them (`orcho_rendered`); token values are never rendered as fact
  unless a runtime reported them.
- When any runtime starts reporting its own context breakdown, the writer
  flips that bucket's source to `runtime_reported` and populates namespaced
  sub-keys with no consumer change — the same forward-compatibility pattern
  as the rest of the ADR 0029 family.
