# ADR 0141 — Subject-aware refresh of failed verification receipts

- **Status:** Accepted
- **Date:** 2026-07-20
- **Clarifies:** ADR 0094 (Stage 9 auto-run required receipts)
- **Related:** ADR 0089, ADR 0090, ADR 0130, ADR 0132, ADR 0140

## Context

ADR 0094 correctly keeps a failed command receipt authoritative for the subject
it recorded: a command failure must not be silently turned green merely because
the pre-final materializer runs.  With typed verification subjects, however, a
failed receipt can be known to describe older content in the same current run.
Leaving that receipt as the only result forces a command failure for subject A
to stand in for a later checkout subject B.

The distinction must be narrow.  Changed path names, legacy Git fields,
transcript output, an inherited receipt, or an absent/unavailable identity do
not prove that a failed execution covered different content.

## Decision

Stage 9 may refresh a failed receipt exactly once in its existing command pass
only when all of the following hold:

1. the official command receipt physically belongs to the current run;
2. its execution-first classification is `failed`; and
3. comparison of the receipt's usable recorded typed subject and the usable
   current checkout subject returns `STALE`.

The refresh writes the normal official command receipt for the current subject.
If that execution fails, the replacement remains `failed`.  `classify_receipt`
continues to report execution failure as `failed` regardless of subject drift;
the typed-subject comparison is an execution-eligibility decision, not a new
receipt status or classifier.

This policy is fail-closed.  A same subject, legacy/historical receipt without
a typed subject, malformed subject, unavailable recorded subject, unavailable
current subject, absent current-run receipt, or parent/inherited receipt is not
eligible for automatic refresh.

The existing ownership boundary remains unchanged: only selected engine-owned
delivery identities enter the shared target pass.  `manual`, `suggest`, and
other operator-owned identities are never executed by this exception.

Stage 9 still runs at most one `verify_env` pass per needed environment and one
`verify_run` command pass per materializer call.  It adds no retry loop, waiver,
transcript inference, or execution inside `final_acceptance`.

## Consequences

- A failed receipt remains authoritative for the content it actually tested.
- Proven content drift can replace obsolete failed evidence with a new official
  result for the current run's checkout.
- Parent continuity remains read-only; it is not an execution authority.
- The command-receipt schema, `ReceiptAutoRunResult.to_evidence()` keys,
  persisted extras, SDK payloads, and MCP payloads do not change.

## Alternatives considered

### Never refresh failed receipts

Rejected because it retains a known-old failure as the result for new content.

### Treat path sets, legacy provenance, or transcripts as drift proof

Rejected because none identifies the complete tested content.

### Refresh every failed receipt

Rejected because it could hide a same-subject command failure and violate the
execution-first, never-falsely-green invariant.

### Execute operator-owned commands automatically

Rejected because it bypasses the declared engine/operator execution boundary.
