# ADR 0142 — Contract-grounded handoff advice

- **Status:** Accepted
- **Date:** 2026-07-20
- **Extends:** ADR 0124, ADR 0092, ADR 0093
- **Related:** ADR 0031, ADR 0087

## Context

The handoff advisor currently receives the handoff verdict, findings, last
output, a working-tree summary, and only the first non-empty line of the run
task. It does not receive the complete task or the accepted plan contract. A
plausible local repair can therefore contradict an acceptance criterion that is
not visible in the advisor prompt. Confidence, file scope, destructive-marker,
and waiver checks do not detect that class of error.

This is especially dangerous for unattended advice. A well-formed
`retry_feedback` recommendation at medium or high confidence can pass the
current safety classifier even when it proposes reverting an outcome the task
explicitly requires.

The opposite extreme is also unsafe: Orcho cannot reliably infer arbitrary
semantic contradictions between two free-text passages. A model-based prose
judge must not become an audit authority or produce a false deterministic
guarantee.

The evidence classifier has a related causality problem. A later successful
phase or terminal run can make advice appear resolved even when the immediate
retry did not establish that outcome. Advice effectiveness must be attributed
to the result directly following the advised decision.

## Decision

Handoff advice becomes contract-grounded. The advisor receives the complete
run task and the accepted typed plan contract, and every recommendation carries
a structured contract-effect declaration. A deterministic safety assessment
may authorise automatic use only when the structured declaration is complete
and contains no exact conflict.

This decision does not add a new phase-handoff action. The four canonical
actions and the existing decide/resume path remain unchanged.

### Contract snapshot

Core builds an immutable `AdviceContractSnapshot` from authoritative run state
when advice is requested. The snapshot contains:

- a digest of the complete raw task supplied to the run;
- the parsed plan goal;
- stable invariant identifiers for every plan acceptance criterion and subtask
  done criterion, retaining their exact text;
- plan and subtask owned files and allowed modifications;
- the current phase, handoff trigger, available actions, and retry round;
- the current verification identity and failure class when the handoff was
  raised by scheduled verification; and
- the correction boundary already present in the handoff artifacts, when one
  exists.

The complete raw task and a deterministic rendering of the accepted plan
contract are supplied to the advisor as separate turn-scoped prompt parts. The
snapshot is persisted with the advice artifact so later consumers can establish
which contract the recommendation saw. The raw task need not be duplicated in
the advice artifact; its digest plus the typed snapshot provide durable
identity, while the run metadata remains the source of the raw text.

If no parsed plan is available, the snapshot records that fact. Advice may
still be rendered for an operator, but it cannot be marked safe for automatic
application merely from prose.

### Structured recommendation intent

The code-owned advisor response contract gains two structured fields:

```json
{
  "proposed_operations": [
    {
      "kind": "repair | preserve | revert | remove | waive | stop",
      "targets": ["<path, gate identity, or contract subject>"]
    }
  ],
  "contract_effects": [
    {
      "invariant_id": "acceptance:1",
      "effect": "preserve | advance | violate | unknown",
      "reason": "<short explanation>"
    }
  ]
}
```

Every invariant in the snapshot must appear exactly once in
`contract_effects`. Unknown identifiers, duplicates, missing invariants, an
`unknown` effect, or an unparseable structured intent make the recommendation
operator-review-only. An explicit `violate` effect is a deterministic contract
conflict. Existing exact policy checks remain authoritative: unavailable
actions, waiver rules, destructive markers, file scope, retry budget, and
repeated findings are not delegated to the model.

The effect declaration is an auditable statement made by the advisor, not a
proof that two arbitrary prose passages are semantically equivalent. Orcho does
not claim to detect an undeclared semantic contradiction. Ambiguity fails
closed: it prevents automatic application and asks the operator to decide.

### Safety disposition

The safety classifier has one authoritative typed disposition:

- `safe` — the recommendation is structurally complete, declares no conflict,
  and passes the existing exact policy gates;
- `contract_conflict` — the structured recommendation explicitly violates a
  persisted invariant or an exact operation conflicts with an exact contract
  boundary;
- `operator_review_required` — intent or invariant coverage is missing,
  ambiguous, or unknown; and
- `policy_blocked` — an existing exact policy gate blocks the recommendation.

`auto_apply_ok` is derived from this disposition and is true only for `safe`
`retry_feedback` at non-low confidence. It is not independently assigned by a
second policy path. `blocked_reason` and conflict details explain the same
decision; they do not recompute it.

Interactive advice may display every disposition. `contract_conflict` and
`operator_review_required` cannot use the advisor's apply shortcut; the
operator may return to the canonical handoff menu and author an explicit
decision. The non-interactive `ci_agent` path stops with
`needs_operator` for both dispositions and records no decision.

### Prompt and module boundaries

The complete task, accepted plan contract, handoff facts, and findings are
dynamic prompt parts. The JSON response contract remains code-owned. No
machine-output schema is moved into an editable Markdown prompt.

`pipeline.project.handoff_advice` is already an oversized facade. Contract
snapshot construction, structured intent parsing, and conflict assessment must
live in focused modules. This change must not append those responsibilities to
the existing orchestration body.

### Durable artifact and public projection

The advice artifact grows additively with the contract snapshot, structured
intent, and safety disposition. Existing fields retain their meaning.

The core SDK advice result exposes the disposition and conflict details.
`orcho-mcp` must project those fields and must emit no `ready_next_action` when
the disposition is `contract_conflict` or `operator_review_required`. This is a
cross-repository wire update and requires matching core, MCP, schema, and
protocol tests in the same delivery wave.

### Immediate outcome attribution

An applied advice retry is classified from the nearest causally linked retry
result for the same phase and handoff lineage:

- approved or cleared at that result: `resolved`;
- the same blocking finding recurs at that result: `repeated`; and
- no directly linked result: `unknown`.

A later downstream approval, terminal run status, manual waiver, or unrelated
retry cannot retroactively classify the advice as resolved.

## Consequences

- The advisor sees the contract it is supposed to preserve instead of only a
  task title.
- Explicit structured contradictions are rejected deterministically and never
  auto-applied.
- Free-text ambiguity is represented honestly as operator review rather than a
  fabricated semantic guarantee.
- CI and MCP cannot forward a recommendation that core classified as unsafe.
- Advice-effectiveness metrics describe the immediate advised retry rather than
  eventual run success.
- The advice artifact and MCP result gain additive fields, requiring a
  coordinated core/MCP implementation.

## Alternatives considered

### Treat the full task prompt as sufficient safety

Rejected. More context reduces model error but does not make automatic
application auditable or deterministic.

### Add a second model as a universal contradiction judge

Rejected. It adds cost and another probabilistic opinion without producing a
machine-verifiable contract boundary.

### Reject any advice that mentions an acceptance criterion

Rejected. Repairs routinely need to change implementation details while
preserving or advancing the required outcome.

### Keep producing a ready MCP call and rely on the captain to inspect it

Rejected. A typed safety decision must be enforced at the projection boundary;
otherwise the easiest next action contradicts the core policy.
