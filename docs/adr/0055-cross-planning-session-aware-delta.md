# ADR 0055 — Cross planning session-aware delta rendering

- **Status:** Accepted
- **Date:** 2026-05-29
- **Deciders:** project owner
- **Relates to:** [ADR 0026](0026-session-aware-prompt-parts.md),
  [ADR 0036](0036-agent-session-persistence-across-subprocess-restart.md),
  [ADR 0047](0047-cross-project-application-boundary.md)

## Context

Single-project ("mono") planning already does session-aware delta
rendering: when a plan is rejected and round 2 resumes the same provider
session, `pipeline.phases.builtin._session_aware_invoke` consumes the M2
`PromptRenderEnvelope`, runs the M6 selector (`select_parts_for_turn`),
and sends only the decision-bearing delta — the new task framing plus the
reviewer critique. The role, format, task prose, contracts, and policy
parts the architect already saw are omitted from the resumed turn.

Cross-project planning did **not**. `pipeline.cross_project.planning_loop`
built the full `cross_plan` / `cross_replan` prompt each round and called
`ctx.plan_agent.invoke(full_prompt, continue_session=True)` directly,
bypassing the selector. So round 2 (`cross_replan`) re-glued every static
part — `plan_artifact_boundary`, `cross_subtask_blocks`,
`role=systems_architect`, `format=detailed`, `task=cross_replan`,
`policy=authoring_language` — even though the trace showed
`session=resume:<uuid>`, i.e. the provider already held round 1's context.
The session was resumed but not respected.

The M2 envelope was already being published by the cross prompt builders
(via `_render_prompt_output`); it was simply discarded instead of feeding
the selector.

## Decision

Route the cross planning loop's plan-agent invocations through a
session-aware delta helper, mirroring mono.

- New `pipeline/cross_project/session_invoke.py::session_aware_invoke` —
  the same core dance as the mono helper (take envelope → compute
  `PhysicalSessionKey` under `PER_PHASE` → `select_parts_for_turn` →
  delta wire + republish the prompt-trace composition → invoke → commit
  on success), but **decoupled from `PipelineState`**. It threads a plain
  `prompt_sessions` mapping instead, because the cross loop has its own
  `CrossPlanningContext`, not a pipeline state.
- `CrossPlanningContext` gains an in-memory `prompt_sessions` dict
  (`field(default_factory=dict)`). Both `_invoke_plan_round` and
  `_retry_feedback_round` invoke through the helper with
  `phase="cross_plan"`, so round 1 and the replan share one reusable
  session — exactly as mono keys plan + replan under `phase="plan"`.

## Scope / non-goals

- The helper deliberately does **not** persist the provider session id to
  the run checkpoint (mono's E1) or write M12 trace metadata into a phase
  log. Those are pipeline-state concerns; the cross orchestrator handles
  session/usage elsewhere. Keeping the helper narrow avoids dragging
  `PipelineState` coupling into the cross package.
- The reviewer (`cross_validate_plan`) is unchanged. In practice its
  runtime ran stateless in the observed trace, so delta rendering would
  no-op there; wiring it is a separate follow-up if that runtime starts
  resuming.
- Not a wire-format change — no `orcho-mcp` update required.

## Consequences

- In-process rounds (round 1 → round 2 after a QA reject within one
  `run_cross_planning` call) share the in-memory store and get delta
  rendering: round 2 sends the new `cross_replan` framing + critique and
  omits the stable prefix the architect already holds.
- A cross-process resume (operator handoff → fresh subprocess) starts with
  an empty store, so the first post-restart invoke renders full. This is
  identical to mono's cold-session behaviour (mono also re-sends on the
  first invoke after a subprocess restart) — parity, not a regression.
- Round 1 output is unchanged: the full-render path sends
  `envelope.text`, which is byte-identical to the builder's full prompt.
