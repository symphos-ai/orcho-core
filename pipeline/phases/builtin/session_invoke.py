# SPDX-License-Identifier: Apache-2.0
"""Session-aware agent invocation for the builtin phase handlers.

Owns ``_session_aware_invoke`` — the single boundary every handler calls
to put a prompt on the wire. It resolves the physical session key and
split (``session_keys``), runs the M6 delta selector, invokes the agent,
commits the prompt-session state, persists the provider session id to the
run checkpoint (E1), and stamps the post-invoke observability records
(``session_observability``). Heavy prompt imports stay lazy, so the module
imports cheaply and never re-enters ``pipeline.phases.builtin`` during
package init.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pipeline.phases.builtin.session_keys import (
    _agent_to_role_attr,
    _compute_session_key,
    _physical_session_key_to_dict,
    _resolve_session_split_for_step,
)
from pipeline.phases.builtin.session_observability import (
    _TraceInputs,
    render_live_card,
    stamp_context_clearing,
    stamp_context_growth,
    stamp_context_pressure,
    stamp_prompt_render,
    stamp_runtime_compaction,
)

if TYPE_CHECKING:
    from pipeline.prompts.turn import PromptTurn
    from pipeline.runtime import PipelineState


def _is_missing_provider_session_error(exc: Exception) -> bool:
    """True when a runtime rejected a stale local conversation id."""
    text = f"{exc} {getattr(exc, 'stderr', '')}".lower()
    return any(needle in text for needle in (
        "no conversation found with session id",
        "conversation not found",
        "session not found",
        "no session found",
    ))


def _notify_provider_session_fallback(
    state: PipelineState,
    *,
    phase: str,
    stale_session_id: str | None,
) -> None:
    """Surface a recovered missing-provider-session fallback.

    Called only after the fresh-session retry has already succeeded, so
    this is a first-class recovery notice — not a phase failure. The
    original missing-session exception was caught and fully handled; no
    ``failed`` record leaks out.

    Always emits a structural ``phase.provider_session_fallback`` event
    so the recovered attempt stays observable in debug / progress
    artifacts even when the operator line is suppressed. Prints exactly
    one compact warning line unless the run is under SILENT presentation
    (``state.extras["_silent"]``), mirroring the phase-output-preview
    suppression rule.
    """
    from core.observability import events as _events

    sid = stale_session_id or "unknown"
    _events.emit(
        "phase.provider_session_fallback",
        phase=phase,
        stale_session_id=sid,
        recovered=True,
    )
    if not (state.extras and state.extras.get("_silent")):
        from core.observability.logging import warn

        warn(
            f"Provider session resume unavailable for {phase} ({sid}). "
            "Continuing with a fresh provider session, the same run "
            "worktree, and persisted run context."
        )


def _reset_agent_session(agent: Any) -> None:
    reset = getattr(agent, "reset_session", None)
    if callable(reset):
        reset()
        return
    if hasattr(agent, "session_id"):
        agent.session_id = None
    if hasattr(agent, "_followup_resume_pending"):
        agent._followup_resume_pending = False


def _session_aware_invoke(
    agent: Any,
    state: PipelineState,
    *,
    phase: str,
    turn: PromptTurn,
    cwd: str,
    continue_session: bool = False,
    attachments: tuple = (),
    split: Any = None,
    mutates_artifacts: bool = False,
    trace_phase: str | None = None,
    delta_droppable_part_ids: tuple[str, ...] = (),
    commit_session: bool = True,
) -> str:
    """Invoke ``agent`` with M2/M5/M6 session-aware delta rendering.

    The full prompt is produced by the caller via the normal builder
    pipeline as a :class:`~pipeline.prompts.turn.PromptTurn`. M7 derives
    the :class:`PromptRenderEnvelope` from the turn, runs the M6
    selector, and decides between full and delta wire prompts.

    ``mutates_artifacts`` (M9) propagates the runtime write-flag to
    ``agent.invoke``. Implement and repair_changes opt in so the
    runtime exposes Edit/Write tools; reviewer phases keep the
    M7-default ``False`` so the runtime stays read-only.

    Decision flow per call:

    1. Derive the source envelope from ``turn.envelope()``.
    2. Compute a :class:`PhysicalSessionKey` under
       :data:`PromptSessionSplit.PER_PHASE` (M11 will add profile
       knobs). Runtime id derives from the agent class; model key
       from ``agent.model``.
    3. Look up :class:`PromptSessionState` for that key in
       ``state.prompt_sessions``.
    4. Run :func:`select_parts_for_turn`. The selector returns
       ``render_mode="full"`` for stateless/no-state/fresh-session,
       ``"delta"`` for resumed.
    5. If delta, build the effective turn via
       ``turn.render_selected(result.selected_parts)`` and set the
       prompt-turn slot so the runtime adapter's debug renderer shows
       what actually went on the wire (M7 trace-honesty rule). If
       full, use the original turn.
    6. Invoke the agent. Exceptions propagate without committing
       state. Parse failures count as success (the provider returned
       text), so state commit happens on a successful return
       regardless of downstream parsing.
    7. After successful invoke, fold the provider's ``session_id``
       into the candidate ``state_update`` and write it to
       ``state.prompt_sessions[key]``.
    8. Stash trace metadata at ``state.phase_log[phase]
       ["prompt_render"]``: render_mode, session_key (dict view),
       selected/omitted part keys, prefix/payload hashes, wire char
       count. M12 will durably persist this; the validate_plan
       handler must merge ``prompt_render`` into its own phase_log
       overwrite.

    ``trace_phase`` separates the session-key phase from the trace
    attribution slot. When set, prompt_render metadata is written
    under ``state.phase_log[trace_phase]`` while the session key
    still uses ``phase``. Default ``None`` keeps the two aligned.
    CHAIN repair_changes uses ``phase="implement"`` to share the
    implement physical session key while attributing the trace to
    ``trace_phase="repair_changes"`` so the repair handler's phase
    log entry actually carries the render metadata for M12.

    ``commit_session`` (ADR 0113, F1) controls whether a successful invoke
    writes its provider session id back into ``state.prompt_sessions[key]``
    (and the E1 checkpoint row). Default ``True``. A fresh new-zone/companion
    subtask invoke shares the implement physical session key (so it reuses the
    same delta/trace plumbing) but must NOT seed that shared slot with its own
    transcript — otherwise a later same-write-zone implement follow-on would
    resume the companion session instead of the original implement chain. Such
    callers pass ``commit_session=False``: the invoke still runs fresh and is
    fully traced, but the shared stored session (the original same-zone seed)
    is left untouched.
    """
    from pipeline.prompts.delta import select_parts_for_turn
    from pipeline.prompts.session import (
        PromptSessionState,
        with_provider_session_id,
    )

    if split is None:
        split = _resolve_session_split_for_step(state)

    # Derive the source envelope from the turn. Cache/session selection
    # always uses the source (full) turn's envelope.
    source_envelope = turn.envelope()

    # Compute the physical session key via the shared helper so the
    # resume probe (``_should_continue_prompt_session``) and this
    # render path can never key the same call differently. The helper
    # anchors runtime on the agent class and model on ``agent.model``,
    # enforces the ADR 0027 / M11 PER_ROLE role requirement, and keeps
    # state isolated per ``run_id``. ``runtime_id`` / ``model_key`` are
    # also surfaced verbatim in the trace metadata below (populated even
    # for STATELESS, where ``key`` is ``None``), so keep them local.
    runtime_id = f"{type(agent).__module__}.{type(agent).__qualname__}"
    model_key = str(getattr(agent, "model", "") or "")
    key = _compute_session_key(state, agent, phase=phase, split=split)
    session_state = state.prompt_sessions.get(key) if key is not None else None
    # M7 owns state creation (M5/M6 contract): when a reusable key
    # exists but no PromptSessionState has been recorded yet, hand
    # the M6 selector a fresh state with ``session_id=None``. The
    # selector then takes its "fresh physical session" branch and
    # returns a candidate ``state_update`` that seeds
    # ``sent_part_keys`` for round 2.
    if key is not None and session_state is None:
        session_state = PromptSessionState(key=key)
    elif key is not None and not continue_session:
        # Delta rendering is only valid when this invocation continues the
        # same physical provider session. A stored prompt-session state may
        # exist from an earlier call under the same key, but if the caller is
        # deliberately starting fresh then omitted stable parts would not
        # actually be present in the provider context. Select against a fresh
        # state and let the successful invoke overwrite the stored state with
        # the new provider session id.
        session_state = PromptSessionState(key=key)

    # Cross-phase physical continuation. Pipeline phases bind distinct
    # agent instances (PhaseAgentConfig slots), so a COMMON / shared
    # per_role key seeded by an earlier phase lives in
    # ``state.prompt_sessions`` but not on *this* agent. When the caller
    # opts to continue, the M6 delta selector assumes the provider
    # session is ``session_state.session_id``; the runtime, however,
    # resumes whatever ``agent.session_id`` currently holds. We must
    # reconcile the two or the wire omits parts from one session while
    # the provider resumes another. Align the agent to the stored id
    # whenever they differ — covers both the empty case (a fresh phase
    # agent) and the stale case (an agent still pointing at a prior
    # session). Same primitive as ``apply_followup_session_seeds``;
    # equal ids (within-phase / CHAIN same-instance resume) are left
    # untouched.
    if (
        continue_session
        and session_state is not None
        and session_state.session_id
        and getattr(agent, "session_id", None) != session_state.session_id
    ):
        agent.session_id = session_state.session_id
        agent._followup_resume_pending = True

    def _select_effective_turn(current_session_state: PromptSessionState):
        current_result = select_parts_for_turn(
            source_envelope, current_session_state, split,
            delta_droppable_part_ids=frozenset(delta_droppable_part_ids),
        )
        current_render_mode = current_result.render_mode
        current_turn = (
            turn.render_selected(current_result.selected_parts)
            if current_render_mode == "delta"
            else turn
        )
        return current_result, current_turn

    result, effective_turn = _select_effective_turn(session_state)
    render_mode = result.render_mode
    selected_part_keys = result.selected_part_keys
    omitted_part_keys = result.omitted_part_keys
    delta_dropped_part_keys = result.delta_dropped_part_keys
    prefix_hash = source_envelope.prefix_hash
    payload_hash = source_envelope.payload_hash
    state_update = result.state_update

    # Publish the effective turn so the runtime adapter's debug renderer
    # sees what actually goes on the wire (M7 trace-honesty rule).
    from core.observability.prompt_trace import set_last_prompt_turn as _set_turn
    _set_turn(effective_turn)

    # Invoke. Exceptions skip state commit and trace metadata write —
    # M7 commit-on-success rule, except for one sanctioned recovery:
    # follow-up/checkpoint session ids live in provider-owned local state.
    # If the provider says that stored id no longer exists, burn only the
    # stale bridge, recompute a full prompt, and continue in a fresh
    # provider session while preserving Orcho's persisted parent context.
    # M14.4.4 — capture wall-clock duration so the live card can
    # show it. We measure the user-perceived turn-around (includes
    # retries inside the runtime), not the API-side ``duration_ms``
    # which only counts a single underlying call.
    import time as _time
    _invoke_started = _time.monotonic()
    try:
        raw = agent.invoke(
            effective_turn.text, cwd,
            continue_session=continue_session,
            attachments=attachments,
            mutates_artifacts=mutates_artifacts,
        )
    except Exception as exc:
        if (
            not continue_session
            or key is None
            or not _is_missing_provider_session_error(exc)
        ):
            raise
        stale_session_id = getattr(agent, "session_id", None)
        _reset_agent_session(agent)
        state.prompt_sessions.pop(key, None)
        session_state = PromptSessionState(key=key)
        result, effective_turn = _select_effective_turn(session_state)
        render_mode = result.render_mode
        selected_part_keys = result.selected_part_keys
        omitted_part_keys = result.omitted_part_keys
        delta_dropped_part_keys = result.delta_dropped_part_keys
        state_update = result.state_update
        continue_session = False
        _set_turn(effective_turn)
        raw = agent.invoke(
            effective_turn.text, cwd,
            continue_session=False,
            attachments=attachments,
            mutates_artifacts=mutates_artifacts,
        )
        # The fresh-session retry succeeded (a failed retry would have
        # propagated above). Surface the recovery as a first-class
        # warning + structural event rather than letting the swallowed
        # missing-session exception look like a phase failure.
        _notify_provider_session_fallback(
            state, phase=phase, stale_session_id=stale_session_id,
        )
    _invoke_duration_s = _time.monotonic() - _invoke_started

    # Successful invoke: commit state_update with the provider's
    # current session_id (may still be None for runtimes without
    # resumable sessions; that just keeps the next call on full
    # render until a session id appears).
    #
    # ADR 0113 (F1): a fresh new-zone/companion invoke passes
    # ``commit_session=False`` so its transcript never overwrites the shared
    # implement session slot (or the E1 checkpoint row). Skipping the commit
    # leaves the original same-zone seed intact, so a later same-write-zone
    # implement follow-on resumes the right chain rather than the companion's.
    if commit_session and state_update is not None and key is not None:
        provider_session_id = getattr(agent, "session_id", None)
        committed = with_provider_session_id(state_update, provider_session_id)
        state.prompt_sessions[key] = committed

    # E1: persist ``agent.session_id`` to the run's checkpoint so the
    # next subprocess after ``orcho_run_resume`` (or any handoff pause)
    # can rehydrate it and pass ``--resume <sid>`` on the very first
    # invoke. Without this, every subprocess restart silently starts a
    # fresh provider conversation and ``continue_session=True`` no-ops.
    #
    # Identity-keyed reverse-lookup against ``state.phase_config`` so
    # CHAIN-mode dispatch (where ``phase="repair_changes"`` invokes
    # ``implement_agent``) saves under the *real* role_attr instead of
    # the phase label.
    _ckpt = state.extras.get("_ckpt") if state.extras else None
    if commit_session and _ckpt is not None:
        _role_attr = _agent_to_role_attr(state, agent)
        if _role_attr is not None:
            # E1 contract: always sync checkpoint with the post-invoke
            # truth. Non-None sid → INSERT OR REPLACE; ``None`` sid →
            # DELETE the row. Otherwise a runtime that burned its
            # session (follow-up burn, Claude session reset, any path
            # that sets ``agent.session_id = None``) would leave a
            # stale checkpoint row that the next subprocess's
            # rehydrate path would reuse — pointing the runtime at a
            # session the provider no longer recognises.
            _sid_after_invoke = getattr(agent, "session_id", None)
            # Checkpoint write failure must not break the in-flight
            # invocation. The session continues working in-memory;
            # next resume will fall back to fresh session.
            import contextlib as _ctx
            with _ctx.suppress(Exception):
                _ckpt.set_agent_session(_role_attr, _sid_after_invoke)

    # Trace metadata for M12. Stored under
    # ``state.phase_log[trace_phase or phase]["prompt_render"]``;
    # phase handlers that overwrite ``state.phase_log[phase]``
    # must preserve the ``prompt_render`` sub-key (the validate_plan
    # handler does). CHAIN repair_changes passes
    # ``trace_phase="repair_changes"`` to attribute the trace to
    # the repair handler's slot while reusing the implement
    # session key.
    # ADR 0027 / M11: ``session_split`` surfaces the resolved
    # prompt-session policy explicitly. STATELESS produces
    # ``session_key=None``, so the policy is otherwise invisible
    # from the key alone — recording the split value here makes
    # the chosen mode obvious to M12 trace persistence.
    # M12 closing: ``part_ids`` captures the full ordered set of
    # envelope parts (the SOURCE prompt) so consumers can audit "what
    # could have been sent" against "what actually went on the wire"
    # without re-running the selector. Completeness invariant:
    # ``part_ids ⊇ selected_part_keys ∪ omitted_part_keys ∪
    # delta_dropped_part_keys`` — every wire / cached-omit / history-drop
    # key traces back to a source part. Uses the ``part_session_key``
    # format (``"{id}@{version or 0}"``) so the value is comparable
    # across all four lists. ``provider_session_id`` mirrors the
    # runtime's session id at invoke time — null on stateless
    # runtimes; populated on Claude / Codex sessions that survive
    # the call.
    from pipeline.prompts.session import part_session_key as _part_key
    part_ids: tuple[str, ...] = tuple(_part_key(p) for p in source_envelope.parts)
    provider_session_id_value = getattr(agent, "session_id", None)
    trace_slot = trace_phase if trace_phase is not None else phase
    # Resolve the round counter relevant to this invocation. Mirrors
    # ``lifecycle._resolve_round_n``: prefer the typed pointer set by
    # ``runtime._run_loop_step`` (``_active_loop_round_key``) and read
    # the counter through it; fall back to the phase-name convention
    # so direct-handler tests and isolation paths still get a value.
    _round_n_val: int | None = None
    _active_round_key = state.extras.get("_active_loop_round_key")
    if isinstance(_active_round_key, str) and _active_round_key:
        _v = state.extras.get(_active_round_key)
        if _v is not None:
            _round_n_val = int(_v)
    if _round_n_val is None:
        _by_phase = {
            "plan":           "plan_round",
            "validate_plan":  "plan_round",
            "review_changes": "repair_round",
            "repair_changes": "repair_round",
        }
        _fallback_key = _by_phase.get(trace_slot, "loop_round")
        _v = state.extras.get(_fallback_key)
        if _v is not None:
            _round_n_val = int(_v)
    phase_log_entry = state.phase_log.setdefault(trace_slot, {})
    trace = _TraceInputs(
        trace_slot=trace_slot,
        phase_key=phase,
        round_n=_round_n_val,
        loop_round=state.extras.get("loop_round"),
        render_mode=render_mode,
        session_split=split.value,
        session_key=_physical_session_key_to_dict(key),
        provider_session_id=provider_session_id_value,
        prefix_hash=prefix_hash,
        payload_hash=payload_hash,
        wire_chars=len(effective_turn.text),
    )
    stamp_prompt_render(
        phase_log_entry,
        trace,
        part_ids=part_ids,
        selected=selected_part_keys,
        omitted=omitted_part_keys,
        delta_dropped=delta_dropped_part_keys,
        continue_session=continue_session,
    )
    stamp_context_growth(phase_log_entry, trace, agent=agent)
    stamp_context_clearing(phase_log_entry, trace, source_envelope=source_envelope)
    pressure = stamp_context_pressure(
        phase_log_entry,
        trace,
        agent=agent,
        runtime_id=runtime_id,
        model_key=model_key,
    )
    stamp_runtime_compaction(phase_log_entry, trace, agent=agent)
    from pipeline.observability.invocation_outcome import build_invocation_outcome
    outcome = build_invocation_outcome(
        agent=agent,
        runtime_id=runtime_id,
        model=model_key,
        wire_text=effective_turn.text,
    )
    agent.last_invocation_outcome = outcome
    render_live_card(
        agent=agent,
        outcome=outcome,
        pressure=pressure,
        duration_s=_invoke_duration_s,
        trace_slot=trace_slot,
        loop_round=state.extras.get("loop_round"),
    )
    return raw
