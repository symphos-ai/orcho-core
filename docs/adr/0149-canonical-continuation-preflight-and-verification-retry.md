# ADR 0149 — Canonical continuation preflight and verification retry

- **Status:** Accepted
- **Date:** 2026-07-21
- **Related:** ADR 0021, ADR 0081, ADR 0133, ADR 0140

## Context

Checkpoint resume, retained-change correction, and reuse of a persisted plan
are different operations.  Treating them as variants of a generic resume lets a
terminal parent consume a finalized execution ledger, lets a plan artifact
accidentally recover a retained change, or makes each launch surface infer its
own policy.

Similarly, a phase handoff at `final_acceptance` is not necessarily a scope
expansion decision.  A failed verification gate can occur there too; routing
from the phase first misclassifies the recovery subject.

## Decision

### One operation selector and preflight

`ContinuationRequest` plus `resolve_continuation` is the sole owner of
operation selection.  CLI, SDK launch functions, and transports are thin
adapters: they supply the explicit intent, render its typed blocker, and invoke
only the operation selected by the reducer.  `preflight_continuation` owns the
disk checks immediately before launch, including malformed-ledger refusal.

| Intent | Required durable subject | Selected operation | Isolation rule |
| --- | --- | --- | --- |
| `resume` | checkpoint-resumable paused/interrupted state | `resume_checkpoint` | Reuses the parent run directory only when its scheduled-gate ledger is not finalized. |
| `followup` | isolated, dirty retained worktree and non-empty operator comment | `start_followup` | Starts a distinct child id and fresh output directory; the child records parent lineage. |
| `from_run_plan` | parent `parsed_plan.json` | `launch_from_run_plan` | Starts a distinct child id and fresh output directory; it consumes the plan artifact only. |

No fallback selects a different row.  In particular, a retained-change
correction cannot use `from_run_plan`; a clean, missing, unreadable, or
non-isolated retained worktree is a blocker, not permission to apply an
artifact diff.  A finalized scheduled-gate ledger blocks only same-run
checkpoint resume.  It must not block either fresh-child operation, because a
child has its own ledger and execution subject.

The reducer returns a typed `blocked` resolution for every unsupported or
unproven request.  A launcher must perform all control-plane validation before
spawning, so a refused operation creates no subprocess and consumes no durable
decision.

### Trigger-first handoff routing and exact-once consumption

`classify_handoff_route` selects the handoff owner.  It examines
`trigger="verification_gate_failed"` before phase-based classification.  Such a
handoff is `verification_retry` even when its phase is `final_acceptance`; only
an explicit `scope_expansion:*` trigger at `final_acceptance` is scope
expansion.  Missing or ambiguous trigger/identity facts fail closed as a
blocker.

The verification route requires an exact gate identity
`(command, hook, phase)`, from the handoff artifact or an unambiguous ledger
match.  Its owner validates feedback, the active handoff id, retained repair
subject, and an available `repair_changes` step before consuming the decision.
It then performs exactly one repair and reruns exactly that gate identity over
the fresh repaired subject.  A failed rerun publishes a new handoff; a passed
rerun continues.  A routing or control-plane failure restores the original
handoff for operator recovery.  Provider/process crashes are not translated
into blockers and continue through the normal interrupted/failed lifecycle.

Decision consumption remains exact-once: an action applies only to its active
handoff identity, and retry state cannot be reused to execute a second repair
or a different gate.  This ADR defines neither an MCP implementation nor a
`final_acceptance` recovery mechanism; those remain separate consumers or
phase concerns.

### Additive SDK and companion MCP contract

The public SDK additions are typed and additive.  `RunStatus` exposes optional
`continuation_decision`; the SDK exports `ContinuationDecision`,
`ContinuationRequest`, `ContinuationResolution`, `resolve_continuation`, and
`resolve_continuation_decision`; and run control exposes the separate resume,
correction-followup, and from-run-plan launch operations.

The companion `orcho-mcp` adapter must project these exact fields without
inventing a second classifier:

- `ContinuationDecision`: `run_id`, `continuation_subject`,
  `recommended_next_action`, `allowed_intents`, `requires_operator_comment`,
  `checkpoint_resumable`, `retained_worktree`, `diff_source`, `blocked`, and
  `reason`.
- `ContinuationRequest`: `run_id`, `intent`, `operator_comment`; and
  `ContinuationResolution`: `request`, `decision`, `operation`, `blocker`.
- Enum values: subject `checkpoint | plan_artifact | retained_change | none`;
  intent `resume | followup | from_run_plan`; operation
  `resume_checkpoint | start_followup | launch_from_run_plan | blocked`;
  recommended action `resume_checkpoint | plan_artifact_continuation |
  start_followup | none`; and diff source `worktree | artifact | none`.
- Launch semantics: checkpoint resume reuses only a non-finalized parent;
  followup requires a comment and retained dirty worktree; from-run-plan
  requires `parsed_plan.json` and is never a retained-change route.  Both
  child operations require a distinct id, fresh output directory, and parent
  lineage.

No `orcho-mcp` production file changes in this repository.  Its adapter,
schema/registration coverage, and mock E2E validation must land in the
companion repository before stable promotion.  There is no intermediate stable
promotion of core-only wire values: SDK schema and MCP projection move as one
coordinated release boundary.

## Consequences

- Every core launch entry point shares one selection and preflight policy.
- Gate recovery cannot be misrouted merely because it paused at
  `final_acceptance`.
- Operators retain a recoverable handoff after a validation blocker, while a
  real provider crash remains visible as a crash rather than a false retry
  choice.
- Consumers can expose the additive typed wire only by adapting the published
  fields and enum values above.

## References

- [Phase lifecycle](../architecture/phase_lifecycle.md)
- [Verification contract](../architecture/verification_contract.md)
- ADR 0021 — Public SDK boundary
- ADR 0133 — Retained-change correction followups
