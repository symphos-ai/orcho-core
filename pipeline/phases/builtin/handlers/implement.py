# SPDX-License-Identifier: Apache-2.0
"""``implement`` phase handler — implement (build) phase handler.

Imports helpers from their real homes (never from the package
facade) so there is no import cycle through the builtin __init__.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline.phases import adapters
from pipeline.phases.builtin.lifecycle import (
    _agent_project_dir,
    _carry_trace_metadata,
    _change_handoff_for,
    _ensure_lifecycle_ctx,
    _guardrail_blocked,
    _handoff_contract_for,
    _implementation_execution_for,
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
)
from pipeline.phases.builtin.session_invoke import _session_aware_invoke
from pipeline.phases.builtin.session_keys import (
    _runtime_session_meta,
    _should_continue_prompt_session,
)
from pipeline.phases.builtin.subtask_dag import _run_subtask_dag_implement
from pipeline.runtime.roles import SessionInvocationRole

if TYPE_CHECKING:
    from pipeline.runtime import PipelineState

def _phase_implement(state: PipelineState) -> PipelineState:
    """Wrap ``adapters.run_build``. Records build output + session_id.

    Phase 5c step 1: handler also computes ``progress`` (k-of-N
    planned-file existence count) when ``state.parsed_plan`` carries a plan.
    Previously the orchestrator did this in ``run_build_phase`` before
    invoking BuildAdapter; moving it here means BuildAdapter sees the
    same data regardless of dispatch path.

    M9: live implement runs route through :func:`_session_aware_invoke`
    under ``phase="implement"`` so the M5 :class:`PhysicalSessionKey`
    is seeded for CHAIN repair to reuse on round 2. ``mutates_artifacts
    =True`` propagates the runtime write-flag (Edit/Write tools must
    stay enabled). Dry-run keeps the legacy ``adapters.run_build`` path
    unchanged.
    """
    agent = _require_agent(state, "implement_agent")
    ctx = _ensure_lifecycle_ctx(state)
    prompt_spec = _prompt_from_active_step(ctx)
    verification_part = _verification_contract_part(state, "implement")
    implement_baseline = _capture_phase_baseline(state)
    implementation_execution = _implementation_execution_for(state)
    if implementation_execution == "subtask_dag":
        state.phase_log["implement"] = _run_subtask_dag_implement(
            state, agent, implement_baseline,
        )
        _write_implement_verification_receipt(state)
        return state
    if state.dry_run:
        result = adapters.run_build(
            agent,
            state.task,
            _agent_project_dir(state),
            state.plugin,
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
        # M9: replace adapters.run_build's invoke step with the M7
        # session-aware helper so implement seeds prompt-session
        # state under phase="implement". CHAIN repair_changes will
        # later resume that key. The prompt assembly mirrors
        # adapters.run_build exactly so the wire prompt is byte-
        # identical to the legacy path on a single call.
        from pipeline.prompts import build_prompt as _build_prompt

        # A non-empty ``last_critique`` on entry to implement means the
        # last finding-gate verdict (validate_plan) is REJECTED-unresolved
        # and was bypassed without a replan loop: validate_plan writes ""
        # on APPROVED, and review/final gates run after implement. Forward
        # it as advisory reviewer critique so the developer addresses the
        # findings while implementing instead of replanning. Generic
        # trigger — no hardcoded validate_plan->implement coupling and no
        # task-name check; profiles without validate_plan keep "" and add
        # no part.
        advisory_critique = (state.last_critique or "").strip()
        turn = _build_prompt(
            state.task,
            _agent_project_dir(state),
            state.plugin,
            plan_contract=_plan_contract_for(state),
            plan_tasks=_plan_tasks_for(state),
            handoff_contract=_handoff_contract_for(state),
            change_handoff=_change_handoff_for(state),
            advisory_critique=advisory_critique,
            prompt_spec=prompt_spec,
            verification_part=verification_part,
        )
        # No out-of-builder prefix for implement (AGENTS.md injection
        # removed — ADR 0059; TEXT attachments are plan-only).
        # ADR 0113: implement is edit-shaped → the policy continues only a
        # same-write-zone follow-on (a same-run predecessor seeded the
        # implement session); otherwise FRESH. Compute the disposition once so
        # the invoke and the reflected session meta agree on the policy value.
        implement_continue_session = _should_continue_prompt_session(
            state, agent, phase="implement",
            role=SessionInvocationRole.IMPLEMENT,
        )
        output = _session_aware_invoke(
            agent, state,
            phase="implement",
            turn=turn,
            cwd=_agent_project_dir(state),
            mutates_artifacts=True,
            attachments=_multimodal_attachments(state),
            continue_session=implement_continue_session,
        )
        result = adapters.PhaseResult(
            name="implement",
            output=output,
            meta=_runtime_session_meta(
                agent, continue_session=implement_continue_session,
            ),
        )
    # M9 / M14.1: ``_session_aware_invoke`` stamped trace metadata
    # under ``state.phase_log["implement"]`` before this handler
    # rebuilt the entry. ``_carry_trace_metadata`` preserves both
    # ``prompt_render`` (M12) and ``context_growth`` (M14.1).
    _implement_carried = _carry_trace_metadata(state, "implement")
    entry = {"output": result.output, "meta": dict(result.meta)}
    entry.update(_implement_carried)
    if implement_baseline:
        entry["change_baseline_ref"] = implement_baseline
    if _guardrail_blocked(result.output):
        entry["guardrail_blocked"] = True
        state.stop("agent guardrail blocked destructive git command during implement")

    # Phase 5c step 1: post-build progress computation. Skip on dry-run
    # (parsed_plan empty). When parsed_plan carries file_paths, count
    # how many actually exist on disk now (build may have created new
    # ones) → BuildAdapter promotes this into session shape.
    parsed_plan = state.parsed_plan
    if not state.dry_run and parsed_plan and parsed_plan.file_paths:
        # Phase 5e-5 substep 4 + 6b: ctx is always populated by FSM.
        ctx = _ensure_lifecycle_ctx(state)
        post_existing, _ = ctx.plan_helpers.validate_paths(
            parsed_plan, _agent_project_dir(state),
        )
        entry["progress"] = {
            "kind":      "planned_files",
            "completed": len(post_existing),
            "total":     len(parsed_plan.file_paths),
        }

    state.phase_log["implement"] = entry
    _write_implement_verification_receipt(state)
    if not state.dry_run:
        _print_implement_summary(
            state, entry, title="Implementation",
            phase_name="implement", baseline_ref=implement_baseline,
        )
    return state


def _write_implement_verification_receipt(state: PipelineState) -> None:
    """ADR 0076: persist the implement phase's verification receipt.

    Written *after* the runtime work, with real environment checks /
    commands (never empty), under the run output dir — never the source
    checkout. Skipped on dry-run (no real environment to verify). The
    writer creates no environment, so no ``.venv`` leaks into the checkout.
    """
    if state.dry_run:
        return
    from pipeline.evidence.verification_receipt import (
        write_phase_verification_receipt,
    )
    write_phase_verification_receipt(
        output_dir=state.output_dir,
        phase="implement",
        round=state.extras.get("loop_round") or 1,
        cwd=_agent_project_dir(state),
        contract=state.extras.get("verification_contract"),
        ctx=state.extras.get("verification_placeholders"),
    )
