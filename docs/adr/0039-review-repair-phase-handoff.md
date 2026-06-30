# ADR 0039: Review/Repair Phase Handoff

Status: Accepted

Addendum (2026-05-26): the delayed pause now waits for the paired
`repair_changes` step **and** one final validating `review_changes` pass. The
initial rejected review still decides that the loop has exhausted automatic
budget, but the runner re-reviews after repair before resolving the handoff. If
the final review approves, the pending pause is cleared and the loop exits via
`until: review_changes.clean`. If the final review still rejects, the persisted
handoff payload and the operator-facing outcome are built from that post-repair
verdict, not from the stale pre-repair critique.

Addendum (2026-05-26 part 2): the validating pass must keep the reviewer's
conversational context — it is auditing fixes to its own prior findings, so a
cold session would force a re-read of the diff and risk diverging from the
original critique. The loop runner sets `state.extras["_review_reverify_resume"]
= True` around the re-dispatch (cleared in a `finally`); `_should_resume(state,
"repair_round")` in the review handler honors this flag, returning `True` even
when `round_n == 1`, so the agent runtime receives `continue_session=True` and
resumes its prior session. The flag is scoped to the `repair_round` key, so
plan-loop session resume is unaffected. The phase banner emitted by
`pipeline.project.profile_dispatch.emit_phase_banner` reads the same flag and
appends ` (re-verify)` to the `review_changes -- Round N` label so the
validating pass is visually distinct from the original review of the same
round.

Addendum (2026-05-27): cross CLI prompt parity is intentionally narrower than
the full cross-parent proxy surface. The CLI may prompt and immediately resume
ADR 0038 `cross_plan:*` pauses because the decision belongs to the cross run's
own planning loop. A proxied child pause with id
`project:<alias>:<child_handoff_id>` still exits as a resumable pause (`rc=4`)
for off-band resolution through the SDK / MCP / child-run path; writing that
decision through the cross CLI prompt would route it to the parent run instead
of the child run that owns the underlying handoff.

Addendum (2026-05-31): a `retry_feedback` repair round ran its `repair_changes`
agent `session=stateless`, losing the implementer's context. Two facts combined.
(1) Two distinct axes are easy to conflate: `execution.session_split`
(`common` / `per_role` / `per_phase`, ADR 0026/0027) only groups physical
sessions for prompt-part reuse and never drives `--resume`; only
`SessionMode.CHAIN` → `continue_session=True` produces an actual resume.
(2) `retry_feedback` lands at `repair_round = prior + 1` (> 1), and at
`repair_round > 1` the handler swapped `repair_changes_agent →
repair_escalation_agent`. Because `repair_escalation` is a more capable model
(default opus-4-7 vs base sonnet-4-6) and `AgentRegistry.resolve` builds a fresh
instance with `session_id=None`, that round could not resume, and
`_resolve_fix_runtime_config` resolved it to a cold `STATELESS`.

Decision: a human-directed `retry_feedback` round is a **continuation**, not an
escalation. The loop driver already marks it via
`HUMAN_DIRECTED_FLAG_KEY = "_phase_handoff_human_directed_round"`. When that flag
is set, `_resolve_fix_runtime_config` keeps the base `repair_model` and resolves
session mode from the implement turn (so AUTO yields `CHAIN` on matching
models), and `_phase_repair_changes` reuses `implement_agent` — the object that
already carries the captured `session_id` — instead of swapping to the fresh
escalation agent. The human round therefore resumes the implementer thread.

For **automatic** budget rounds the escalation swap is unchanged, but their
session mode now resolves through `_resolve_session_mode` on every round rather
than a hardcoded `STATELESS`: a cross-model escalation under the shipped
`chain_same_model_only=true` guard resolves to `HYBRID` (codemap re-prime),
matching the long-documented "Round 2+ with escalation defaults to HYBRID"
intent. Explicit `stateless` / `chain` / `hybrid` still pass through unchanged.

## Context

ADR 0038 gave cross-plan validation the same operator handoff lifecycle as
single-project plan validation. The remaining asymmetry was the
`review_changes` / `repair_changes` loop: when automated review budget was
exhausted, single-project runs and cross child runs had no pause point where an
operator could choose to continue, provide targeted feedback, or halt.

## Decision

`review_changes` may declare `handoff: human_feedback_on_reject` when it is
inside the canonical `review_changes -> repair_changes` loop with
`until: review_changes.clean`.

The trigger is intentionally delayed until the current `repair_changes` step
finishes. The rejected review verdict creates the handoff payload, but the
runner lets the final automatic repair land and persists the round trace before
pausing. This preserves the existing meaning of `max_rounds`: a configured
repair round still performs a repair.

Resume semantics:

- `continue` accepts the repaired state as-is and skips the review/repair loop.
- `retry_feedback` runs one human-directed `repair_changes -> review_changes`
  round using the previous critique plus operator feedback.
- `halt` remains terminal through the existing phase-handoff decision path.

For cross-project runs, a child project pause is proxied through the parent
cross run. The parent publishes a `phase_handoff` payload with id
`project:<alias>:<child_handoff_id>` and stores the child handoff id in
`artifacts.child_handoff_id`. On resume, the parent decision is written through
to the child run before that child is resumed.

## Consequences

The built-in `advanced`, `enterprise`, and `task` profiles now pause on
unresolved review rejection after their automatic repair budget is exhausted.
`lite` and `review` remain bypass-style flows.

The public phase-handoff payload shape is unchanged. New payload values may now
have `phase="review_changes"` and ids such as
`review_changes:repair_round:1` or
`project:api:review_changes:repair_round:1`.
