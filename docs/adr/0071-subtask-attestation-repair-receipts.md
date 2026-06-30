# ADR 0071 - Subtask attestation repair receipts

- Status: Accepted
- Date: 2026-06-04
- Relates to: ADR 0067 (session-aware subtask DAG implementation), ADR 0068
  (subtask done-criteria self-attestation), MCP receipt inspection surface
- Extends: ADR 0068. ADRs are append-only; this records the follow-on repair
  protocol rather than editing the original attestation decision.

## Context

ADR 0068 made a criteria-bearing subtask close with a typed
`subtask_attestation` object. That kept the subtask-DAG gate honest: a subtask
is `done` only when the invocation returns and the declared done criteria are
all explicitly self-attested.

Real runtimes can satisfy the criteria but return a malformed machine-readable
tail, for example by wrapping the object in a drifted envelope. Treating that as
a hard execution failure is too harsh, because the implementation turn itself
may have completed. Treating it as `done` is too loose, because downstream
subtasks and review gates need the typed receipt. The repair path therefore has
to fix only the protocol shape, not the substance of the claim.

The first implementation also exposed two protocol gaps:

1. The repair marker was added to the core receipt, but the evidence bundle,
   SDK slice, and MCP typed receipt did not all carry the field.
2. The repair call invoked the runtime directly, bypassing the canonical
   session-aware invoke boundary that owns continuation policy, prompt trace,
   checkpoint session sync, and runtime write-intent flags.

## Decision

Add a one-shot malformed-attestation repair protocol with an explicit receipt
marker.

1. **Repair trigger.** The repair path runs only when the original attestation
   is unparseable or shape-invalid. It does not run when a parsed attestation
   says `met=false`, references the wrong criteria, or otherwise fails
   substance validation. Those failures remain `incomplete`.

2. **No artifact mutation.** The repair prompt is a follow-up turn whose only
   job is to return a flat JSON `subtask_attestation` object for the previous
   response. It is invoked through the same subtask invoke seam as the original
   implementation turn, with `mutates_artifacts=False`.

3. **Canonical session machinery.** Production repair turns go through
   `_session_aware_invoke` via the injected `SubtaskInvoke` strategy. That keeps
   continuation policy, prompt rendering, checkpoint session sync, and runtime
   accounting on the same path as ordinary subtask turns. The focused
   attestation helper builds/parses prompt turns only; it does not call
   `agent.invoke`.

4. **Receipt marker.** When repair succeeds, the terminal `subtask.receipt`
   carries `attestation_repaired=true`. The same field is projected through the
   evidence bundle's `implementation_receipts`, `sdk.SubtaskReceipt`, and the
   MCP `SubtaskReceiptRecord` schema. Omitted or `false` means no successful
   repair happened.

5. **Incomplete, not failed.** If repair fails, the subtask remains
   `incomplete` with the original parse reason plus repair failure diagnostic.
   The integrated implement output keeps the human output and the raw malformed
   tail so operators can inspect what happened.

## Consequences

- A malformed attestation can be recovered once without letting the agent do
  new implementation work.
- Downstream consumers can distinguish a clean attestation from a repaired one
  without reading raw logs.
- `orcho_run_status`, `orcho_run_evidence(slice="receipts")`, and evidence
  bundles expose the same receipt shape.
- Repair turns remain visible to the runtime invocation machinery instead of
  becoming an unaccounted direct provider call.

## Out of scope

- Repairing substance failures (`met=false`, wrong index, wrong subtask id).
- Multiple repair attempts or an unbounded correction loop inside
  `subtask_dag`.
- Changing how final review or final acceptance verifies whether the
  self-attested evidence is true.
