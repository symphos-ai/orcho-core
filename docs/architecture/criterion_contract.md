# Criterion contract and the criterion matrix

> Reference for ADR 0188. Read `verification_contract.md` first — this layer
> *composes* verification facts, it never re-derives them.

## The chain

```
task -> typed criteria -> verification mapping -> subtasks -> receipts -> readiness
```

The accepted plan is the immutable traceability contract for a run. Every
criterion has a stable ID and exactly one verification class; every relevant
subtask, claim, human decision, and selected gate refers to that ID; one pure
reducer computes the matrix; evidence, Markdown, SDK, CLI, and MCP project the
same result without re-deriving it.

## 1. Typed criteria in the plan

`acceptance_criteria` is a list of typed objects, never `list[str]`:

```json
"acceptance_criteria": [
  {"id": "C1", "intent": "The changed behavior is regression-tested",
   "verify": "executable",
   "gate_refs": [{"command": "unit", "hook": "after_phase", "phase": "implement"}]},
  {"id": "C2", "intent": "The explanation reads without internal context",
   "verify": "agent_assertion"},
  {"id": "C3", "intent": "The journey is acceptable to an operator",
   "verify": "human",
   "human_instructions": "Exercise the journey and record accept or reject."}
]
```

Each plan task carries `acceptance_refs: ["C1"]` — references only.

Rules enforced by `core.contracts.plan_schema.validate_plan_dict` *before*
implement starts, so a violation routes to plan repair:

| Rule | Failure |
| --- | --- |
| IDs unique, non-empty, `C[1-9][0-9]*` when composer-generated | schema error |
| `executable` needs ≥1 `gate_refs`, forbids `human_instructions` | schema error |
| `human` needs non-empty `human_instructions`, forbids `gate_refs` | schema error |
| `agent_assertion` forbids both | schema error |
| A `gate_ref` is the complete `(command, hook, phase)` identity | schema error |
| Task refs resolve to a declared criterion | schema error |
| Every `executable` criterion is referenced by ≥1 task | schema error |
| Every executable ref resolves against the durable gate ledger | `CriterionGateRefError` (`pipeline.criterion_gate_refs`) |

Gate-ref resolution is fail-closed: an absent or unreadable ledger rejects
every executable criterion, as does an undeclared identity or one the run has
resolved as `selected is False`. `selected is None` (the selection epoch has
not run yet) is admitted at plan time — it cannot be resolved before the
implement diff exists — and stays fail-closed downstream, where an identity the
run never selects reduces to `not_selected` and blocks.

Legacy `list[str]` plans are accepted through exactly one normalizer,
`core.contracts.criteria.normalize_legacy_criteria`, which assigns positional
IDs once for that immutable artifact and classifies every entry as
`agent_assertion`. New writers never emit `list[str]`.

## 2. The reducer

`pipeline.criterion_matrix.build_criterion_matrix` is pure. It emits exactly
one row per criterion in plan order:

```json
{
  "criterion_id": "C1", "intent": "...", "verify": "executable",
  "executors": ["task-2"],
  "method": {"kind": "gates", "gate_refs": [{"command": "unit", "hook": "after_phase", "phase": "implement"}]},
  "proof_refs": [{"kind": "receipt", "id": "receipt-17"}],
  "state": "proven", "reason": "", "blocking": false
}
```

A `gate_ref` always carries all three keys. `phase` is non-empty only for the
phase-anchored hooks `before_phase` / `after_phase`; `before_delivery`,
`on_resume`, and `manual_only` carry `"phase": ""`, matching how the ledger
keys them.

`method` is discriminated: `{"kind": "gates", "gate_refs": [...]}`,
`{"kind": "inspection"}`, or `{"kind": "manual", "instructions": "..."}`.
`gate_refs` is **absent** (not `[]`) outside `gates`; `instructions` is absent
outside `manual`.

`proof_refs[].kind` is one of `receipt | finding | claim | human_decision`.

### State algebra

* `executable` — `proven` only when every referenced identity has a fresh
  passing canonical classification **and** a canonical receipt id; a pass with
  no receipt reduces that identity to `missing`. Otherwise `failed`, `stale`,
  `missing`, or `not_selected`. Always blocks unless `proven`.
* `agent_assertion` — `advisory` with a linked typed claim/finding, `pending`
  without. Never blocks, never `proven`.
* `human` — `accepted` / `rejected` / `pending` from the validated decision
  chain head. Only `accepted` satisfies.

**Executable precedence** (multi-gate rows):
`failed > stale > missing > not_selected > proven`.

**Canonical serialization order** — a different axis, used for
`counts_by_state` keys and every state enumeration everywhere:
`proven, failed, stale, missing, not_selected, advisory, accepted, rejected, pending`.
It lives once, as `pipeline.criterion_matrix.CRITERION_STATE_ORDER`, and is
re-exported from the public SDK.

### Summary

```json
{"total": 3, "blocking_open": 1, "ready": false,
 "counts_by_state": {"proven": 1, "advisory": 1, "pending": 1},
 "pending_human_ids": ["C3"]}
```

`ready == (blocking_open == 0)`. `counts_by_state` carries only present states
with positive counts, in the canonical order.

