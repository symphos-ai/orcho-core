# ADR 0165 — Eager fast verification after repair

- **Status:** Accepted
- **Date:** 2026-07-29
- **Relates to:** [ADR 0117](0117-verification-blocking-tier-independent-of-cost.md), [ADR 0132](0132-scheduled-gate-selection-execution-and-disposition.md), and [ADR 0151](0151-verification-ownership-and-cost-aware-agent-variants.md)

## Context

An `after_phase(implement)` gate can pass, after which a reviewer-requested
`repair_changes` phase mutates the verified subject again. Before this decision,
the next automatic check was pre-final receipt materialization. A one-second
lint failure introduced by the repair therefore appeared only after the review
loop had closed and forced a full correction follow-up.

Asking the repair agent to infer a changed-file command from prose is not a
reliable control boundary. Adding a `diff-aware` flag would create another
policy axis even though the contract already has deterministic selection,
engine ownership, and a `fast | moderate | slow | unknown` cost vocabulary.

## Decision

The engine eagerly reruns every selected, engine-owned
`after_phase(implement)` identity whose resolved cost is `fast` after a
subsequent `repair_changes` phase.

The eager execution preserves the original `(command, hook, phase)` identity.
It is recorded as a rerun in the existing scheduled-gate ledger and writes the
usual immutable execution receipt plus the latest command receipt. It does not
create a synthetic `after_phase(repair_changes)` identity.

The original effective policy, action, consequence, and repair budget remain
authoritative. A failed `repair_loop` identity uses the existing bounded repair
flow; warning and terminal actions retain their declared behavior. Moderate,
slow, and unknown-cost identities remain on their declared schedule. Pre-final
verification reuses a fresh passing receipt and does not execute the command
again.

`cost` therefore affects eager execution cadence only. It still does not alter
selection, executor ownership, policy, action, consequence, disposition, or
receipt freshness.

## Consequences

- Fast hygiene regressions introduced by repair are caught while the existing
  repair loop is still available.
- Projects do not need a second `diff-aware` declaration or agent-side command
  inference.
- A fast command may run more than once during a repair-heavy run; that is the
  intended cost contract.
- Authors should classify as `fast` only commands cheap enough to execute after
  every mutation of the verified subject.

## Rejected alternatives

1. **Infer changed-file commands in the prompt.** Rejected because prose and
   shell semantics cannot define deterministic execution ownership.
2. **Add a `diff-aware` contract flag.** Rejected because command scope already
   belongs to the declared command, while selection and cost are independent
   existing axes.
3. **Wait for pre-final verification.** Rejected because a cheap repair-local
   defect then escapes the bounded repair loop and requires a correction run.
4. **Rerun every gate after repair.** Rejected because moderate and slow gates
   retain their explicitly declared cadence.
