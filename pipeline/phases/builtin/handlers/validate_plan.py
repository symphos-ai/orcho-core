# SPDX-License-Identifier: Apache-2.0
"""``validate_plan`` phase handler — validate_plan reviewer phase handler.

Imports helpers from their real homes (never from the package
facade) so there is no import cycle through the builtin __init__.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.contracts.review_schema import ReviewSchemaError
from core.io.stdout_render import defer_assistant_json
from core.io.transcript import render_parse_failure as _render_parse_failure
from pipeline.phases.builtin.lifecycle import (
    _agent_project_dir,
    _carry_trace_metadata,
    _ensure_lifecycle_ctx,
    _prompt_from_active_step,
)
from pipeline.phases.builtin.plan_artifact import (
    _approved_review_json,
    _plan_contract_for,
    _review_plan_artifact,
)
from pipeline.phases.builtin.prompt_parts import (
    _multimodal_attachments,
    _verification_contract_part,
)
from pipeline.phases.builtin.registry import _require_agent
from pipeline.phases.builtin.review_support import _print_review_preview
from pipeline.phases.builtin.session_keys import (
    _runtime_session_meta,
    decide_session_continuation,
)
from pipeline.phases.review_contract_recovery import retry_review_contract_once
from pipeline.review_markdown import render_review_markdown
from pipeline.review_parser import (
    ReviewParseError,
    parse_review,
)
from pipeline.runtime.roles import SessionInvocationRole

if TYPE_CHECKING:
    from pipeline.runtime import PipelineState

def _phase_validate_plan(state: PipelineState) -> PipelineState:
    """validate_plan reviewer: validate the just-produced plan markdown.

    Prefers the file-targeted prompt builder when a plan artefact path is
    available; otherwise falls back to the diff-targeted prompt. Sets
    ``state.last_critique`` to the body of the verdict and halts when
    REJECTED with the gate flag on.
    """
    agent = _require_agent(state, "validate_plan_agent")

    from pipeline.prompts import plan_review_focus

    ctx = _ensure_lifecycle_ctx(state)
    prompt_spec = _prompt_from_active_step(ctx)
    cwd = _agent_project_dir(state)
    plan_contract = _plan_contract_for(state)
    focus = plan_review_focus(
        state.task, state.plugin, cwd,
        plan_contract=plan_contract,  # REA-1
        prompt_spec=prompt_spec,
        verification_part=_verification_contract_part(state, "validate_plan"),
    )
    plan_round = int(
        state.extras.get("plan_round")
        or state.extras.get("loop_round")
        or 1
    )
    # ADR 0113: validate_plan is non-edit-shaped → the policy resolves it FRESH.
    # Compute the disposition once so the invoke and every reflected session
    # meta below agree on the policy value (never re-derived from
    # ``agent._last_resumed_session_id``).
    validate_plan_continue = decide_session_continuation(
        state,
        role=SessionInvocationRole.VALIDATE_PLAN,
        phase="validate_plan",
        round_key="plan_round",
    ).continue_session
    if state.dry_run:
        raw = _approved_review_json(
            "validate_plan dry run skipped reviewer invocation."
        )
    else:
        # The reviewer's primary output is a typed JSON contract. Suppress
        # the raw JSON from the live transcript (mirroring the plan phase)
        # so the operator sees the one-line "Contracted answer prepared."
        # marker instead of streamed machine output; the full structured
        # detail is rendered deterministically below via
        # _print_review_preview from the parsed contract.
        with defer_assistant_json():
            # ADR 0113: validate_plan is non-edit-shaped → FRESH. The plan +
            # critique handoff (repair receipt + current review subject) is
            # assembled inside ``_review_plan_artifact`` on plan_round >= 2,
            # independent of session continuation, so the fresh reviewer keeps
            # its prior critique context without resuming.
            raw = _review_plan_artifact(
                agent, state, focus, cwd,
                prompt_spec=prompt_spec,
                continue_session=validate_plan_continue,
            )
    plan_artifact = state.extras.get("plan_artifact_path", "") or ""

    # M7: _session_aware_invoke stashed prompt_render trace metadata
    # under state.phase_log["validate_plan"] before the parser ran.
    # Both the success and parse-error paths overwrite that dict
    # below; _carry_trace_metadata captures M12 prompt_render +
    # M14.1 context_growth so they survive the rebuild.
    _validate_plan_carried = _carry_trace_metadata(state, "validate_plan")
    validate_plan_session_meta = _runtime_session_meta(
        agent, continue_session=validate_plan_continue,
    )
    contract_repair: dict[str, Any] | None = None
    try:
        parsed = parse_review(raw)
    except (ReviewSchemaError, ReviewParseError) as e:
        original_raw = raw
        retry_raw = ""
        try:
            contract_result = retry_review_contract_once(
                agent,
                phase="validate_plan",
                cwd=cwd,
                raw_output=original_raw,
                parse_error=e,
                attachments=_multimodal_attachments(state),
            )
            retry_raw = contract_result.raw_output
            parsed = parse_review(retry_raw)
            raw = retry_raw
            contract_repair = {
                **contract_result.repair_meta,
                "session_meta": _runtime_session_meta(
                    agent, continue_session=validate_plan_continue,
                ),
            }
        except (ReviewSchemaError, ReviewParseError) as retry_error:
            raw_for_failure = retry_raw or original_raw
            repair_meta = {
                "triggered": True,
                "original_parse_error": str(e),
                "original_raw_output": original_raw,
                "failed": True,
                "session_meta": _runtime_session_meta(
                    agent, continue_session=validate_plan_continue,
                ),
            }
            if retry_raw:
                repair_meta["retry_raw_output"] = retry_raw
            body = (
                f"validate_plan parse error: {retry_error}\n\n"
                f"Raw output:\n{raw_for_failure}"
            )
            state.last_critique = body
            state.phase_log["validate_plan"] = {
                "output":           body,
                "raw_output":       raw_for_failure,
                "approved":         False,
                "verdict":          "REJECTED",
                "parse_error":      str(retry_error),
                "contract_repair":  repair_meta,
                "attempt":          plan_round,
                "plan_file":        plan_artifact,
                "critique":         body,
                **validate_plan_session_meta,
                **_validate_plan_carried,
            }
            print(_render_parse_failure(
                title="validate_plan",
                error=str(retry_error),
                raw_output=raw_for_failure,
            ))
            from core.observability import events as _events
            _events.emit(
                "validate_plan.verdict",
                attempt=plan_round,
                approved=False,
                critique=body,
            )
            state.stop(
                "validate_plan contract rejected before implement: "
                f"{retry_error}"
            )
            return state
    approved = parsed.approved
    body = render_review_markdown(parsed)

    state.last_critique = "" if approved else body
    # Phase 7.10: surface the bridge edge in the saved log so the
    # evidence consumer (UI, MCP, decision-provenance graph) can draw
    # round-2-resumes-round-1 without parsing stdout. ``session_id`` is
    # captured AFTER the call (post invoke), ``continue_session`` is
    # the policy decision the handler made.
    entry = {
        "output":           body,
        "raw_output":       raw,
        "approved":         approved,
        "verdict":          parsed.verdict,
        "short_summary":    parsed.short_summary,
        "findings":         parsed.findings_as_dicts(),
        "parse_warnings":   list(parsed.parse_warnings),
        "attempt":          plan_round,
        "plan_file":        plan_artifact,
        "critique":         body,
        **validate_plan_session_meta,
        **_validate_plan_carried,
    }
    if contract_repair is not None:
        entry["contract_repair"] = contract_repair
    state.phase_log["validate_plan"] = entry
    _print_review_preview(state, "validate_plan", "Plan validation")
    # Phase 5d-fixup + 6b: emit ``validate_plan.verdict`` event unconditionally.
    # The legacy ``_v2_dispatch_active`` guard distinguished v2 dispatch
    # from the deleted v1 path; v1 is gone in 5d-5, so all dispatches
    # are v2 — always emit.
    from core.observability import events as _events
    _events.emit(
        "validate_plan.verdict",
        attempt=plan_round,
        approved=approved,
        critique=body,
    )

    # Phase 3 cutover: handler-side gate-blocked halt was removed.
    # Pause semantics now live in the loop runner — a non-bypass
    # ``handoff`` policy on validate_plan triggers
    # ``PhaseHandoffRequested`` after the inner step dispatches, and
    # the project orchestrator's ``_apply_phase_handoff_pause`` writes
    # ``meta.phase_handoff`` + ``awaiting_phase_handoff`` status. The
    # handler only records verdict/critique here; the runner gates.
    return state
