# ADR 0093 — Handoff advice evidence (Stage 2): additive `handoff_advice` evidence section, observe-only usage

- Status: Accepted
- Date: 2026-06-13
- Relates to: ADR 0090 (handoff advice Stage 0 — the interactive `advice` /
  `retry_with_advice` pseudo-actions, the durable advice artifact under
  `phase_handoff_advice/`, and the `note`-based provenance this stage reads),
  ADR 0092 (handoff advice CI Stage 1 — the `ci_agent` policy-controlled
  auto-retry, the third `feedback_source`, and the `_ci_agent_advice` in-memory
  aggregate this stage supersedes for the terminal summary), ADR 0076 / ADR 0082
  (prior strictly-additive top-level evidence keys — `verification_receipts` /
  `verification_readiness` — the precedent that the v1 bundle grows by *adding*
  keys, never by bumping `schema_version`), ADR 0025 (the `final_acceptance`
  singleton-dict phase shape the outcome classifier must normalize)

## Context

Stages 0 (ADR 0090) and 1 (ADR 0092) made an advisor recommend the smallest
honest way forward on a rejected/incomplete handoff and flow an accepted
recommendation through the canonical `retry_feedback` decide + resume path —
interactively (`agent_advice`) or under CI policy (`ci_agent`). They left durable
artifacts behind: one advice object per advisor invocation under
`phase_handoff_advice/`, and a decision artifact under
`phase_handoff_decisions/` whose free-text `note` carries
`feedback_source=<...>; advice_artifact=<relpath>`.

What was missing was a **read-only retrospective surface**: a durable, stable
answer to *did the advice actually help?* that a human, the evidence bundle, the
DONE/HALTED summary, and a future change-retrospect can all read without
re-walking raw artifacts or trusting only the in-memory `_ci_agent_advice`
aggregate (which is lost once the process exits, and only ever covered the CI
source). Stage 1's terminal block was built solely from that in-memory CI
aggregate; it could not describe an interactive `agent_advice` retry, and it
double-counted advisor token usage into the run totals (see *Observe-only usage*
below).

The hard constraint is the same wire-stability one ADR 0076 / ADR 0082 faced: the
v1 evidence bundle is a lower-bound contract — every required key is always
present, and the document grows by **adding** top-level keys, never by removing,
repurposing, or making a baseline key conditional. The MCP wire surface
(`orcho_run_evidence`) is a projection of that bundle, so any change to the
*required* shape would pull in an `orcho-mcp` update under the **MCP Validation**
rule.

## Decision

Add a single, strictly-additive, **optional** top-level evidence key,
`handoff_advice`, normalized once from the durable artifacts and projected into
three read-only surfaces. The normalizer is a pure leaf module
(`pipeline.project.handoff_advice_evidence.collect_handoff_advice(run_dir,
meta)`) that imports no other `pipeline.project` module at runtime and only
*reads* artifacts — it never decides, retries, spawns a process, or mutates run
state.

### The `handoff_advice` section (per-call + summary)

`handoff_advice` is `{"calls": [...], "summary": {...}}`:

- **`calls`** — one record per advice artifact (attempt-suffixed `_N.json`
  divergent advice are distinct calls): `handoff_id`, `phase`, `advice_artifact`
  (relpath — the full reviewer output is referenced, never copied),
  `feedback_source` (`agent_advice` | `ci_agent` | `null` when unapplied),
  `recommended_action`, `applied_action` (`null` when no matching decision),
  `confidence`, a compact `finding_fingerprint`, optional `severity_counts`,
  `resolved` (`bool | null`), `repeated` (`bool`), `outcome`, and — when the
  advice artifact carried it — observe-only `tokens_in` / `tokens_out` /
  `tokens_cached` / `duration_s` / `cost_usd_equivalent` / `model`.
- **`summary`** — `calls`, `applied_retries`, `resolved_retries`, `repeated`,
  `stopped`, `unknown`, plus an aggregated observe-only `usage`
  (`tokens_in` / `tokens_out`, and `cost_usd_equivalent` only when *every*
  usage-bearing call carried accounting).

A decision is matched to the advice it came from **strictly** by the
`advice_artifact=<relpath>` token in the decision `note`; `feedback_source` is
read from the `feedback_source=<src>` token. This reuses the Stage 0/1 provenance
shape verbatim — no new artifact field.

### Outcome classification rules (conservative)

The classifier reads `meta['phases']` (normalizing both the attempt-list and the
ADR 0025 `final_acceptance` singleton-dict shapes) and is deliberately
conservative:

- An advisor recommendation that is **not** an applied `retry_feedback`
  (`continue` / `halt` / `continue_with_waiver`), or an advice artifact with **no
  matching applied decision** (unapplied advice), classifies as `stopped`.
- An **applied `retry_feedback`** is classified by the next attempt of the same
  phase: `repeated` when the pre-retry finding fingerprint reappears in that
  attempt (**never** `resolved` while the same finding is still rejected),
  `resolved` when the finding cleared / the attempt was approved, and `unknown`
  when the run ended before the next verdict.

This covers both sources and answers *resolved vs repeated* without ever
optimistically claiming a fix.

### Narrow stop condition (BLOCKED only when Stage 0/1 surface is absent)

