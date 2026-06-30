# ADR 0025 — Release gate (project final_acceptance) + cross final acceptance

## Status

Accepted (Phase 1 implemented). Phase 3 (cross_final_acceptance) deferred.

## Context

`final_acceptance` runs through `review_json_contract` — the same machine
contract as `validate_plan` / `review_changes` / `compliance_check`. The
shape is `{verdict, short_summary, findings, risks, checks}` with the
P0/P1/P2/P3 severity enum. The reviewer answers a generic "is this
review-clean?" question and the orchestrator records a generic finding
list.

This conflates two distinct gates:

* `review_changes` — "is the code review-clean?"
* `final_acceptance` — "can this ship?"

The release-readiness question wants a different output:

* an explicit `ship_ready` flag (load-bearing, not implied from verdict);
* `release_blockers` (concrete, ship-blocking items);
* `verification_gaps` (explicit risks with missing evidence);
* `contract_status` — structured per-aspect breakdown
  (`task_contract`, `interfaces`, `persistence`, `tests`).

P3-class observations belong in `review_changes`; they aren't release
blockers by definition.

The same shape will compose into a future `cross_final_acceptance`
gate that asks "can the coordinated multi-repo change ship?" after
`contract_check` (Phase 3 of the same plan).

## Decision

Split `final_acceptance` away from `review_json_contract` onto a new
`release_json_contract`. Phase name stays `final_acceptance`; the
machine contract and the parser change.

### Wire format (`release_json`)

```json
{
  "verdict":        "APPROVED" | "REJECTED",
  "ship_ready":    true | false,
  "short_summary": "≤280 chars",
  "release_blockers": [
    {
      "id":                 "R1",
      "severity":           "P0" | "P1" | "P2",
      "title":              "short label",
      "body":               "concrete failure scenario",
      "required_fix":       "what must change before ship",
      "file":               "path/to/file.py",       // optional
      "line":               123,                      // optional
      "why_blocks_release": "release-specific framing"
    }
  ],
  "verification_gaps": [
    {
      "risk":             "what could go wrong",
      "missing_evidence": "test / check / proof not present",
      "required_check":   "what would close the gap"
    }
  ],
  "contract_status": {
    "task_contract": "satisfied" | "incomplete" | "unclear",
    "interfaces":    "compatible" | "broken"   | "not_applicable",
    "persistence":   "safe"       | "risky"    | "not_applicable",
    "tests":         "sufficient" | "weak"     | "missing"
  }
}
```

`release_blocker` is a structural superset of `review_changes` finding —
`id / severity / title / body / required_fix / file / line` line up
exactly so the evidence collector can project a blocker onto the review-
shape `findings` slot without bespoke field mapping.
`why_blocks_release` is the release-tier addition.

### Coherence invariants (strict)

* `APPROVED` iff `ship_ready=true` AND `release_blockers=[]` AND
  `verification_gaps=[]`. No grey zone: an unaddressed verification gap
  blocks ship.
* `REJECTED` iff `ship_ready=false` AND at least one
  `release_blockers` OR `verification_gaps` entry is present.
* When verdict is `APPROVED`, every `contract_status` value must be the
  positive enum (`satisfied` / `compatible` / `safe` / `sufficient`)
  or `not_applicable`.
* Severity is `P0|P1|P2` only — P3 dropped at the release tier.

### Contract threading

Routing is parameter-driven, not heuristic. A new kwarg
`output_contract: Literal["review", "release"] = "review"` threads
through every reviewer prompt builder that final_acceptance touches:

```
phases.run_review(output_contract=…)
  → prompts.review_focus(output_contract=…)
  → prompts.runtime_review_uncommitted_prompt(output_contract=…)
```

Each layer selects the corresponding `*_json_contract()` block. The
wrapper `runtime_review_uncommitted_prompt` strips the embedded
system-tail from focus (existing behaviour) and re-attaches its own;
under `output_contract="release"` the attached block is
`release_json_contract` — closing the double-contract bug class.

