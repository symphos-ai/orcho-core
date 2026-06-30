"""Session-aware delta invoke for the cross planning loop (ADR 0055).

Mirrors :func:`pipeline.phases.builtin._session_aware_invoke` (the mono
path) but is decoupled from :class:`pipeline.runtime.PipelineState`: it
threads a plain ``prompt_sessions`` mapping instead of reaching into a
pipeline state object. The cross planning loop has its own context
(:class:`pipeline.cross_project.planning_loop.CrossPlanningContext`),
not a ``PipelineState``, so it cannot reuse the mono helper directly.

ADR 0026 / 0027 delta rendering: on a *resumed* provider session the
agent already holds the stable prefix parts (role, format, task framing,
contracts, policy) in its conversation. Re-sending them every round is
wasteful and is exactly what cross replan did — it called
``agent.invoke(full_prompt, continue_session=True)`` directly, bypassing
the M6 selector. This helper closes that gap: it consumes the
:class:`~pipeline.prompts.turn.PromptTurn` published by the prompt
builder, runs :func:`~pipeline.prompts.delta.select_parts_for_turn`,
and on a resumed turn sends only the decision-bearing delta (the new
cross_replan task framing + the reviewer critique), omitting the parts
the architect already saw.

Scope vs the mono helper: this intentionally does NOT persist the
provider session id to the run checkpoint (E1) or write M12 trace
metadata into a phase log — those are pipeline-state concerns the cross
orchestrator handles elsewhere. In-process rounds (round 1 → round 2
after a QA reject within one ``run_cross_planning`` call) share the
in-memory ``prompt_sessions`` store and therefore get delta rendering. A
cross-process resume (operator handoff → new subprocess) starts with an
empty store, so the first invoke after restart degrades to a full render
— identical to the mono path, which also re-sends on a cold session.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pipeline.prompts.session import PromptSessionSplit

if TYPE_CHECKING:
    from pipeline.prompts.turn import PromptTurn


def session_aware_invoke(
    agent: Any,
    *,
    prompt_sessions: dict,
    run_id: str,
    phase: str,
    turn: PromptTurn,
    cwd: str,
    continue_session: bool,
    split: PromptSessionSplit = PromptSessionSplit.PER_PHASE,
) -> str:
    """Invoke ``agent`` with ADR 0026 session-aware delta rendering.

    The full prompt turn is produced by the caller's builder.  This helper
    takes that turn, computes a
    :class:`~pipeline.prompts.session.PhysicalSessionKey` under ``split``
    (default :data:`PromptSessionSplit.PER_PHASE`, keyed on ``phase`` so
    both round 1 and the replan share one reusable session), and selects
    the wire parts:

    * Stateless split / no prior state / fresh provider session → full
      render; on success, seed ``sent_part_keys`` so the next round can
      delta.
    * Resumed session with prior state → delta render; omit stable parts
      already sent.

    State is committed into ``prompt_sessions`` only after a successful
    invoke (commit-on-success), so a provider failure leaves the cache
    view untouched for a retry.
    """
    from core.observability.prompt_trace import set_last_prompt_turn as _set_turn
    from pipeline.prompts.delta import select_parts_for_turn
    from pipeline.prompts.session import (
        PromptSessionState,
        make_session_key,
        with_provider_session_id,
    )

    source_envelope = turn.envelope()

    runtime_id = f"{type(agent).__module__}.{type(agent).__qualname__}"
    model_key = str(getattr(agent, "model", "") or "")
    key = make_session_key(
        run_id=run_id,
        runtime=runtime_id,
        model_key=model_key,
        split=split,
        phase=phase,
    )
    session_state = prompt_sessions.get(key) if key is not None else None
    # Delta rendering is only valid when this call resumes the same
    # physical provider session. When starting fresh (round 1, or any
    # ``continue_session=False`` call) select against a fresh state so
    # the successful invoke overwrites any stale stored state with the
    # new provider session id.
    if key is not None and (session_state is None or not continue_session):
        session_state = PromptSessionState(key=key)

    result = select_parts_for_turn(source_envelope, session_state, split)
    state_update = result.state_update
    if result.render_mode == "delta":
        effective_turn = turn.render_selected(result.selected_parts)
    else:
        effective_turn = turn

    _set_turn(effective_turn)
    raw = agent.invoke(effective_turn.text, cwd, continue_session=continue_session)

    if state_update is not None and key is not None:
        provider_session_id = getattr(agent, "session_id", None)
        prompt_sessions[key] = with_provider_session_id(
            state_update, provider_session_id,
        )
    return raw


__all__ = ["session_aware_invoke"]