`collect_handoff_advice` returns `None` — and a consumer treats the section as
absent — **only** when the Stage 0/1 artifact surface is *entirely* missing: no
advice artifacts under `phase_handoff_advice/` **and** no decision note carrying
`advice_artifact=` / `feedback_source=` provenance (i.e. Stage 0/1 is not on
HEAD). The mere absence of a matching decision for a present advice artifact is
**not** a stop condition: that advice is still emitted as a call with
`applied_action=null` and outcome `stopped` (the mandatory *unapplied advice*
row). The collector attaches the bundle key only when there is ≥1 call, so a run
that never paused for advice shows no key — never a misleading empty section.

### No `EVIDENCE_SCHEMA_VERSION` bump

`handoff_advice` is added like ADR 0076 / ADR 0082 before it: a new optional
top-level key. It is **not** added to `REQUIRED_TOP_LEVEL_KEYS`, and
`EVIDENCE_SCHEMA_VERSION` stays `"1"`. `validate_bundle` light-checks only the
outer envelope when the key is present (`calls` is a list of objects, `summary`
is an object), leaving the per-call field set owned by the normalizer so it can
grow additively without a schema change. The markdown renderer prints an
`## Agent advice` section only when the key is present.

### Observe-only usage / cost (no double counting, no fake cost)

Advisor token usage is attributed **observe-only**, mirroring the
`metrics.json["subtasks"]` slot:

- The advisor runs *outside* the FSM phase loop. Recording its usage as a
  `handoff_advice` `MetricsCollector` *phase* (the Stage 1 behaviour) folded it
  into `total_tokens*` / `total_cost*` — a double count. That phase recording is
  removed.
- `metrics.json` instead carries an additive, observe-only `handoff_advice`
  usage slot, written via a new primitive-only `MetricsCollector.record_advice_usage`
  API and **never** summed into `total_*`. The run totals stay authoritative.
- The primary attribution lives in the evidence `handoff_advice.summary.usage`
  (this ADR's section); the `metrics.json` slot is the secondary observe-only
  projection.
- Cost is recorded **only** when provider accounting is available; with
  accounting off it is scrubbed. No cost is ever invented from heuristic token
  counts, and usage that the artifacts did not report is simply omitted (no
  zero-filled or `unknown`-labelled fabrication).

### Layer boundary: core never imports `pipeline.project`

`core.observability.metrics` must not depend on `pipeline.project`. The advice
usage is normalized in the **upper** layer (`pipeline.project.run._PipelineRun`
re-derives the aggregate from the durable artifacts via the leaf normalizer at
phase-end) and handed to `record_advice_usage` as **primitives only**. `core`
stays unaware of where the numbers came from; the dependency arrow points
upward, never down.

### Fit for a future change retrospect

The per-call records plus the classified summary are sufficient for a future
*change retrospect* to answer "which advice helped, which repeated, which was
never applied, and at what token cost" directly from the durable bundle — without
a redesign and without re-running the advisor. Building that consumer (and any
dashboard / Web surface for it) is deferred; this stage only guarantees the
durable, additive surface it would read.

## Schema / MCP impact

Additive and CLI/session-only, the same deciding factor as ADR 0076 / ADR 0082:

- `handoff_advice` is a **new optional top-level key**; `EVIDENCE_SCHEMA_VERSION`
  is unchanged and the key is absent from `REQUIRED_TOP_LEVEL_KEYS`. Existing
  consumers that ignore unknown keys are unaffected (see the regression
  `tests/unit/pipeline/evidence/test_evidence_bundle.py::test_existing_schema_validation_unchanged`).
- The evidence MCP projection is `sdk.evidence.collect_evidence`, which delegates
  to `pipeline.evidence.collect_evidence` + `validate_bundle`. Because adding a
  top-level key is explicitly permitted by the v1 contract, that projection
  **tolerates** the new key and still returns a valid bundle — proven by
  `tests/sdk/test_evidence.py` (a run dir carrying advice artifacts yields a
  bundle with `handoff_advice` that still validates).
- **`orcho-mcp` is a separate repository, outside this checkout.** An additive
  evidence key is **not** a wire-format change to a runtime schema, profile
  shape, mode flag, or gate primitive, so the **MCP Validation** rule does
  **not** trigger and an `orcho-mcp` edit is **out of scope** for this stage. The
  advice artifact / decision-note shapes are reused unchanged from Stage 0/1, so
  the strict decision reader and its wire contract are untouched.
- The `metrics.json["handoff_advice"]` usage slot is likewise additive and
  observe-only; it does not alter `total_*` and is gated by the existing
  accounting switch.

## Consequences

- The evidence bundle now durably answers *did the advice help?* — resolved vs
  repeated vs stopped vs unknown — for both the `agent_advice` and `ci_agent`
  sources, including the unapplied-advice case, surviving process exit.
- The DONE/HALTED `Agent advice:` summary block is unified on the same durable
  digest, so its counts agree with the evidence section and cover both sources;
  it renders only when advice evidence exists.
- Advisor usage is attributed without polluting the authoritative run totals, and
  no dollar figure is ever fabricated.
- The `core ↔ pipeline` boundary is preserved: the metrics layer gained only a
  primitive-only record API, fed from above.

## Non-goals (deferred)

- **Dashboards / UI / Web surface** for the advice section.
- **A full change-retrospect consumer.** This stage guarantees the durable,
  additive surface; the retrospective tool that reads it is future work.
- **`orcho-mcp` changes.** Out of scope — the additive evidence key is not a wire
  contract change (see *Schema / MCP impact*).
