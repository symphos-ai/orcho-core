# SPDX-License-Identifier: Apache-2.0
"""``repair_changes`` phase handler — repair_changes (fix) phase handler.

Imports helpers from their real homes (never from the package
facade) so there is no import cycle through the builtin __init__.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pipeline.phases import adapters
from pipeline.phases.builtin.lifecycle import (
    _agent_project_dir,
    _carry_trace_metadata,
    _change_handoff_for,
    _ensure_lifecycle_ctx,
    _guardrail_blocked,
    _handoff_contract_for,
    _prompt_from_active_step,
)
from pipeline.phases.builtin.plan_artifact import (
    _plan_contract_for,
    _plan_tasks_for,
)
from pipeline.phases.builtin.prompt_parts import (
    _multimodal_attachments,
    _verification_contract_part,
)
from pipeline.phases.builtin.registry import _require_agent
from pipeline.phases.builtin.review_support import (
    _capture_phase_baseline,
    _print_implement_summary,
    _resolve_fix_runtime_config,
    _store_repair_receipt,
)
from pipeline.phases.builtin.session_invoke import _session_aware_invoke
from pipeline.phases.builtin.session_keys import (
    _runtime_session_meta,
    decide_session_continuation,
)
from pipeline.runtime.roles import SessionInvocationRole

if TYPE_CHECKING:
    from pipeline.runtime import PipelineState

def _phase_repair_changes(state: PipelineState) -> PipelineState:
    """Wrap ``adapters.run_fix``. Clears ``last_critique`` once consumed.

    Phase 5c step 3: handler stuffs ``state.phase_log["rounds_pending"]``
    so v2 dispatch's RoundAdapter (registered under "repair_changes")
    writes the per-round session entry from ``_on_phase_end``.

    Phase 5c step 4: handler resolves per-round agent escalation +
    session_mode (CHAIN / HYBRID / STATELESS) when v2 dispatch is
    active. Legacy ``run_review_fix_loop`` still drives escalation
    imperatively — Phase 5d unifies both paths.
    """
    import dataclasses

    from agents.protocols import SessionMode

    # Phase 5c step 4 + 6b: handler-side escalation runs unconditionally.
    # The legacy ``_v2_dispatch_active`` guard distinguished v2 dispatch
    # from the deleted ``run_review_fix_loop`` path; v1 is gone, so the
    # handler always owns escalation.
    cfg = _resolve_fix_runtime_config(state)
    # CHAIN repair reuses the implement provider session + worktree, so it is
    # a same-write-zone follow-on. Record only that same-write-zone posture as
    # a policy *input*; the continue/fresh decision itself comes from the
    # session-disposition policy below, not from this signal (ADR 0113).
    state.extras["_repair_same_write_zone"] = (
        cfg["effective_mode"] is SessionMode.CHAIN
    )
    state.extras["hybrid_codemap"] = (
        state.extras.get("codemap", "")
        if cfg["effective_mode"] is SessionMode.HYBRID
        else ""
    )
    # Swap phase_config.repair_changes_agent:
    #   * automatic round > 1 → repair_escalation_agent (more capable model);
    #   * round 1 OR human-directed retry in CHAIN mode → implement_agent,
    #     which carries the captured session_id so the round resumes the
    #     implementer thread;
    #   * otherwise the default repair_changes_agent stays.
    # A human-directed ``retry_feedback`` round is a continuation, not an
    # escalation, so it never swaps to the fresh escalation agent.
    pc = state.phase_config
    if pc is not None:
        human_directed = cfg.get("human_directed", False)
        is_continuation = cfg["repair_round"] == 1 or human_directed
        if (cfg["repair_round"] > 1 and not human_directed
                and getattr(pc, "repair_escalation_agent", None)):
            state.phase_config = dataclasses.replace(
                pc, repair_changes_agent=pc.repair_escalation_agent,
            )
        elif (is_continuation
              and cfg["effective_mode"] is SessionMode.CHAIN
              and getattr(pc, "implement_agent", None)):
            state.phase_config = dataclasses.replace(
                pc, repair_changes_agent=pc.implement_agent,
            )

    agent = _require_agent(state, "repair_changes_agent")
    critique_for_round = state.last_critique  # captured before fix consumes it

    # Phase 5e-5 substep 4 + 6b: read text/test-config helpers from ctx.
    # FSM always populates ``state.lifecycle_ctx``; legacy
    # fallback removed.
    ctx = _ensure_lifecycle_ctx(state)
    critique_is_empty = ctx.text_helpers.critique_is_empty
    _resolve_tests_config_local = ctx.test_config_resolver
    pending = state.phase_log.get("rounds_pending", {}) or {}
    # If review handler skipped on no-uncommitted, propagate the skip
    # marker so RoundAdapter omits the round entry (legacy parity).
    if pending.get("_skip_adapter"):
        state.phase_log["repair_changes"] = {"skipped": "review skipped (no uncommitted)"}
        return state
    if critique_is_empty(critique_for_round) and not state.dry_run:
        state.phase_log["repair_changes"] = {"skipped": "review clean"}
        state.phase_log["rounds_pending"] = {
            **pending,
            "critique": critique_for_round or "",
        }
        return state

    # Take the baseline snapshot only once we're committed to invoking
    # the runtime — the early-return guards above don't print a summary.
    repair_baseline = _capture_phase_baseline(state)

    # ADR 0113: the repair continuity decision is the session-disposition
    # policy keyed on the explicit ``repair`` role. The policy derives the
    # same-write-zone input from state (CHAIN posture set above): same-zone →
    # CONTINUE (resume the implement thread), non-same-zone (HYBRID/STATELESS)
    # → FRESH. No ad-hoc SessionMode/CHAIN continuity math here, and
    # ``state.extras['continue_session']`` is no longer a second source.
    continue_session = decide_session_continuation(
        state, role=SessionInvocationRole.REPAIR, phase="repair_changes",
    ).continue_session

    prompt_spec = _prompt_from_active_step(ctx)
    verification_part = _verification_contract_part(state, "repair_changes")
    if state.dry_run:
        result = adapters.run_fix(
            agent,
            state.task,
            critique_for_round,
            _agent_project_dir(state),
            state.plugin,
            test_failures=state.last_test_output,
            write_style=_resolve_tests_config_local(state.plugin).write_style,
            continue_session=continue_session,
            hybrid_codemap=state.extras.get("hybrid_codemap", "") or "",
            plan_contract=_plan_contract_for(state),
            plan_tasks=_plan_tasks_for(state),
            handoff_contract=_handoff_contract_for(state),
            change_handoff=_change_handoff_for(state),
            dry_run=state.dry_run,
            prompt_spec=prompt_spec,
            attachments=_multimodal_attachments(state),
            verification_part=verification_part,
        )
    else:
        # M9: route through the session-aware helper. CHAIN repair
        # reuses the implementer prompt-session key (phase="implement")
        # because the runtime bridge resumes the implement provider
        # session — the prompt-side "what was already sent" is the
        # implement state. Non-CHAIN repair (HYBRID / STATELESS / no
        # CHAIN) gets its own per_phase:repair_changes key.
        from pipeline.prompts import fix_prompt as _fix_prompt

        base_turn = _fix_prompt(
            state.task,
            critique_for_round,
            _agent_project_dir(state),
            state.plugin,
            test_failures=state.last_test_output,
            write_style=_resolve_tests_config_local(state.plugin).write_style,
            plan_contract=_plan_contract_for(state),
            plan_tasks=_plan_tasks_for(state),
            handoff_contract=_handoff_contract_for(state),
            change_handoff=_change_handoff_for(state),
            prompt_spec=prompt_spec,
            verification_part=verification_part,
        )
        hybrid_codemap = state.extras.get("hybrid_codemap", "") or ""
        # The hybrid codemap is spliced INTO the base prompt body via
        # ``inject_context`` (a mid-prompt position). We apply the
        # injection to the wire text and register the codemap part so
        # the M12 trace records that the codemap participated in this
        # render. No out-of-builder prefix (AGENTS.md injection
        # removed — ADR 0059).
        if hybrid_codemap:
            from core.context import inject_context as _inject_context
            from pipeline.prompts.turn import PromptSegment, PromptTurn
            from pipeline.prompts.types import (
                PromptCacheScope,
                PromptLayer,
                PromptPart,
                PromptStability,
            )
            _injected_text = _inject_context(base_turn.text, hybrid_codemap)
            # Build a turn whose wire text is the inject_context output.
            # The injected-text part carries the full injected body so
            # ``turn.text`` is byte-identical to the legacy path.
            # A zero-body codemap marker is appended as an envelope-only
            # shadow (empty ``text``) so the M12 trace records that the
            # codemap participated in this render without adding wire bytes.
            _injected_part = PromptPart(
                kind="inject_mid_text",
                name="injected_text",
                source="artifact",
                body=_injected_text,
                layer=PromptLayer.TURN,
                stability=PromptStability.TURN,
                cache_scope=PromptCacheScope.NONE,
                volatile_reason="hybrid codemap injected into prompt body",
                id="inject_mid_text:injected_text",
            )
            _codemap_marker = PromptPart(
                kind="codemap",
                name="repo_map",
                source="artifact",
                body="",  # zero-body: wire bytes already baked into injected_text
                layer=PromptLayer.TURN,
                stability=PromptStability.TURN,
                cache_scope=PromptCacheScope.NONE,
                volatile_reason="codemap injected mid-prompt via inject_context",
                id="codemap:repo_map:inject_mid",
            )
            turn = PromptTurn(segments=(
                PromptSegment(
                    text=_injected_text,
                    part=_injected_part,
                    segment_id="inject_mid:injected_text",
                ),
                PromptSegment(
                    text="",
                    part=_codemap_marker,
                    segment_id="inject_mid:codemap_marker",
                ),
            ))
        else:
            turn = base_turn
        # M9 routing: CHAIN -> share implement key; otherwise own
        # repair_changes key. The prompt-session key boundary is
        # independent of the runtime bridge mode (SessionMode), but
        # CHAIN is the only mode that semantically aligns with
        # reusing implement prompt parts (provider session is the
        # same), so prompt and runtime alignment lands together.
        # M11.5: trace_phase decouples session-key phase from the
        # phase_log slot the prompt_render metadata lands in. CHAIN
        # repair must still attribute its trace to "repair_changes"
        # so the handler's phase_log entry (built below) carries it
        # — otherwise the trace lands under "implement" and the
        # repair handler's overwrite cannot preserve it.
        repair_phase = "implement" if continue_session else "repair_changes"
        output = _session_aware_invoke(
            agent, state,
            phase=repair_phase,
            turn=turn,
            cwd=_agent_project_dir(state),
            continue_session=continue_session,
            mutates_artifacts=True,
            attachments=_multimodal_attachments(state),
            trace_phase="repair_changes",
        )
        result = adapters.PhaseResult(
            name="repair_changes",
            output=output,
            meta=_runtime_session_meta(
                agent, continue_session=continue_session,
            ),
        )
    # M9: preserve prompt_render across the phase_log overwrite below
    # (mirror of M7 validate_plan / M8 plan pattern). M14.1 extends
    # to carry ``context_growth`` alongside ``prompt_render`` via
    # _carry_trace_metadata.
    _repair_carried = _carry_trace_metadata(state, "repair_changes")
    state.phase_log["repair_changes"] = {
        "output": result.output,
        "meta": dict(result.meta),
        **_repair_carried,
    }
    if _guardrail_blocked(result.output):
        state.phase_log["repair_changes"]["guardrail_blocked"] = True
        state.stop("agent guardrail blocked destructive git command during repair_changes")
    # Phase 5d-fixup + 6b: stuff repair_model + session_mode + session_id
    # into rounds_pending so RoundAdapter writes the rich session-shape
    # entry the legacy ``run_review_fix_loop`` produced. ``cfg`` is now
    # always populated (substep 6b made handler-side escalation
    # unconditional). ``session_id`` comes from the agent's response
    # meta.
    repair_model_for_round = cfg.get("repair_model_for_round", "")
    eff_mode = cfg.get("effective_mode")
    session_mode_value = eff_mode.value if eff_mode is not None else ""
    repair_meta = result.meta or {}
    session_id = repair_meta.get("session_id")
    from pipeline.repair_protocol import build_repair_receipt

    repair_receipt = _store_repair_receipt(
        state,
        build_repair_receipt(
            source_phase="review_changes",
            source_round=cfg.get("repair_round"),
            repair_phase="repair_changes",
            repair_round=cfg.get("repair_round"),
            critique=critique_for_round,
            repair_output=result.output,
            operator_feedback=state.human_feedback,
            changed_refs=tuple(
                state.phase_log.get("repair_changes", {}).get("files", ()) or ()
            ),
        ),
    )

    rounds_pending: dict[str, Any] = {
        **pending,
        "critique":   critique_for_round,
        "repair_output": result.output,
        "repair_receipt": repair_receipt,
    }
    if repair_model_for_round:
        rounds_pending["repair_model"] = repair_model_for_round
    if session_mode_value:
        rounds_pending["session_mode"] = session_mode_value
    if cfg.get("session_mode_reason"):
        rounds_pending["session_mode_reason"] = cfg["session_mode_reason"]
    if cfg.get("session_mode_context_pressure"):
        rounds_pending["session_mode_context_pressure"] = (
            cfg["session_mode_context_pressure"]
        )
    if session_id is not None:
        rounds_pending["repair_session_id"] = session_id
        rounds_pending["session_id"] = session_id
    repair_continue_session = repair_meta.get("continue_session")
    if repair_continue_session is not None:
        rounds_pending["repair_continue_session"] = repair_continue_session
    followup_parent_repair_session_id = repair_meta.get("followup_parent_session_id")
    if followup_parent_repair_session_id is not None:
        rounds_pending["followup_parent_repair_session_id"] = (
            followup_parent_repair_session_id
        )
    state.phase_log["rounds_pending"] = rounds_pending
    state.last_critique = ""  # consumed
    # ADR 0076: durable verification-environment receipt written *after*
    # the runtime work, with real environment checks/commands — identical
    # condition to the implement handler. Under the run output dir, never
    # the source checkout; the writer creates no environment (no .venv in
    # ``git status``).
    if not state.dry_run:
        from pipeline.evidence.verification_receipt import (
            write_phase_verification_receipt,
        )
        write_phase_verification_receipt(
            output_dir=state.output_dir,
            phase="repair_changes",
            round=cfg.get("repair_round"),
            cwd=_agent_project_dir(state),
            contract=state.extras.get("verification_contract"),
            ctx=state.extras.get("verification_placeholders"),
        )
    _print_implement_summary(
        state, state.phase_log["repair_changes"], title="Repair changes",
        phase_name="repair_changes", baseline_ref=repair_baseline,
    )
    return state
