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
from pipeline.criterion_gate_refs import (
    official_gate_identities,
    plan_gate_ref_problems,
    render_gate_ref_rejection,
)
from pipeline.phases.builtin.lifecycle import (
    _agent_project_dir,
    _carry_trace_metadata,
    _ensure_lifecycle_ctx,
    _prompt_from_active_step,
)
from pipeline.phases.builtin.plan_artifact import (
    PLAN_CONTRACT_REJECTION_KEY,
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
from pipeline.runtime.roles import PhaseHandoffType, SessionInvocationRole
from pipeline.verification_ownership import (
    find_verification_ownership_conflicts,
    render_verification_ownership_rejection,
)

if TYPE_CHECKING:
    from pipeline.runtime import PipelineState


def _rejection_requires_stop(
    state: PipelineState,
    plan_round: int,
) -> bool:
    """Fail closed when no replan or operator-decision path remains.

    Shared by every engine-synthesized rejection (plan-contract violation,
    verification-ownership conflict, unresolvable gate ref): while rounds
    remain the rejection is critique for the next plan round; on the final
    round a pausing handoff policy hands the decision to an operator; with
    neither left, the run must stop rather than fall through to implement.
    """
    max_rounds = int(state.extras.get("plan_round_max") or 0)
    if max_rounds <= 0 or plan_round < max_rounds:
        return False
    active_step = getattr(state.lifecycle_ctx, "active_step", None)
    policy = getattr(active_step, "handoff", None)
    return (
        policy is None
        or policy.type is PhaseHandoffType.HUMAN_BYPASS
    )


def _declared_gate_identities(state: PipelineState) -> frozenset:
    """The ledger's declared identities, or empty when it cannot be read.

    Only feeds the rejection's ``required_fix`` catalogue. The problem list
    already states an unreadable ledger, so a second failure here must not
    replace that message with a traceback.
    """
    run_dir = getattr(state, "output_dir", None)
    if run_dir is None:
        return frozenset()
    try:
        return official_gate_identities(run_dir).declared
    except Exception:  # noqa: BLE001 — catalogue is best-effort context
        return frozenset()


def _render_plan_contract_rejection(rejection: dict[str, Any]) -> str:
    """Render a valid validate-plan review for a plan-contract violation.

    The plan handler recorded the violation instead of halting; this turns it
    into the verdict shape the loop already understands, without a reviewer
    call, so the planner receives the exact error as critique next round.
    """
    import json

    error = str(rejection.get("error", "")).strip()
    kind = str(rejection.get("kind", "PlanContractError"))
    return json.dumps({
        "verdict": "REJECTED",
        "short_summary": "The plan output does not satisfy the plan contract.",
        "findings": [{
            "id": "plan-contract",
            "severity": "P1",
            "title": f"Plan rejected by the engine: {kind}",
            "body": (
                f"{error}. The engine could not accept the plan as written; "
                "nothing was reviewed for substance."
            ),
            "required_fix": (
                "Re-emit the complete plan so it satisfies the plan contract. "
                "The error above names the exact violation; fix that and keep "
                "everything else unchanged."
            ),
        }],
        "risks": [],
        "checks": ["Parsed the plan output against the plan contract."],
    })


def _phase_validate_plan(state: PipelineState) -> PipelineState:
    """validate_plan reviewer: validate the just-produced plan markdown.

    Prefers the file-targeted prompt builder when a plan artefact path is
    available; otherwise falls back to the diff-targeted prompt. Sets
    ``state.last_critique`` to the body of the verdict and halts when
    REJECTED with the gate flag on.
    """
    agent = _require_agent(state, "validate_plan_agent")
    # A plan-contract violation recorded by the plan handler: the plan did not
    # parse, so there is no plan object to check for ownership or gate refs.
    # It is consumed here so a later, valid round is judged on its own.
    contract_rejection = state.extras.pop(PLAN_CONTRACT_REJECTION_KEY, None)
    if state.parsed_plan is not None:
        ownership_conflicts = find_verification_ownership_conflicts(
            state.parsed_plan,
            state.extras.get("verification_contract"),
            state.extras,
        )
        # ADR 0188 resolves every executable criterion's gate_refs before
        # implement. Detecting it here rather than in the plan handler keeps an
        # unresolvable ref a *rejection* the planner can fix on the next round,
        # instead of a halt that ends the run over a fixable naming mistake.
        gate_ref_problems = plan_gate_ref_problems(
            state.parsed_plan, getattr(state, "output_dir", None),
        )
    else:
        ownership_conflicts = ()
        gate_ref_problems = []
    engine_rejected = bool(contract_rejection or ownership_conflicts or gate_ref_problems)

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
    if contract_rejection:
        raw = _render_plan_contract_rejection(contract_rejection)
    elif ownership_conflicts:
        raw = render_verification_ownership_rejection(ownership_conflicts)
    elif gate_ref_problems:
        raw = render_gate_ref_rejection(
            gate_ref_problems, _declared_gate_identities(state),
        )
    elif state.dry_run:
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
    # A rejected-by-contract round stored no artifact; do not point the log at
    # a prior round's plan.
    plan_artifact = (
        "" if contract_rejection
        else state.extras.get("plan_artifact_path", "") or ""
    )

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
    if contract_rejection:
        entry["contract_conflict"] = "plan_contract"
        entry["plan_contract_rejection"] = dict(contract_rejection)
    elif gate_ref_problems:
        entry["contract_conflict"] = "criterion_gate_refs"
        entry["criterion_gate_ref_problems"] = list(gate_ref_problems)
    if ownership_conflicts:
        entry["contract_conflict"] = "verification_ownership"
        entry["verification_ownership_conflicts"] = [
            {
                "location": conflict.location,
                "plan_command": conflict.plan_command,
                "gate_command": conflict.gate_command,
                "hook": conflict.hook,
                "phase": conflict.phase,
            }
            for conflict in ownership_conflicts
        ]
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
    if engine_rejected and _rejection_requires_stop(state, plan_round):
        if contract_rejection:
            reason = f"plan rejected before implement: {contract_rejection.get('error', '')}"
        elif ownership_conflicts:
            reason = (
                "validate_plan verification ownership conflict rejected before "
                "implement"
            )
        else:
            reason = (
                "validate_plan rejected before implement: "
                + "; ".join(gate_ref_problems)
            )
        state.stop(reason)

    # Phase 3 cutover: handler-side gate-blocked halt was removed.
    # Pause semantics now live in the loop runner — a non-bypass
    # ``handoff`` policy on validate_plan triggers
    # ``PhaseHandoffRequested`` after the inner step dispatches, and
    # the project orchestrator's ``_apply_phase_handoff_pause`` writes
    # ``meta.phase_handoff`` + ``awaiting_phase_handoff`` status. The
    # handler only records verdict/critique here; the runner gates.
    return state
