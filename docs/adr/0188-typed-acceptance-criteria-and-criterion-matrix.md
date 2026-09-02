# ADR 0188 — Typed acceptance criteria and one criterion-evidence reducer

- **Status:** Accepted
- **Date:** 2026-09-02
- **Related:** [ADR 0065](0065-policy-derived-acceptance.md),
  [ADR 0068](0068-subtask-done-criteria-attestation.md),
  [ADR 0132](0132-scheduled-gate-selection-execution-and-disposition.md),
  [ADR 0151](0151-verification-ownership-and-cost-aware-agent-variants.md),
  [ADR 0181](0181-rework-provenance-stamped-at-birth.md)

## Context

The PLAN contract stored plan-level `acceptance_criteria` as `list[str]`.
Prose criteria cannot be referenced, so nothing in the engine can answer

    criterion -> responsible executor -> verification mechanism -> proof -> state

with machine facts. Subtask attestations (ADR 0068) are indexed only inside
their own task, official gate receipts are keyed by scheduled identity
(ADR 0132), and reviewer findings carry no criterion link. ADR 0181 recorded
that no machine contract links an attestation criterion to an official gate.
ADR 0065 pointed at criterion-owned proof but left the implication that
planner-authored commands are official proof — ADR 0151 has since made
verification engine-owned.

## Decision

### 1. Typed criteria with stable IDs

A plan-level acceptance criterion is a typed object, not a string:

```json
{"id": "C1", "intent": "...", "verify": "executable",
 "gate_refs": [{"command": "unit", "hook": "after_phase", "phase": "implement"}]}
```

* `id` matches `C[1-9][0-9]*` — enforced for every typed criterion, not only
  composer-generated ones — and is unique within the accepted contract. The
  legacy ingress normalizer generates ids in the same grammar, so there is no
  second accepted id shape anywhere.
* IDs are assigned and validated before implement starts. No reader derives an
  ID from array position for a new-format plan.
* Plan repair preserves IDs for retained criteria; removed IDs are not reused in
  the same contract lineage.
* After implement begins, criteria, IDs, verification class, and gate mappings
  are immutable for that run. A correction/replan creates an explicit revision
  and retains unchanged IDs.

Each plan task carries `acceptance_refs: list[str]` — references by ID only,
never copies of criterion text.

### 2. Three verification classes

* `executable` — one or more `gate_refs`, no `human_instructions`. A `gate_ref`
  is the **complete** scheduled identity `(command, hook, phase)`; a command
  name alone is invalid. All three keys are always present. `phase` is
  non-empty exactly for the phase-anchored hooks (`before_phase` /
  `after_phase`) and **empty** for every other hook (`before_delivery`,
  `on_resume`, `manual_only`) — that is how the scheduled-gate ledger keys
  those rows, so requiring a phase there would make a `before_delivery` gate
  unaddressable, and allowing one would invent an identity no ledger row can
  match. Every ref is resolved against the run's durable scheduled-gate ledger
  before implement starts. Policy, selection, execution,
  freshness, and consequence stay owned by the existing verification
  authorities (ADR 0132/0151) — this ADR only *references* them.

  Resolution is **fail-closed**. No ledger, or an unreadable one, rejects every
  executable criterion: a project that declares no verification contract has no
  official gates, so a criterion claiming gate proof there cannot be honoured.
  An undeclared identity is rejected, and so is one the run has already
  resolved as not selected (`selected is False`).

  The one deliberate exception is `selected is None` — the selection epoch for
  that hook has not resolved yet. It is admitted at plan time and *cannot* be
  forced earlier: path- and task-kind-based selection rules read the implement
  diff, which does not exist during planning, so resolving selection then would
  freeze it against an empty change set and change which gates the run picks.
  Such a ref stays fail-closed downstream instead — an identity the run never
  selects reduces to `not_selected`, and a non-`proven` executable row blocks
  readiness.
* `agent_assertion` — no gate refs. Yields advisory evidence only. It can never
  become `proven` and never satisfies a blocking criterion by proxy. Its typed
  evidence comes from the durable claim log and from reviewer output: both the
  review and the release output contracts carry an optional `criterion_id` on a
  finding / blocker, validated against the same id grammar. A link to an id the
  plan never declared contributes no proof rather than fabricating a row.
* `human` — non-empty `human_instructions`, no gate refs. Stays pending until a
  typed per-criterion human decision is durably recorded. Phase continuation or
  a general waiver never implicitly approves it.

Executor coverage is validated, not invented by a renderer: every `executable`
criterion must be referenced by at least one task before implement; an
unreferenced `agent_assertion` projects the literal executor `reviewer`; a
`human` criterion projects the literal executor `human`.

