# ADR 0193 — The closing gate reads the latest applicable review

- **Status:** Accepted
- **Date:** 2026-09-16
- **Related:** [ADR 0039](0039-review-repair-phase-handoff.md),
  [ADR 0066](0066-repair-receipt-re-review-protocol.md),
  [ADR 0072](0072-continue-with-waiver-handoff-action.md),
  [ADR 0082](0082-verification-contract-final-acceptance-readiness.md),
  [ADR 0090](0090-require-gate-no-silent-green.md),
  [ADR 0109](0109-supersede-stale-final-acceptance-rejection.md),
  [ADR 0188](0188-typed-acceptance-criteria-and-criterion-matrix.md),
  [ADR 0192](0192-general-waiver-does-not-excuse-required-verification-proof.md)

## Context

`final_acceptance` closes a run that has usually already been reviewed. The
review/repair loop produces a verdict per round; ADR 0039 added a post-repair
re-review pass, ADR 0066 gave the repairer a receipt to hand the re-reviewer,
and ADR 0072 lets an operator continue over findings with a recorded waiver. By
the time the closing gate runs, the run may carry a rejection, a repair that
claims to have fixed it, a later verdict that agrees or disagrees, and an
operator rationale for continuing anyway.

The closing gate saw almost none of it. Three gaps, each independent:

**1. Only the last critique survived.** The round entry in the durable session
carried `critique` — the rendered body of whichever review ran last. A round
that was reviewed, repaired and re-verified therefore kept the re-verify prose
and lost the original verdict, its finding ids and their severities: exactly
the facts needed to say *what* was rejected and *whether the rejection still
stands*. A verdict string is not recoverable from rendered prose, and a finding
id certainly is not.

**2. The post-repair re-review left no per-attempt record at all.** The loop
re-dispatches `review_changes` after repair. Both dispatches wrote into the
same in-memory phase log and the same single round entry, so the second
overwrote the first. Two attempts with two verdicts were persisted as one.

**3. The obvious place to persist them is not available.** Every other phase
records itself under `session["phases"][<phase>]`, but `review_changes`
deliberately does not, and adding the key is not a neutral act: the checkpoint
callback (`_fsm_checkpoint`) saves any phase *that has a session entry* as a
completed checkpoint phase, and stamps a loop cursor when the phase belongs to
the active loop. `review_changes` is a loop phase. Creating
`session["phases"]["review_changes"]` would therefore begin writing checkpoint
rows and loop cursors for it and change what `resolve_loop_resume` restores —
a resume-behaviour change smuggled in under a documentation-shaped feature.

Without those facts the gate was left to infer. The nearest reviewer prose it
could see was ambiguous about provenance: it could not tell a standing
rejection from one a later pass had already overruled, nor an attempt whose
output never parsed from one that reported a real verdict. Meanwhile ADR 0109
had already established that the *latest authoritative* verdict is what a
reader must reconcile to — the closing gate simply had no durable way to
identify which review attempt that was.

## Decision

The closing gate is handed the latest applicable review as **evidence**, with
provenance, resolved from durable facts the run already writes.

1. **Per-attempt sub-records live inside the round entry.** Each
   `review_changes` invocation is persisted as `review` or `reverify` **inside**
   its `session["phases"]["rounds"][n]` entry — additive, and invisible to
   checkpoint and loop-resume, which is the whole reason for the placement. A
   round entry may therefore hold both attempts alongside the existing
   `critique` / `repair_receipt` fields. Writing the same `(round, pass)` twice
   overwrites the same key, so a replayed attempt stays one attempt.

   Attempt identity is `(round, pass)`, and the pass comes from an **explicit
   runner signal** that the current dispatch is the post-repair re-verify pass —
   never from "a `review` key already exists". Inferring the pass from key
   presence would silently relabel a re-run of the first attempt as a
   re-verification, inventing a second opinion that nobody gave.

   The pass is identity, not chronology. It does not say where the round's
   repair sat relative to the attempt: the operator-feedback retry round runs
   `repair_changes -> review_changes` and still records the round's first
   `review` pass, so there the repair precedes the review. Each sub-record
   therefore states the ordering it observed at write time
   (`repair_preceded`), read off the round entry rather than derived from the
   pass. Without it an already-reviewed repair would be announced to the
   closing gate as an unverified post-review claim, inviting the gate to
   reopen settled work.

