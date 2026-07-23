# ADR 0133: Retained-change correction follow-ups

## Status

Accepted.

## Context

A rejected final-acceptance run can retain an uncommitted change in its
isolated worktree.  Earlier control paths described that recovery as a
`from_run_plan` continuation.  That is incorrect: a parsed plan is not the
change being recovered, and a patch artifact cannot safely be replayed as a
substitute for the retained worktree.

Checkpoint resume, plan-artifact continuation, and correction follow-up have
different ownership and safety rules.  Frontends also need one durable,
typed answer rather than independently inferring a correction from log text.

## Decision

Core exposes one `ContinuationDecision`, resolved from persisted run status,
halt reason, correction-gate state, and `meta.worktree`.  A terminal
`commit_decision_fix`, `final_acceptance_rejected`, or
`final_acceptance_no_diff` is a retained-change candidate only when the
recorded worktree is readable and still has an uncommitted change.  Its only
operator intents are `followup` and `exit`; a follow-up requires a non-empty
operator comment.  Missing, clean, unreadable, or artifact-only evidence is a
typed blocked decision and never falls through to plan-artifact promotion.

A correction follow-up is a new sibling run with `resume_mode="followup"`,
the `correction` profile, the exact parent worktree, and a
`correction_context.md` artifact containing persisted rejection facts and the
operator comment.  It begins with `CORRECTION_TRIAGE`; it does not run PLAN or
VALIDATE_PLAN.  Launch is client-neutral and detached.  The launch seam
rechecks the durable decision immediately before spawning.

`from_run_plan` remains a plan-only continuation.  It requires a parsed plan
and a parent task for a ready call; it never represents correction recovery.
Successful delivered correction children supersede their rejected parent based
on ordinary follow-up lineage, correction profile/context, and delivery state.

## Consequences

SDK and MCP-facing action records include readiness metadata, so clients can
render or elicit required operator input without recreating policy.  The
recorded parent worktree is a hard continuity invariant: teardown or a clean
worktree is an expected blocked outcome, not permission to start on a fresh
checkout or apply an artifact diff.
