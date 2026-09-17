# ADR 0189 — Unpriced usage is visible in `metrics.json`

- **Status:** Accepted
- **Date:** 2026-09-06
- **Related:** [ADR 0020](0020-run-evidence-in-core.md),
  [ADR 0021](0021-public-sdk-boundary.md),
  [ADR 0035](0035-terminal-status-and-resume-observability.md),
  [ADR 0166](0166-run-status-spend-and-liveness.md)

## Context

`metrics.json` carries a dollar-denominated **cost reference**
(`total_cost_usd_equivalent` and its per-record `cost_usd_equivalent`).
It has two sources: a cost the runtime/endpoint reported, or — when the
provider reported exact tokens but no cost — an estimate from Orcho's
local pricing table.

The local table can miss. A new or self-hosted model id has no row, so
`estimate_cost_usd` returns `None` and the invocation contributes
nothing to the total. Until now that produced:

- a **one-shot stderr warning** in the cross-level usage capture, per
  process, per model — a terminal artifact that survives nothing;
- a durable `metrics.json` whose total was silently smaller, with no
  key anywhere saying so.

The failure mode is not "a number is missing" — it is "a number is
present and reads as complete." An operator comparing a run's DONE line
against a provider invoice, or a dashboard summing spend across runs,
has no way to tell a genuinely cheap run from one whose most expensive
phase ran on a model the table did not know.

Readers cannot recover the fact on their own. A `None`/absent cost on a
record has several causes that are not "unpriced": dollar accounting is
disabled, the phase burned no tokens, no model was resolved, or the
token counts were heuristic (`tokens_exact: false`) and the mono
resolver deliberately refuses to price them so byte estimates never
become dollar-looking facts. Any reader-side re-derivation would
conflate all of these, and would additionally have no way to name the
model: a `phases` rollup's `model` collapses to `"mixed"` as soon as a
phase spans two models.

## Decision

**The unpriced fact is recorded durably by the metrics writers, once,
with the exact model ids — as additive optional keys.**

### 1. Three additive keys, never a `false` form

- `cost_unpriced: bool` — on `phase_attempts[]` entries, `phases.<name>`
  rollups, `subtasks.<phase>[]` records, and cross-run `phases` entries.
- `unpriced_models: list[str]` — top-level, sorted, deduped exact ids.
- `total_cost_partial: bool` — top-level, written only next to a present
  `total_cost_usd_equivalent`.

All three are written **only when true / non-empty**. A fully-priced run
keeps its historical key set byte for byte, so nothing downstream sees a
shape change until there is something to say. All three are dollar
semantics and therefore accounting-gated: they join `_ACCOUNTING_KEYS`
and `scrub_accounting_fields` strips them wherever the other cost fields
are stripped.

### 2. One invariant for the marker

`cost_unpriced: true` means **pricing was attempted for this record and
the table returned nothing.** It never means anything else, on any
record, in mono or cross form. Every early return above the pricing
lookup — accounting off, provider cost present, no model, zero tokens —
leaves it false, because those records were never pricing candidates.
Marking them would claim the run lost money it never had a price for.

The invariant is about the lookup, not about token quality, and the two
writers reach the lookup on different terms:

- **Mono** (`_resolve_phase_cost_usd_equivalent`) prices **only exact
  tokens**. A phase with `tokens_exact: false` returns above the lookup,
  so heuristic tokens are never pricing candidates and never carry the
  marker.
- **Cross** (`capture_invoke_usage`) prices **any `total_tokens > 0`**,
  regardless of `token_split_source` — `exact`, `runtime_estimate`,
  `text_estimate_scaled` and `aggregate_total_only` all reach the table
  alike. So a heuristic-split cross invocation on an unpriced model
  *does* carry the marker: the lookup ran and came back empty.

That asymmetry is deliberate, not an oversight. Mono refuses to turn
byte estimates into dollar-looking facts; cross already publishes an
estimated dollar figure for heuristic splits, so when it cannot, the
gap has to be recorded like any other.

### 3. The fact is born once, at the writer

`MetricsCollector` (mono) and `cross_metrics_dict` (cross) are the only
places the fact is created, because they are the only places that know
whether the lookup actually ran:

- The mono resolver `_resolve_phase_cost_usd_equivalent` returns
  `(cost, estimated, unpriced)`; the collector stores `unpriced` on the
  phase record and ORs it into the phase rollup.
- `unpriced_models` is collected from the records that were actually
  marked, taking each record's **own exact `model`** — never a rollup's
  `"mixed"`.
- In cross form the fact itself is born earlier, in
  `capture_invoke_usage`, which sets `cost_unpriced` on the invocation
  record; `accumulate_phase_usage` carries the exact model id into the
  aggregate's `unpriced_models`. `cross_metrics_dict` only **aggregates**:
  it unions ids from the sources that already recorded them (each child's
  own top-level `unpriced_models`, each cross-level entry's
  `unpriced_models`) and marks the contributing `phases` rows. Malformed values arriving from a child
  `metrics.json` on disk are ignored, not merged.
- The durable marker is independent of the stderr warning's per-model
  one-shot dedupe: every unpriced invocation carries it, while the
  warning still fires once.

**Readers must not re-derive it.** The DONE summary reads
`metrics["unpriced_models"]` and appends one inline caveat to the cost
part (`… (excl. unpriced: ghost-model-1)`); it does not infer anything
from a `null` cost. The same rule binds SDK and MCP consumers.

### 4. The total keeps its meaning; a qualifier sits next to it

`total_cost_usd_equivalent` is unchanged in value and rounding — it sums
the priced invocations, exactly as before. `total_cost_partial: true`
sits beside it and says that sum omits at least one invocation, making
the number an explicit **lower bound**; `unpriced_models` names who is
missing. Writing a corrected or inflated total was rejected: Orcho has
no price for those invocations, and inventing one is the same class of
error as pricing heuristic tokens.

### 5. Resume rehydrates the fact

`MetricsCollector.load_from_disk` restores `cost_unpriced` from
`phase_attempts`, so a pause → resume → re-save re-emits
`unpriced_models` (ADR 0035). Without this, a second process whose
pricing table answers differently would drop a fact the first process
had already established.

### 6. No SDK dataclass or `orcho-mcp` change

The keys are optional and additive, and both boundaries already pass the
raw mapping through: `RunMetrics.raw` and `RunStatus.raw_metrics` carry
`metrics.json` verbatim (post-scrub), `RunMetrics.phases` copies the
`phases` dict, and the MCP `RunMetrics.metrics` field is a
`dict[str, Any]` passthrough while `PhaseCost` projects named fields and
ignores extras. No promoted scalar changes: `total_cost_usd_equivalent`
stays the `float` ADR 0166 defined. This slice therefore ships without a
companion `orcho-mcp` schema change — the keys reach MCP clients as-is.

## Consequences

- A run whose spend is partly unpriced says so in three places that
  survive the process: the durable artifact, the DONE line, and any
  consumer reading `raw_metrics`.
- `total_cost_usd_equivalent` becomes a lower bound whenever
  `total_cost_partial` is present. Consumers aggregating spend across
  runs should surface the qualifier rather than summing silently.
- Adding the missing model to the pricing table makes both the marker
  and the qualifier disappear on the next run; neither is retroactive
  for an already-written `metrics.json`.
- Any future producer of `subtasks[]` records that performs its own
  pricing lookup inherits the invariant: mark the record, and let the
  collector name the model.
- With dollar accounting disabled the whole surface is absent, so a
  no-accounting run is unaffected.