### 3. State algebra

Per-criterion state is derived only from typed machine facts — never from
transcript prose, Markdown, command output, or finding text.

* `executable` — `proven` only when every referenced official identity has a
  fresh passing canonical classification for the current subject **and** a
  canonical receipt id behind it. A passing classification with no receipt is
  not proof: that identity reduces to `missing`, so the row stays blocking
  rather than shipping green with an empty `proof_refs`. Otherwise the
  meaningful canonical fact is preserved as `failed`, `stale`, `missing`, or
  `not_selected`. Every non-`proven` executable row blocks.
* `agent_assertion` — `advisory` with a linked typed claim/finding, `pending`
  without one. Never blocks, never `proven`.
* `human` — `accepted`, `rejected`, or `pending` from the validated decision
  chain head. Only `accepted` satisfies; `rejected`/`pending` block.

**Executable precedence** (multi-gate rows, after consuming the canonical
per-identity classifications):

    failed > stale > missing > not_selected > proven

**Canonical state serialization order** (a *different* axis — it orders
`counts_by_state` keys and every state enumeration in docs, evidence JSON, the
SDK canonical dump, and Markdown):

    proven, failed, stale, missing, not_selected, advisory, accepted, rejected, pending

These two orders are deliberately distinct and are declared separately. The
serialization order lives as a single constant in the reducer module
(`CRITERION_STATE_ORDER`) and is re-exported through the public SDK; no other
module may re-declare it. `counts_by_state` contains only states present in the
rows, with positive counts, emitted strictly in that order.

### 4. One authoritative reducer

`pipeline/criterion_matrix.py` is the single owner. It is pure: it consumes
typed criteria, task references, typed criterion claims, typed findings,
canonical gate classifications, and typed human decisions, and returns exactly
one row per criterion in plan order plus a summary. It does not reimplement
receipt freshness or gate selection, and it does not parse prose.

Final readiness consumes the reducer's summary in one policy site. Evidence
JSON, Markdown, SDK, CLI, and MCP are read models over that same result.

`ready == (blocking_open == 0)`.

### The persisted matrix keeps its order

`counts_by_state` key order **is data**. The evidence writers therefore
serialize the `criterion_matrix` subtree with insertion order preserved while
every other bundle section stays key-sorted as before
(`pipeline.evidence.bundle.dumps_bundle`, used by both the engine writer and
`sdk.write_evidence_bundle`). A plain `sort_keys=True` dump would rewrite
`CRITERION_STATE_ORDER` alphabetically in `evidence.json` and break
byte-equivalence with the SDK's canonical JSON.

### The matrix is checked against the plan it describes

`validate_bundle` cross-checks `criterion_matrix.rows` against
`plan.acceptance_criteria`: exactly one row per criterion in plan order, unique
ids, matching `intent` and `verify`, and the one `method` projection that
criterion admits. Validating rows only against themselves would let a bundle
drop a blocking criterion and still report `ready`, and every downstream reader
(persisted evidence, SDK, MCP) would inherit that false readiness without
recomputing anything.

`plan.source` is the authority signal, not the criteria list. A projected plan
record (`json` / `markdown`) states the accepted contract, so an explicitly
empty `acceptance_criteria` is a real claim — *this plan declares no criteria*
— and the matrix must then be the explicit empty matrix; an undeclared row is a
phantom criterion for the SDK and MCP. Only `source == "absent"` means the
bundle carries no plan projection at all, and only that case skips the
cross-check.

### Proof kind and cardinality are fixed by `(verify, state)`

A global "kind is one of four" check is not enough. The schema pins the
pairing: a `proven` executable row cites one receipt per gate identity and only
receipts; an `advisory` row cites at least one claim/finding and nothing else;
an `accepted`/`rejected` human row cites exactly one `human_decision` — the
validated chain head; a `pending` row of any class cites nothing.

### The schema validates the matrix, not just its shape

`validate_bundle` checks the row/summary contract *and* their mutual
consistency: non-negative integer counters (never `bool`), `total == len(rows)`,
`counts_by_state` exactly tallying the rows, `blocking_open` matching the
blocking rows, `pending_human_ids` being the pending human criteria in plan
order, non-empty string executors and proof ids, and — per verification class —
the allowed states, the required `method.kind`, and the derived `blocking`
value. Gate identities inside a row are validated by the criterion schema
itself, so a row can never carry a shape the plan contract would reject.

### The criterion backstop is not the receipt backstop

`criterion_release_gaps` is a **separate** closing-gate authority from ADR
0090's `required_receipt_gaps`, and is gated on strictly less:

