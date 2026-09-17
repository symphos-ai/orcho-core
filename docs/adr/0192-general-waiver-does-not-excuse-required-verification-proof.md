# ADR 0192 — A general waiver does not excuse required verification proof

- **Status:** Accepted
- **Date:** 2026-09-15
- **Related:** [ADR 0072](0072-continue-with-waiver-handoff-action.md),
  [ADR 0073](0073-implement-phase-substance-repair-handoff.md),
  [ADR 0089](0089-delivery-receipt-continuity.md),
  [ADR 0090](0090-require-gate-no-silent-green.md),
  [ADR 0136](0136-explicit-waiver-for-incomplete-implement.md),
  [ADR 0188](0188-typed-acceptance-criteria-and-criterion-matrix.md),
  [ADR 0191](0191-delivery-ledger-and-unresolved-action-guard.md)

## Context

ADR 0090 §4 installed the closing-gate backstop: after the release verdict is
parsed, `final_acceptance` merges one engine-computed gap per required delivery
command whose receipt classifies missing / failed / stale, and forces
`approved=False / verdict=REJECTED / ship_ready=False`. The same section
declared the backstop inert "when an operator waiver (`continue_with_waiver`)
is active", and the ADR's closing paragraph restated it: "a recorded waiver
disarms the backstop".

That exemption was implemented as a text test. The handler guard returned an
empty gap list as soon as the durable `phase_handoff_waiver` record carried any
non-empty `waiver_text`, without ever looking at *what* was waived. A single
key holds that record, and every waiver kind writes to it:

- a review waiver over `review_changes` findings,
- a plan waiver,
- the implement-incompleteness waiver (ADR 0136),
- the implement auto-waiver taken by the engine itself when the repair budget
  is exhausted (ADR 0073), and
- the verification-gate waiver the gate repair loop records with
  `handoff_id = gate:<command>:<round>`.

So an operator who accepted a reviewer's critique — or an engine that
auto-continued a stuck implement loop — silenced the gap for **every** required
receipt in the run, including commands that had never been executed at all. The
run reported a green acceptance for verification that did not happen; the only
authority designed to catch exactly that had been disarmed by a decision about
something else.

Two neighbouring authorities had already rejected that conflation:

- The Stage-6 delivery guard reads the durable record through
  `verification_waiver.collect_gate_waivers`, which resolves a waiver to an
  **exact gate command** from structure alone (an explicit `gate_command` field
  or the `gate:<command>:<round>` handoff id) and drops every record that names
  no gate. It excuses only a `failed` / `missing` receipt for that one command.
- The ADR 0188 criterion backstop honours no waiver at all.

Three authorities over the same run therefore disagreed: the same durable
record could make final acceptance green while delivery stayed red, and a
reviewer waiver could buy a green release that a per-criterion decision could
not.

## Decision

A waiver is a decision about findings and about continuing a phase. It is not
evidence that a command ran. The two are separate claims and only the second
can close a verification gap.

1. **Exact-command exemption only.** The receipt backstop excuses a required
   command's gap only when the durable waiver record resolves to *that exact
   command*, through the same reader the delivery guard uses. Identity is taken
   from the record's structure — the `gate_command` field, or a `handoff_id` of
   the form `gate:<command>:<round>`. Waiver prose, waived findings, and the
   prior critique are never parsed.
2. **Only accepted failures.** The exemption applies to a `failed` or `missing`
   receipt: the operator accepted a known red gate, or accepted that it will
   not be run. A `stale` (or unverifiable) receipt is never excused — a waiver
   accepts a known failure, not subject drift, and a receipt that no longer
   covers the current tree proves nothing about it.
3. **A general waiver excuses nothing here.** A review, plan,
   implement-incompleteness, or auto-waiver record names no gate command, so it
   resolves to no command and removes no gap. It keeps every other effect it
   already had.
4. **One owner for the policy.** The rule lives in the gap builder
   (`verification_readiness.required_receipt_gaps`), which both the closing gate
   and the delivery guard reach the same durable record through. The
   handler-side guard no longer reads the waiver at all; its remaining
   conditions are unchanged (inert under dry-run, and without a run directory or
   a declared contract). A second waiver check anywhere above the builder would
   re-open the divergence this ADR closes.
5. **Everything else about a waiver is preserved.** The operator verdict, the
   waived findings, and the prior reviewer critique are still injected into the
   downstream review gates (`review_changes`, `final_acceptance`) as the
   code-owned reconciliation directive of ADR 0072, so waived findings are still
   not reopened as blocking. Continuation over a precisely-waived failing gate
   still ends in the reviewer's own verdict. The record is still written to the
   session and to meta, still rehydrated verbatim on a fresh-process resume, and
   still surfaced in the evidence bundle. The backstop never writes a receipt to
   close its own gap.
6. **Correction runs inherit proof, not permission.** A correction child run
   reaches the release verdict through the same `final_acceptance` handler and
   therefore the same backstop. The parent's waiver record is not seeded into
   the child; the parent's *receipts* are inherited through the ADR 0089 parent
   sources read inside the same classification, so a child is excused only by a
   gate waiver its own gate handoff recorded.

Nothing here adds a flag, a profile field, an extras key, or a public function.
The gap dictionary (`{risk, missing_evidence, required_check}`), the
`engine_backstop` phase-log record, and the release schema are unchanged.

### What this replaces

This ADR partially supersedes ADR 0090. Specifically, in ADR 0090 §4 the clause
that makes the backstop inert "when an operator waiver (`continue_with_waiver`)
is active — the waiver is the explicit human decision the contract demands", and
the closing sentence "a recorded waiver disarms the backstop while remaining
durable in meta/evidence", are replaced by the exact-command rule above. Every
other part of ADR 0090 stands: the verification subject, receipt persistence,
delivery-policy derivation, the backstop's existence and its non-halting scope
are untouched. ADRs are append-only; ADR 0090 is not edited.

## Consequences

- An accepted critique can no longer be mistaken for a passed gate. A run whose
  required receipt is missing, failed, or stale reaches a REJECTED release
  verdict regardless of which general waiver is on file.
- The closing gate and the delivery guard now excuse exactly the same thing, so
  a run can no longer be green at final acceptance and blocked at delivery, or
  the reverse.
- The durable record holds a **single** waiver entry. A later review waiver
  therefore overwrites an earlier gate waiver, after which the previously waived
  command blocks again. That was already the delivery guard's behaviour; final
  acceptance now agrees with it instead of hiding it. Multi-record waiver
  storage is deliberately not introduced here — the conservative outcome
  (blocking) is the safe one, and the operator can re-waive the gate.
- The criterion backstop (ADR 0188) and the delivery gate remain separate
  authorities with their own gating; this ADR aligns the receipt backstop with
  the delivery guard and does not merge the three.
- A run that relied on a general waiver to ship unproven required gates will now
  stop. The remedy is the honest one: run the gate, or record a waiver that
  names it.

## Out of scope

- Storing more than one waiver record, or per-round waiver history.
- Making the criterion backstop waivable in any form.
- Any change to how a waiver is decided, persisted, rehydrated, or rendered.