Sibling builders (`runtime_review_file_prompt`, `plan_review_focus`,
`hypothesis_review_focus`, the direct `qa_agent.invoke` paths in
`pipeline/cross_project/orchestrator.py::_validate_cross_plan` and
`pipeline/engine/hypothesis.py`, the cross runner's `contract_check`)
stay on `review_json`. Their migration would be a separate ADR.

### Dual-shape `phase_log["final_acceptance"]`

Backward compatibility for existing Web / MCP / evidence / acceptance-
fixture consumers is built in: the phase handler writes BOTH the
review-shape mirror and the release fields:

```python
state.phase_log["final_acceptance"] = {
    # Review-shape mirror — preserved for existing consumers:
    "approved":      parsed.approved,
    "verdict":       parsed.verdict,
    "short_summary": parsed.short_summary,
    "findings":      [b.to_finding_dict() for b in parsed.release_blockers],
    # Release-shape (new):
    "ship_ready":         parsed.ship_ready,
    "release_blockers":   parsed.blockers_as_dicts(),
    "verification_gaps":  parsed.gaps_as_dicts(),
    "contract_status":    parsed.contract_status.to_dict(),
    # …
}
```

`ReleaseBlocker.to_finding_dict()` projects each blocker onto the
review-finding shape (dropping `why_blocks_release`). The persisted
session entry, written by `FinalAcceptanceAdapter`, carries both
shapes identically.

The evidence bundle adds a top-level `release_summary` section
(`ship_ready / verification_gaps / contract_status`) without
disturbing the existing `findings` slice.

### Control flow

A well-formed REJECTED release verdict does NOT halt the mono pipeline
— matches the current ADR 0022 non-halting behaviour for review_json
rejected verdicts. Only a parse error
(`ReleaseParseError` / `ReleaseSchemaError`) hard-halts via
`state.stop`. The operator decides whether to relitigate via
`review_changes` / `repair_changes`. A future "REJECTED release →
run failed" mapping is a separate ADR.

### Dry-run

The dry-run branch of `phases.run_review` synthesises an approved
JSON envelope so the downstream parser can verify the contract on
the dry-run path. When `output_contract="release"` is set, the
dry-run payload is release-shaped (APPROVED + ship_ready=true + empty
blockers/gaps + all `contract_status` values set to the positive
enum); otherwise the existing review-shaped payload is returned.

### Mock + acceptance stubs

Every fake reviewer stub that emits `_approved_review_json(…)` checks
the prompt for the `release_json` block marker — detected by an
`<orcho:system-block …>` opening tag carrying both `kind="contract"`
and `name="release_json"` — and emits a sibling `_approved_release_json(…)`
helper when matched, falling through to the review payload otherwise.
A bare substring search for the token `release_json` would
false-positive on a review prompt that mentions the word.

## Consequences

* `final_acceptance` becomes a real release gate with structured
  ship-readiness signal.
* Existing review-shape consumers (Web phase card, MCP
  `orcho_run_evidence`, `sdk.evidence_slices.list_findings`,
  acceptance fixtures, golden snapshots) keep working because the
  phase_log entry mirrors review-shape fields alongside the release
  fields.
* Contract-ownership hygiene preserved: `release_json` token added to
  `_CONFIG_OWNED_POLICY_TOKENS`; user-editable
  `_prompts/{roles,tasks,formats}/*.md` cannot reference it; the
  `RELEASE_JSON` template body carries no orchestrator-topology terms.
* Phase 3 (`cross_final_acceptance` cross-only terminal gate) composes
  on top of this contract without modifying the local gate.

## Out of scope (this ADR)

* `cross_final_acceptance` cross-runner gate — Phase 3.
* New MCP typed records / Web UI panels for release fields — Phase 1.5.
* SDK `list_release_summary()` slice — Phase 1.5.
* Migrating `validate_plan` / `validate_hypothesis` / `contract_check`
  to `release_json` — separate ADR if/when needed.
* Auto-routing a REJECTED release back into a repair loop — separate
  ADR.
* Wire-format migration of stored evidence — clean break (no
  production install base).
