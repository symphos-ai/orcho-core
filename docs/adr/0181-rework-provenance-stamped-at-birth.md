# ADR 0181 — Rework provenance is stamped at birth, classified by projection

- **Status:** Proposed (lands with the first stamping slice)
- **Date:** 2026-08-23
- **Related:** [ADR 0068](0068-subtask-done-criteria-attestation.md),
  [ADR 0073](0073-implement-phase-substance-repair-handoff.md),
  [ADR 0081](0081-verification-contract-scheduling-and-repair-routing.md),
  [ADR 0176](0176-operator-retry-feedback-reaches-the-agent.md)

## Context

Orcho already records that rework happened — repair-loop rounds, an
`implement_retry` re-run, a substance-repair pass, a review round — but not
*why this attempt exists* in any machine-readable, uniform way. The origin of
a correction attempt is scattered across carriers that were each designed for
their own control flow: `gate_repair` knows a require gate failed,
`implement_retry` knows which subtask ids were incomplete and what the
operator said, the substance-repair path knows a receipt was missing. None of
them stamp a common record, so no downstream consumer can answer the
questions that matter for run quality:

- How often does work pass on the first attempt, per started subtask, without
  survivorship bias?
- How often does an independent gate fail *after* the developer agent's
  attestation claimed every done-criterion was met (ADR 0068)?
- How much rework is agent-attributable versus operator- or
  reviewer-initiated?

Reconstructing origins after the fact — by correlating logs, receipts, and
event timestamps — is unreliable and invites exactly the kind of heuristic
text interpretation this evidence layer must never depend on.

Two measurement traps shape the design as much as the missing data does:

1. **Survivorship bias.** A first-pass metric computed over *accepted*
   subtasks silently improves when a subtask fails or the run halts. The
   denominator must be *started* subtasks, and the funnel
   (planned/started/terminal/accepted) must be published alongside any rate.
2. **False precision.** There is today no machine contract linking an
   attestation criterion to the gate that would verify it: attestations are
   indexed against a subtask's own criteria, while the gate ledger identifies
   gates by `(command, hook, phase)`. Any record claiming "this gate refuted
   criterion 3 of subtask T2" would be invented, not observed.

## Decision

### One record, stamped where the decision is made

Every **artifact/contract correction attempt** gets a typed
`ReworkProvenance` record, created by the code that decides to rework, at the
moment it decides. Infrastructure retries — transport retries, stale-session
fallbacks, parser reprompts, re-execution of a gate command as such — are
explicitly out of scope: they do not correct an artifact and never enter
first-pass metrics.

The stamping sites are the five places rework is born:

1. the require-gate `repair_loop` at `after_phase(implement)` (ADR 0081) —
   origin `gate_refutation`;
2. the `implement_retry` consume point — a **single** stamp carrying both
   `incomplete_result` and `operator_feedback` origins when the operator
   carrier holds both (ADR 0073 + ADR 0176; the carrier is one decision and
   is never split into two records);
3. the automatic substance-repair pass born in handoff policy — origin
   `substance_gap`;
4. the within-subtask attestation form-repair turn — origin
   `attestation_form_repair`;
5. profile review→repair rounds — origin `review_finding`.

A halt is not a stamping site: halting does not rework anything. A typed halt
participates only as evidence for `claim_relation=disclosed` when rework
follows it.

### Orthogonal axes, not one enum

A single mutually-exclusive class would force an arbitrary precedence
(operator feedback *about* a gate failure on an unattested subtask is one
event with three true facts) and lose data. The record carries independent
axes:

```
schema_version: 1
provenance_id:  str                # unique id, assigned at stamp time
origin:         tuple[str, ...]    # sorted, unique; multi-valued
initiator:      agent | engine | reviewer | operator
scope:          invocation | subtask | phase | run
subtask_ids:    tuple[str, ...]    # empty for phase/run scope
phase:          str
round_n:        int
gate_identity:  (command, hook, phase) | null
receipt_refs:   tuple[str, ...]
claim_relation: contradicted_scoped | disclosed | unattested
                | not_applicable | unknown
evidence_level: receipt | structured_claim | prose | none
```