* it applies with **no declared verification contract** — a project without one
  still has plan criteria;
* it is **not** disarmed by an operator waiver. `continue_with_waiver` is a
  decision about continuing a phase, not the per-criterion human decision a
  `human` criterion requires; letting it satisfy one would be exactly the
  implicit approval this ADR forbids;
* it also guards the no-diff shortcut. "No diff to review" is not "nothing left
  to prove", so that path consults the matrix before it can auto-approve on
  implement evidence alone.

### Unreadable facts are a gap, never an absent matrix

Only a genuinely **absent** plan artifact yields "no criterion contract". A
plan, claim log, decision journal, or gate ledger that exists but does not load
produces one blocking integrity gap, and the evidence SDK reports
`EvidenceInvalid` rather than omitting the key. Degrading a corrupt artifact to
"no matrix" would make it indistinguishable from a legacy run and would erase a
blocking criterion from every readiness consumer.

### 5. Durable human decisions

The append-only per-criterion decision record has required keys `decision_id`,
`run_id`, `criterion_id`, `decision` (`accept|reject`), `recorded_at`, and
optional `note`, `actor`, `supersedes`.

* `recorded_at` is writer-assigned canonical RFC 3339 UTC text in
  `YYYY-MM-DDTHH:MM:SS[.ffffff]Z` form. The writer normalizes aware datetimes to
  UTC; naive datetimes and non-UTC input strings fail **before** write. On read
  the value is *verified* — digit layout plus a real calendar instant, so a
  hand-edited `2026-99-99T99:99:99Z` is rejected — and then handed on byte for
  byte. Readers (SDK, MCP) treat it as an opaque stable string and never
  reparse or reformat it.
* Optional fields are **absent** when unused — `null` is never written and is
  rejected by the schema for new records. When present each is a trimmed
  non-empty string; an empty-after-strip value fails before write.
* `decision_id` is stable, non-empty, unique within the run, and is the ID used
  by `proof_refs[{"kind": "human_decision"}]`.
* Unknown keys fail schema validation.
* The core SDK input does not accept caller-supplied `decision_id` or
  `recorded_at`; the durable writer assigns both.

**Admission is enforced once, at the durable writer**, so the SDK, the CLI, and
any direct writer share one gate. A decision is refused — leaving the artifact
byte-identical — when the `run_id` does not identify the target run directory,
when the run has no accepted plan artifact, when the criterion is not declared
by that plan, or when its class is not `human`.

**The reader replays the whole journal.** Per-record schema validation is not
enough, because the chain invariant is a property of the sequence. On load,
`decision_id` must be unique across the run, every record must carry this run's
id, the first record of a criterion must omit `supersedes`, and every later
record must name its criterion's current head exactly — which rules out a
dangling reference, a stale one, a branch, and a cross-criterion reference in
one check.

**Supersession is append-only with a single head.** The first decision for a
criterion omits `supersedes`. Every later decision must name the *current*
head's `decision_id`. A stale supersession (naming an already-superseded
record) and a branched supersession (a second decision naming the same previous
head) are both rejected before write; the artifact is byte-identical
afterwards. The reducer consumes only the validated head, and head selection is
deterministic across reload/resume.

### 6. Legacy ingress

Old durable plans containing `list[str]` are accepted through exactly one
normalizer (`normalize_legacy_criteria`). It assigns deterministic positional
legacy IDs once for that immutable artifact and classifies them as
`agent_assertion`. Everything downstream is typed. New writers never emit
`list[str]`.

### 7. Absent vs empty evidence

Evidence gains an additive `criterion_matrix` key.

* An old bundle with **no** `criterion_matrix` key stays valid: the SDK returns
  `None`, Markdown omits the section, MCP omits the key. `null` is never
  written.
* A new-format plan with no criteria writes the **explicit** empty matrix
  `{"rows": [], "summary": {"total": 0, "blocking_open": 0, "ready": true,
  "counts_by_state": {}, "pending_human_ids": []}}`.

## Consequences

* ADR 0065's planner-owned raw-command implication is superseded: a planner can
  no longer turn `commands_to_run`, transcript commands, or self-reports into
  official proof. Its criterion-owned-proof *intent* is kept and completed.
* ADR 0151's engine-owned official verification is preserved unchanged; the
  criterion layer composes its facts.
* ADR 0181's noted gap — no machine contract from attestation criterion to
  official gate — is closed by this ADR's `gate_refs`.
* ADR 0068 `done_criteria` remain the subtask-local developer attestation
  contract; they are not aliased into plan acceptance criteria.
* The public SDK/wire shape changes, so the paired MCP projection, schema
  snapshot, and mock smoke ship in the same delivery wave.