## 3. Durable inputs

`pipeline.criterion_evidence` is the only adapter that reads a run directory to
feed the reducer:

| Fact | Source |
| --- | --- |
| criteria, task ownership | `parsed_plan.json` |
| per-identity gate state | `scheduled_gate_ledger.json` dispositions |
| typed claims | `criterion_claims.json` |
| criterion-linked findings | review / release output `criterion_id` (an advertised optional key in both schema docs), mirrored into the claim log by the phase writers |
| human decisions | `criterion_decisions.json` |

Because every input is durable, a resumed run rebuilds the identical matrix.

## 4. Human decisions

`criterion_decisions.json` is append-only, one chain per criterion:

```json
{"decision_id": "hd-C3-1", "run_id": "…", "criterion_id": "C3",
 "decision": "accept", "recorded_at": "2026-01-01T00:00:00Z",
 "note": "…", "actor": "…", "supersedes": "hd-C3-0"}
```

Admission is enforced once, at the durable writer: the `run_id` must identify
the target run directory, the criterion must be declared by that run's accepted
plan, and its class must be `human`. Unknown criterion, non-human criterion,
and wrong run all leave the artifact byte-identical.

On read the whole journal is replayed — unique `decision_id` across the run,
one consistent `run_id`, no `supersedes` on a criterion's first record, and an
exact current-head reference on every later one.

* `recorded_at` is writer-assigned canonical RFC 3339 UTC
  (`YYYY-MM-DDTHH:MM:SS[.ffffff]Z`). Aware datetimes are normalized to UTC;
  naive datetimes fail before write. Readers never reparse it.
* `note` / `actor` / `supersedes` are **absent** when unused — `null` is never
  written and is rejected on read. When present, each is trimmed and non-empty.
* The first decision omits `supersedes`; every later one must name the current
  head. Stale and branched supersessions are rejected before write, leaving the
  artifact byte-identical.
* `decision_id` is what a `human_decision` proof ref cites.

Write access: `sdk.record_criterion_decision` and
`orcho criterion decide --criterion C3 --decision accept`. Neither accepts a
caller-supplied `decision_id` or `recorded_at`.

## 5. Projections

| Surface | Shape |
| --- | --- |
| evidence JSON | additive `criterion_matrix` key |
| evidence Markdown | `## Criterion matrix` table |
| final readiness | `ReadinessSummary.criterion_summary` + `criterion_gaps` |
| release gaps | `criterion_release_gaps` — one entry per blocking row |
| final_acceptance backstop | `review_support._criterion_backstop` |
| SDK | `sdk.get_criterion_matrix()` → `dict \| None` |
| CLI | `orcho criterion matrix / decide / decisions` |

### The criterion backstop is its own authority

`criterion_release_gaps` is separate from ADR 0090's `required_receipt_gaps`:
it fires without a declared verification contract, it is not disarmed by an
operator waiver, and it also guards the no-diff shortcut in
`final_acceptance`. A general "continue with waiver" never satisfies a `human`
criterion.

### Unreadable is not absent

Only a missing plan artifact means "no criterion contract". A plan, claim log,
decision journal, or ledger that exists but does not load produces one blocking
integrity gap (`CRITERION_INTEGRITY_RISK`), and `sdk.collect_evidence` raises
`EvidenceInvalid` instead of quietly omitting the matrix key.

### The matrix must match the plan

`validate_bundle` cross-checks the rows against `plan.acceptance_criteria` —
one row per criterion, in plan order, with matching `intent`/`verify` and the
method projection that criterion admits. `plan.source` decides whether there is
anything to check: a projected record (`json` / `markdown`) is authoritative
even when it declares no criteria, so an explicitly empty plan demands
`rows == []`; only `source == "absent"` skips the check. It also pins proof
kind and cardinality
per `(verify, state)`: receipts for a proven executable (one per gate
identity), claims/findings for advisory, exactly one head `human_decision` for
accepted/rejected, nothing for pending.

### Order survives the write

`counts_by_state` key order is data, so `pipeline.evidence.bundle.dumps_bundle`
— used by the engine writer and by `sdk.write_evidence_bundle` — leaves the
`criterion_matrix` subtree in insertion order while key-sorting everything
else. The written `evidence.json` matrix is byte-equivalent to
`sdk.canonical_criterion_json` of the same matrix.

### Absent vs empty

An old bundle with **no** `criterion_matrix` key stays valid: the SDK returns
`None`, Markdown omits the section, MCP omits the key. A new-format plan with
no criteria writes the explicit empty matrix
`{"rows": [], "summary": {"total": 0, "blocking_open": 0, "ready": true,
"counts_by_state": {}, "pending_human_ids": []}}`. `null` is never written.

### Conformance examples

`sdk.criterion_matrix_example(name)` ships versioned examples from the
installed package (`CRITERION_EXAMPLES_VERSION`) for `three_class`,
`multi_gate`, `explicit_empty`, `absent_matrix`, and `mixed_state`, plus
`sdk.human_decision_chain_example()`. Downstream consumers compare canonical
JSON against these instead of reaching into `orcho-core/tests/`.

<!-- TODO(orcho-phase-2): expand the authoring guide once the composer UI lands. -->