2. **One resolver over durable facts.** A single focused module resolves the
   context from the round sub-records, the operator waiver record, and the
   phase-handoff decision artifacts. It reads the live in-memory session when
   there is one and the run's durable session file otherwise, and the two paths
   are deliberately indistinguishable downstream: **how** the facts were loaded
   appears neither in the serialized record nor in the rendered block. A
   fresh-process resume therefore produces a byte-identical block. Provenance is
   exactly the run id plus each attempt's `(round, pass)` — no file paths, no
   loader names, no timestamps of reading.

   The resolver is lenient about absence and strict about invention: a missing
   or corrupt durable file, a run with no rounds, and a dry run all yield *no
   context* rather than an empty or fabricated one, and a dry run reads nothing
   from disk at all.

3. **Ordering, supersession and what a repair proves.** Attempts are ordered by
   round ascending, and within a round `review` before `reverify`.

   - **latest** = the last *valid* attempt. An attempt whose output failed to
     parse carries no verdict and can never be latest.
   - **superseded** = the earlier valid attempts that were REJECTED, including
     the `review` of the same round when its `reverify` approved.
   - **invalid** = every attempt that failed to parse, reported separately and
     explicitly as not evidence. An invalid attempt supersedes nothing, so a
     later unparseable pass leaves the standing verdict exactly where it was.
   - **A repair is a claim, not a resolution.** A repair receipt the latest
     attempt did not see — recorded after it, with no valid review since — is
     presented as an unverified claim to check against the current subject; it
     never moves a finding to resolved. A repair the latest attempt *did* see
     (`repair_preceded`) is reported as the context the standing verdict was
     reached in, not as an open claim. The findings of a REJECTED latest attempt stay open until a later
     *valid* attempt says otherwise. Waived findings are marked as waived, by
     the same finding identity the evidence layer uses, with the operator's
     rationale attributed to the handoff that recorded it.

4. **This is evidence, not authority.** The block reports what a reviewer said
   and what an operator decided. It changes no verdict rule. The readiness
   summary (ADR 0082) remains the proof surface, and both engine backstops keep
   their existing authority and gating unchanged: the required-receipt backstop
   (ADR 0090) with the exact-command waiver rule of ADR 0192, and the
   acceptance-criteria backstop (ADR 0188), which honours no waiver at all.
   Neither backstop reads the review context. Prose inside a finding, a critique
   or a waiver that instructs the gate to approve is untrusted text and moves
   nothing — an unproven required receipt or an open acceptance criterion still
   forces a REJECTED release verdict.

   The framing that says so is **code-owned** and rides with the block in one
   typed prompt part. It is not a user-editable role/task/format part, so a
   project prompt override cannot restate the evidence as an instruction or let
   it stand in for the readiness summary.

5. **The resolved context is durable.** What the gate was handed is recorded on
   the `final_acceptance` phase entry as `review_context` and persisted to the
   session, including on the halted parse-failure path — a gate that halted must
   still show which evidence it was given. A run with no prior review writes no
   key and renders no part, leaving the wire prompt byte-identical to a run from
   before this surface existed.

Nothing here adds a flag, a profile field, a mode, or a gate primitive.

## Consequences

- The closing gate can distinguish a standing rejection from an overruled one,
  and an unparseable attempt from a verdict, because both distinctions are now
  durable facts rather than inferences from prose.
- A round that was reviewed, repaired and re-verified keeps **both** verdicts.
  Evidence and audit readers gain per-attempt history they previously lost.
- `session["phases"]["review_changes"]` stays absent. Checkpoint rows, loop
  cursors and `resolve_loop_resume` behaviour for the review/repair loop are
  unchanged — the cost is that per-attempt records are reachable only through
  the round entry, which is the intended trade.
- The durable session and run-state file gain two additive keys
  (`rounds[n].review` / `rounds[n].reverify` and
  `final_acceptance.review_context`). Readers that do not know them are
  unaffected; pinned session-shape snapshots were regenerated for the addition.
- The repair/review ordering is a durable fact per attempt. A producer that
  writes a review sub-record without first composing the round's repair
  evidence would report the ordering wrongly; readers fall back to what the
  pass implies when a record predates the field.
- Attempt identity depends on the runner's explicit re-verify signal. If that
  signal is renamed or removed, re-verify passes silently degrade to `review` —
  so the behaviour is pinned by a test that fails loudly rather than drifting.
- A repair that fixed everything but was never re-reviewed still reads as
  unresolved findings plus an unverified claim. That is deliberate: the
  conservative reading is the correct one, and the remedy is a re-review.

## Out of scope

- Multi-record or per-round waiver history. The durable record still holds a
  single waiver entry (ADR 0192); this ADR reads it, it does not restructure it.
- Any change to the `evidence.json` schema or to the MCP wire surface.
- Any change to how a review verdict is parsed, how a waiver is decided or
  rehydrated, or how the review/repair loop routes.
- Making the review context authoritative over any gate decision, or letting it
  substitute for verification readiness.
