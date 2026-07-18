# ADR 0139 — Operating mode is the sole sanction authority for scope expansion

- **Status:** Accepted
- **Date:** 2026-07-18
- **Supersedes:** ADR 0112 §5 sanction matrix

## Context

ADR 0112 separated scope-expansion classification from its delivery outcome,
but retained two exceptions to the selected operating mode: a `pro` blocker
opened a phase handoff, and selected categories or evidence forced a rejected
release in every mode. A normal correction run therefore reached an operator
handoff and rejected delivery although its configured operating mode was `pro`
and all declared verification had passed.

The classifier's `notice`, `risk`, and `blocker` values are useful durable
evidence. They must not become a second, hidden strictness policy alongside
the operating mode.

## Decision

`pipeline.runtime.scope_expansion_sanction.decide` is the single resolver for
the disposition of every scope expansion. It receives the classified status,
the operating mode, and an active waiver flag. Category and evidence remain in
the assessment and presentation, but never override the selected mode.

| Operating mode | Scope-expansion disposition |
| --- | --- |
| `fast` | Record and continue (`AUTO_CONTINUE`). |
| `pro` | Record, disclose, and continue (`AUTO_ALERT`). |
| `governed` | Record and open the existing delivery handoff (`HANDOFF`). |

Consequently, scope expansion alone never manufactures a release gap or a
`REJECTED` final-acceptance verdict. Required verification, review findings,
and explicit delivery policy retain their existing authority to reject a
release. The `governed` handoff is the only scope-expansion path that asks an
operator whether delivery may proceed.

## Consequences

- `forces_rejected` is always false for scope-expansion routing.
- `needs_phase_handoff` is true only for a governed run.
- `pro` runs preserve complete scope evidence and an operator-visible warning
  without pausing, waiving, or suppressing delivery.
- The obsolete `HALT_WAIVER` route and its parallel category-based policy are
  removed rather than retained as an inactive fallback.

## Verification

The policy matrix is pinned in
`tests/unit/pipeline/runtime/test_scope_expansion_sanction.py`; final-acceptance
coverage verifies the same result for persistence and destructive-change
classifier evidence in all operating modes.
