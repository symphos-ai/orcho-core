# ADR 0130 — Typed verification failure and hygiene delivery policy

Status: Accepted

## Context

Command-receipt consumers shared `present`/`missing`/`failed`/`stale`, but not
a typed explanation of a failure. An exit-0 import/environment assertion failure
therefore looked like a nonzero test command and could be sent to an agent repair
loop. ADR 0125 and ADR 0108 correctly made failed environment provenance
visible as an existing `FAIL` gate status, but their terminal consequence was
strict: a provenance-failed `require` gate blocked delivery.

## Decision

### Classifier ownership and matrix

`pipeline.verification_failure` owns the pure `ReceiptClassification`, with
stable status, `failure_kind`, provenance, exit code, assertion counts, and
normalized failed assertions. `verification_readiness` re-exports the type for
callers. `command_receipt_passed` delegates to the classifier; consumers do not
repeat exit/assertion checks.

| Receipt condition | Status | `failure_kind` | Effective `require` consequence |
| --- | --- | --- | --- |
| no receipt | `missing` | `missing` | blocking |
| nonzero exit | `failed` | `test_failure` | blocking |
| no exit code or execution detail | `failed` | `env_failure` | hygiene warning |
| exit 0, failed `import_path_*` assertion | `failed` | `provenance_failure` | hygiene warning |
| exit 0, other failed assertion | `failed` | `env_failure` | hygiene warning |
| fingerprint, HEAD, or depended-on dependency drift | `stale` | `stale` | blocking |
| fresh exit 0 and passing assertions | `present` | none | no gap |

Execution/assertion outcomes precede staleness. Parent inheritance remains
strict: a fresh same-diff failure is never masked by a parent pass. The compact
formatter uses receipt evidence only; prose output never determines a class.

### Routing, waiver, and delivery

`gate_repair` classifies once and threads the result into routing, critique, and
handoff construction. `test_failure` keeps repair-loop and `retry_feedback`.
`provenance_failure` and `env_failure` skip `repair_changes` and publish only
`continue_with_waiver` and `halt`. Their handoff contains one P3 finding in
existing `artifacts.findings`, an existing `artifacts.short_summary`, and
receipt-derived `last_output`. A waiver is always an explicit operator decision:
the advisor may recommend it, but neither advisor nor CI writes a waiver or
decision automatically.

`verification_policy.outcome_aware_policy_by_command` overlays declared policy
without changing `derive_effective_policy`, cost metadata, or selection plans.
`manual_only` stays manual; only `provenance_failure`/`env_failure` become
`warn` at readiness and delivery; `test_failure`, `missing`, and `stale` retain
declared effective policy. Readiness, release-gap construction, and delivery all
apply this overlay. Compatible durable waiver collection is unchanged.

### Wire boundary and MCP stop condition

Core adds no top-level `meta.phase_handoff` field and no SDK/MCP wire field. It
uses existing `artifacts.findings`, `artifacts.short_summary`, and `last_output`;
`python tools/dump_sdk_schema.py --check` must remain clean.

The current `orcho-mcp` adapter reads top-level findings and has its own
default-action preference. It neither forwards `artifacts.findings` as
`findings_summary` nor guarantees `default_action=continue_with_waiver`. A
requirement for those exact MCP values is an explicit cross-repository handoff
to `orcho-mcp`, with a wire decision and mock smoke; it is not silently accepted
or implemented in core-only scope.

## Consequences

- Hygiene evidence stays visible but is not an agent code-repair task or a hard
  readiness/delivery blocker.
- Existing `FAIL` status vocabulary remains unchanged; `failure_kind` supplies
  the extra meaning.
- This ADR partially supersedes the terminal-consequence portions of ADR 0125
  and ADR 0108: their provenance detection remains valid, but typed
  provenance/environment failures warn at readiness and delivery. Historical
  ADRs remain append-only and unedited.
