# ADR 0068 — Subtask done-criteria self-attestation

- Status: Accepted
- Date: 2026-06-03
- Relates to: ADR 0067 (session-aware subtask_dag implementation), ADR 0026
  (session-aware prompt parts / code-owned contracts), ADR 0065
  (policy-derived acceptance), ADR 0066 (repair receipt re-review protocol)
- Builds on: ADR 0067's `ImplementationReceipt` and `delivery_clean` gate.

## Context

`SubTask.done_criteria` is the architect's per-subtask definition of done. After
ADR 0067 the subtask prompt surfaces those criteria to the developer, but the
runner had no way to know whether the developer actually addressed them. A
subtask receipt's `state="done"` meant only "the invocation returned without an
exception" — the criteria were *surfaced but unverified*. A real multi-subtask
run confirmed this: a subtask whose criteria were partially ignored still produced a
clean `done` receipt and flowed straight into the downstream quality gates.

Command-shaped criteria ("tests pass", "lint clean") are already re-checked
downstream by `review_changes` / `final_acceptance` and the gate commands.
The gap is two-fold:

1. **No early, per-subtask signal.** A 20-subtask DAG only learns a criterion
   was missed at the end-of-run review, after every downstream subtask has built
   on the incomplete one.
2. **Non-command criteria are unverifiable downstream.** "Document the new flag
   in the README", "add a migration note" — nothing re-checks these; they are
   simply trusted.

We want Orcho to gate on an explicit, complete *claim* from the developer
without becoming a truth oracle. The distinction we commit to:

> The attestation gate checks: did the implementer explicitly claim every
> criterion? The quality gates check: is that claim believable?

This is a **shape/completeness gate, not an LLM judge** and not a replacement
for the quality gates.

## Decision

Turn `done_criteria` into a delivery contract via a typed developer
self-attestation that Orcho parses and gates on deterministically.

1. **Prompt contract.** When a subtask declares `done_criteria`, the developer
   prompt carries a code-owned `subtask_attestation` contract
   (`pipeline.prompts.contracts.subtask_attestation_contract`, body in
   `contract_templates.SUBTASK_ATTESTATION`). It instructs the developer to keep
   its normal human-readable output and append exactly one machine-readable
   JSON object reporting, per criterion (by 1-based index), `met` + a
   one-sentence `evidence` claim, plus a `<=280`-char `summary`. Boundary
   discipline: the JSON shape lives in code-owned contracts, never in a
   user-editable prompt part. A criteria-less subtask gets no contract and no
   gate — symmetrically.

2. **Schema + parser (two concerns kept separate).**
   - `core.contracts.subtask_attestation_schema.validate_subtask_attestation_dict`
     validates SHAPE only (type tag, one entry per criterion with a positive int
     `index` that rejects `bool`, non-empty `criterion`/`evidence`, boolean
     `met`, non-empty `summary` truncated to 280).
   - `pipeline.subtask_attestation_parser.parse_subtask_attestation` recovers the
     one object from surrounding build prose via the shared JSON-contract
     recovery path; `validate_subtask_attestation` decides whether it *matches
     the current subtask*: `subtask_id` must match and the attested indexes must
     equal `{1..N}` (covers missing / extra / duplicate / wrong index), and every
     entry must be `met=true`.
   - **The index is the binding key, not the criterion text.** A developer may
     reword or translate a criterion while still addressing the right one by
     index; text drift is tolerated (it would otherwise produce false
     incompletes). Truth of the `evidence` is explicitly *not* judged here.

3. **A new terminal state: `incomplete`.** `ImplementationReceipt.state` gains
   `incomplete` alongside `done | failed | skipped`. A subtask whose invocation
   succeeded but whose attestation is missing / malformed / mismatched /
   not-all-met is `incomplete`, carrying `attestation_error` (the gate reason)
   plus the structured `criteria_report` and `attestation_summary`. An
   `incomplete` subtask blocks delivery and, under `stop_on_failure`, skips
   downstream — the same scheduling treatment as a hard failure, but a distinct,
   honestly-labelled state.

4. **Delivery gate.** The existing `delivery_clean = not missing_ids and not
   not_done` check already treats any non-`done` receipt as blocking, so
   `incomplete` is gated generically. The implement entry additionally surfaces
   `attestation_incomplete` (`subtask_id -> reason`) and the run-halt message
   names each blocked subtask with its attestation reason.

5. **Honest propagation.** Upstream receipts (`## Upstream Completed`) report a
   dependency's real state (`done` / `incomplete` / `failed`) and surface its
   attestation `summary` as a structured hint, so a downstream subtask knows its
   input may be partial. The live `ORCHO subtask DONE` marker and the
   `subtask.end` / `subtask.receipt` events carry the attestation outcome, and
   the durable evidence bundle records `criteria_report` /
   `attestation_summary` / `attestation_error`.

## Consequences

- A subtask is now "done" only when the developer explicitly and completely
  closed its declared criteria. Partial work is caught at the subtask boundary,
  before downstream subtasks build on it — not at end-of-run review.
- Non-command criteria gain a first enforcement point (explicit claim) even
  though their truth still rests with reviewers/humans.
- Mocks, fixtures, and dry-run paths that produce subtask output for a
  criteria-bearing subtask must append a valid attestation, exactly as they
  already emit the reviewer JSON contract. `MockAgentProvider` does this from
  the executable-subtask block in the prompt.
- The gate is deterministic and provider-neutral: no model call, no scoring, no
  heuristic "looks done" approval. It cannot pass a subtask the developer did
  not explicitly attest, and it cannot fail one merely for rewording a
  criterion.

## Out of scope (deferred)

- `owned_files` / write-policy enforcement (a criterion may claim a file was
  touched; nothing here checks the diff).
- Per-criterion truth verification — that remains the quality gates' job.
- Durable per-subtask prompt-render fanout (still aggregated; a planned
  follow-up increment).
- Any LLM-judged or fuzzy-matched criterion satisfaction.