Responsibility is split per axis: first-pass metrics read only the *fact* of
rework plus `scope`/`subtask_ids`/`initiator`; claim metrics read only
`claim_relation`; `evidence_level` qualifies it. `disclosed` counts only at
`evidence_level=receipt` — a self-reported blocker must point at a checkable
fact (a failed command, a missing file, a receipt), not prose. Disclosure by
prose records `claim_relation=unknown`, `evidence_level=prose`.

### Claims are phase-scoped because that is what the data proves

The provable fact today is: *after every subtask attestation in the phase
claimed all criteria met, a post-implement require gate failed*. That is
recorded as `claim_relation=contradicted_scoped` with `scope=phase`. No
individual subtask or criterion is ever marked contradicted. A
criterion-to-gate coverage contract (declaring which gate verifies which
criteria) would be a new protocol touching the verification contract and
task-file authoring; it is out of scope here and is only justified if the
phase-scoped signal proves insufficient on real run data.

### Canonical wire form, single authority, dedup by id

In memory the record may use set types; **on the wire only the canonical JSON
form exists**: `to_dict()` / `from_dict()` are the single serialization pair,
`origin` serializes as a sorted unique tuple. (The event writer's payload
cleaner passes values to `json.dumps` unchanged; a set would fail. The
canonical form is a contract, not an implementation detail.)

The event stream (`events.jsonl`, new validated kind `rework.provenance`) is
the **authoritative append-only source**. The copy embedded in the durable
rework carrier (e.g. inside the `implement_retry` payload or a repair-round
record) is a resume-local index, not a second source. Both carry the same
`provenance_id`, and every projection deduplicates by it, so event-plus-copy
and post-resume replay count each correction exactly once. A
checkpoint→resume contract test enforces this.

### Funnel-honest metrics, separated by scope

```
funnel: planned_count, started_count, terminal_count, accepted_count

subtask_strict_fpy =
  started subtasks whose FIRST terminal receipt == done
  and with no invocation/subtask-scoped rework
  / all started subtasks

implementation_batch_first_pass: bool
  # false on any phase/run-scoped correction rework

subtask_fpy_attribution_complete: bool
  # false when phase/run-scoped rework without subtask_ids exists

agent_attributable_fpy
  # secondary; excludes initiator=operator|reviewer;
  # never published without the strict value beside it
```

Engine-formal and externally-initiated rework *does* lower
`subtask_strict_fpy`: rework happened, the first pass did not hold,
regardless of who initiated it. A conservative variant that attributes
phase-scoped rework to all started subtasks may only be published under the
explicit name `subtask_fpy_lower_bound`.

The refutation-after-claim signal is published as
`gate_refuted_after_claim_count`. A *rate* requires its denominator from the
gate ledger (all eligible post-implement require-gate executions after
affirmative claims); provenance events alone contain only failures and would
yield a rate near 1. The rate ships only when the ledger-side denominator
does.

### Observe-only

Nothing here changes routing, sanctions, or gate policy. Whether a given
`claim_relation` should ever influence control flow is a separate future
decision, taken only after real run data exists. No text interpretation or
statistical classification is performed anywhere in this contract.

`schema_version=1` identifies the initial durable shape. It is not a
backward-compatibility promise: there is no legacy shape, no dual-read, no
migrator; before publication the shape changes in place, and an unsupported
version fails explicitly.

## Consequences

- Rework origin becomes a typed, deduplicable, resume-safe fact; first-pass
  and refutation metrics are computable without survivorship bias or invented
  precision.
- Five stamping sites acquire a small, uniform obligation; the classification
  logic lives in one pure projection module
  (`pipeline/rework_provenance.py`), so consumers (metrics, evidence, DONE
  summary, MCP readers, dashboards) read finished values and never
  re-classify.
- The metrics surface is additive keys in `metrics.json`; existing readers
  that pass the raw dict through need no change.
- Phase-scoped claim facts are deliberately weaker than criterion-level ones;
  anyone needing criterion attribution must first bring a gate-coverage
  contract through its own ADR.
- Every subsequent run — including the runs that implement the remaining
  slices — starts producing provenance data as soon as the first stamping
  site lands, so the dataset for any future policy decision accumulates from
  day one.
