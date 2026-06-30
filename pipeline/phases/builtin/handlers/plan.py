# SPDX-License-Identifier: Apache-2.0
"""``plan`` phase handler — PLAN / REPLAN phase handler.

Imports helpers from their real homes (never from the package
facade) so there is no import cycle through the builtin __init__.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.io.stdout_render import defer_assistant_json
from core.io.transcript import render_parse_failure as _render_parse_failure
from pipeline.phases import adapters
from pipeline.phases.builtin.lifecycle import (
    _agent_project_dir,
    _carry_trace_metadata,
    _change_handoff_for,
    _ensure_lifecycle_ctx,
    _prompt_from_active_step,
)
from pipeline.phases.builtin.plan_artifact import (
    _emit_plan_parsed_event,
    _finalize_replan_phase_log,
    _plan_prompt_prefix,
    _print_plan_preview,
    _render_and_store_plan_artifact,
)
from pipeline.phases.builtin.prompt_parts import (
    _codemap_part,
    _multimodal_attachments,
    _text_prefix_part,
    _verification_contract_part,
)
from pipeline.phases.builtin.registry import _require_agent
from pipeline.phases.builtin.review_support import _store_repair_receipt
from pipeline.phases.builtin.session_invoke import _session_aware_invoke
from pipeline.phases.builtin.session_keys import (
    _runtime_session_meta,
    decide_session_continuation,
)
from pipeline.runtime.roles import SessionInvocationRole

if TYPE_CHECKING:
    from pipeline.runtime import PipelineState


def _prior_plan_handoff_part(body: str) -> object:
    """Wrap the prior plan attempt as a replan handoff PromptPart.

    ADR 0113: replan is FRESH (no session resume), so the architect can no
    longer rely on its provider-session memory of the plan it just produced.
    The compact replan handoff therefore carries the prior plan markdown
    verbatim alongside the reviewer critique (rendered by ``replan_prompt``),
    so the fresh architect revises its own prior attempt instead of planning
    blind. Provenance is ``artifact`` — the body is a run-produced plan, not a
    code/template constant — and kept ``TURN``/``NONE`` (volatile, never
    cached) because it changes every round.
    """
    from pipeline.prompts.types import (
        PromptCacheScope,
        PromptLayer,
        PromptPart,
        PromptStability,
    )

    return PromptPart(
        kind="replan_prior_plan",
        name="prior_plan",
        source="artifact",
        body=body,
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="prior plan attempt varies every replan round",
        id="replan_prior_plan:prior_plan",
    )

def _phase_plan(state: PipelineState) -> PipelineState:
    """Wrap ``adapters.run_plan``. Captures rendered plan markdown into state.

    Round 1 (default) uses ``plan_prompt``; round 2+ uses ``replan_prompt``
    fed by the previous round's QA critique. Orchestrator drives the loop
    and signals replan via ``state.extras["plan_round"]``.

    First-round runs may inject a validated hypothesis via
    ``state.extras["validated_hypothesis"]`` — appended as ``prompt_suffix``.
    """
    agent = _require_agent(state, "plan_agent")
    ctx = _ensure_lifecycle_ctx(state)
    prompt_spec = _prompt_from_active_step(ctx)
    # Phase 5c step 3: handler reads ``plan_round`` (legacy orchestrator
    # name + v2 profile's round_extras_key="plan_round") with fallback
    # to v2's default ``loop_round`` so a custom v2 profile that uses
    # the LoopStep default still drives replan correctly.
    plan_round = int(
        state.extras.get("plan_round")
        or state.extras.get("loop_round")
        or 1
    )

    if plan_round >= 2 and (state.last_critique or state.human_feedback):
        # Replan path. Build prompt directly via prompts.replan_prompt and
        # send it through whichever raw-prompt entry point the agent exposes.
        # Reviewer critique and operator feedback are distinct channels;
        # either alone is enough to fire the replan branch.
        if state.dry_run:
            from pipeline.plan_parser import ParsedPlan
            parsed_plan = ParsedPlan(
                short_summary="Dry run plan.",
                planning_context="Dry run skipped planner execution.",
                subtasks=(),
                source="dry_run",
            )
            state.parsed_plan = parsed_plan
            _finalize_replan_phase_log(state, base={
                "output": "[DRY RUN]",
                "meta": {"replan": True},
                "parsed_file_paths": [],
                "existing_files": [],
                "missing_files": [],
                "total_atomic_tasks": 0,
                "attempt": plan_round,
                "codemap_injected": False,
                "hypothesis_injected": False,
                "hypothesis_feedback_injected": False,
            })
            state.plan_markdown = "[DRY RUN]"
            return state
        from pipeline.prompts import replan_prompt
        # ADR 0009 Phase 4: REPLAN is not its own PhaseStep — it reuses
        # the plan step's prompt_spec. The active spec carries
        # ``task="plan"`` (or whatever the profile shipped); the handler
        # swaps to ``task="replan"`` so ``replan_prompt`` renders
        # ``tasks/replan`` instead of ``tasks/plan``. Other spec fields
        # (role, format) are preserved.
        if prompt_spec is not None:
            from dataclasses import replace as _dc_replace
            replan_spec = _dc_replace(prompt_spec, task="replan")
        else:
            replan_spec = None  # builder default kicks in
        replan_turn = replan_prompt(
            state.task,
            state.last_critique,
            state.human_feedback,
            _agent_project_dir(state),
            state.plugin,
            change_handoff=_change_handoff_for(state),
            prompt_spec=replan_spec,
        )
        # Phase 4.5 + 7b: project context must persist across replan rounds.
        # The architect needs the same AGENTS.md rules / TEXT attachments on
        # retry as on round 1; otherwise a rejected plan silently loses its
        # project grounding.
        prompt_prefix = _plan_prompt_prefix(state)
        from pipeline.prompts.turn import PromptTurnEditor as _PromptTurnEditor
        _replan_editor = _PromptTurnEditor(replan_turn)
        if prompt_prefix:
            _replan_editor.prepend(_text_prefix_part(prompt_prefix))
        # ADR 0113 (declarative continuity): plan declares ``loop_continue``,
        # so round 2+ resumes the prior plan-loop session (policy below). Carry
        # the prior plan attempt as a compact handoff anyway — it is content-safe
        # on both paths: on a resumed delta the prior plan is in history, and on
        # a fallback full render (no prior session) it lets the architect revise
        # its own attempt instead of planning blind. ``replan_turn`` already
        # renders the reviewer critique; pairing it with the prior plan markdown
        # keeps the revise-this framing intact.
        prior_plan_md = state.plan_markdown or ""
        if prior_plan_md:
            _replan_editor.append(_prior_plan_handoff_part(
                f"--- PRIOR PLAN (round {plan_round - 1}, revise this) ---\n"
                f"{prior_plan_md}\n--- END PRIOR PLAN ---"
            ))
        turn = _replan_editor.build()
        resume_architect = decide_session_continuation(
            state, role=SessionInvocationRole.PLAN, phase="plan",
            round_key="plan_round",
        ).continue_session
        # M8: replan resumes the SAME plan-loop physical session
        # (phase="plan", not "replan"). Round 1 plan seeded
        # sent_part_keys for the architect role / format / contracts
        # under PER_PHASE:plan; replan must hit that key so the M6
        # selector omits the parts the architect already saw.
        with defer_assistant_json():
            # Replan is a read-only architect invocation. ``mutates_artifacts``
            # stays False so the runtime drops the Claude write flags; the
            # composed replan prompt already carries any TEXT prefix.
            # ``continue_session=True`` for round 2+ so the architect
            # keeps its memory of round-1 attempt + critique.
            output = _session_aware_invoke(
                agent, state,
                phase="plan",
                turn=turn,
                cwd=_agent_project_dir(state),
                continue_session=resume_architect,
                attachments=_multimodal_attachments(state),
                # ADR 0026 delta: on a resumed replan the original task is
                # already in the architect's conversation history (sent on
                # round 1 under the shared phase="plan" key). Drop it from
                # the wire so replan carries only the new critique. On a
                # full render (stateless / no prior session) the selector
                # ignores this and the task is sent.
                delta_droppable_part_ids=("turn_input:replan_task",),
            )
        from core.contracts.plan_schema import PlanSchemaError
        from pipeline.plan_parser import PlanParseError, parse_plan
        # M8 / M14.1: _session_aware_invoke stashed trace metadata
        # (M12 ``prompt_render`` + M14.1 ``context_growth``) under
        # state.phase_log["plan"] before parsing ran. Both the
        # success and parse-error paths overwrite that dict;
        # _carry_trace_metadata captures the sub-keys so they
        # survive the rebuild (mirror of M7 validate_plan pattern).
        _replan_carried = _carry_trace_metadata(state, "plan")
        try:
            parsed_plan = parse_plan(output)
        except (PlanSchemaError, PlanParseError) as e:
            state.plan_markdown = output
            print(_render_parse_failure(
                title=f"PLAN replan (round {plan_round})",
                error=str(e),
                raw_output=output,
            ))
            state.stop(f"plan rejected before implement: {e}")
            _finalize_replan_phase_log(state, base={
                "output": output,
                "meta": {"replan": True, "critique": state.last_critique},
                "parse_error": str(e),
                "attempt": plan_round,
                **_replan_carried,
            })
            return state
        _emit_plan_parsed_event(parsed_plan)
        plan_md = _render_and_store_plan_artifact(
            state, parsed_plan, attempt=plan_round,
        )
        ctx = _ensure_lifecycle_ctx(state)
        existing, missing = ctx.plan_helpers.validate_paths(
            parsed_plan, _agent_project_dir(state),
        )
        state.parsed_plan = parsed_plan
        from pipeline.repair_protocol import build_repair_receipt

        replan_receipt = _store_repair_receipt(
            state,
            build_repair_receipt(
                source_phase="validate_plan",
                source_round=plan_round - 1,
                repair_phase="plan",
                repair_round=plan_round,
                critique=state.last_critique,
                repair_output=plan_md,
                operator_feedback=state.human_feedback,
                changed_refs=("parsed_plan", "plan_markdown"),
            ),
        )
        # Phase 5d-fixup: stuff ``replan_critique`` so PlanAdapter
        # writes it into the session shape (legacy
        # the legacy plan-loop did this orchestrator-side; v2 dispatch
        # delegates to handler). Tests assert
        # ``"replan_critique" in session["phases"]["plan"][round-1]``
        # for any round > 1.
        _finalize_replan_phase_log(state, base={
            "output": plan_md,
            "meta": {
                "replan": True,
                "critique": state.last_critique,
                **_runtime_session_meta(
                    agent, continue_session=resume_architect,
                ),
            },
            "parsed_file_paths": list(parsed_plan.file_paths),
            "existing_files": list(existing),
            "missing_files": list(missing),
            "total_atomic_tasks": parsed_plan.total_atomic_tasks,
            "parse_warnings": list(parsed_plan.parse_warnings),
            "attempt": plan_round,
            "codemap_injected": False,    # always False on replan
            "hypothesis_injected": False, # always False on replan
            "hypothesis_feedback_injected": False,  # always False on replan
            "repair_receipt": replan_receipt,
            **_replan_carried,
        })
        _print_plan_preview(state)
        return state

    suffix = ""
    hypothesis = state.extras.get("validated_hypothesis", "")
    rejected_attempts = state.extras.get("hypothesis_attempts") or []
    feedback_injected = False
    if hypothesis:
        from pipeline.engine.hypothesis import format_validated_hypothesis_context
        suffix = format_validated_hypothesis_context(hypothesis)
        from core.observability.logging import success
        success(
            "Planning with validated hypothesis context "
            "(incorporate, falsify, or explain divergence)"
        )
    elif rejected_attempts and plan_round == 1:
        # No approved direction, but the reviewer's rejection findings
        # are still useful as *negative* planning context. Append them
        # only on round 1 — replan branches already have last_critique
        # to work with.
        from pipeline.engine.hypothesis import format_rejected_hypothesis_feedback
        suffix = format_rejected_hypothesis_feedback(rejected_attempts)
        feedback_injected = True
        from core.observability.logging import success
        success(
            "Planning with rejected hypothesis feedback "
            "(not validated direction)"
        )

    prompt_prefix = _plan_prompt_prefix(state)
    verification_part = _verification_contract_part(state, "plan")

    if state.dry_run:
        # Bypass agent invocation entirely on dry-run; mirror
        # adapters.run_plan's PhaseResult shape so the rest of
        # _phase_plan keeps working unchanged.
        result = adapters.run_plan(
            agent,
            state.task,
            _agent_project_dir(state),
            state.plugin,
            codemap=state.extras.get("codemap", ""),
            prompt_suffix=suffix,
            prompt_prefix=prompt_prefix,
            change_handoff=_change_handoff_for(state),
            dry_run=state.dry_run,
            prompt_spec=prompt_spec,
            attachments=_multimodal_attachments(state),
            continue_session=decide_session_continuation(
                state, role=SessionInvocationRole.PLAN, phase="plan",
                round_key="plan_round",
            ).continue_session,
            verification_part=verification_part,
        )
    else:
        # M8: replace adapters.run_plan's invoke step with the M7
        # session-aware helper so plan rounds opt into per_phase
        # delta rendering (replan keeps the same key — see the
        # replan branch above). Prompt assembly mirrors run_plan
        # exactly so the wire prompt is byte-identical to the
        # legacy path on any single call. The render envelope is
        # rebuilt to include out-of-builder additions
        # (prompt_prefix, codemap, hypothesis suffix); without that
        # the M6 selector would either drop those additions on
        # full render (if the helper used envelope.text) or hide
        # them from --output debug trace.
        from pipeline.prompts import plan_prompt as _plan_prompt
        from pipeline.prompts.turn import (
            PromptTurnEditor as _PromptTurnEditor,
            hypothesis_suffix_part as _hypothesis_suffix_part_from_turn,
        )

        base_turn = _plan_prompt(
            state.task,
            _agent_project_dir(state),
            state.plugin,
            change_handoff=_change_handoff_for(state),
            prompt_spec=prompt_spec,
            verification_part=verification_part,
        )
        codemap_text = state.extras.get("codemap", "")
        codemap_body = (
            f"--- REPO MAP ---\n{codemap_text}\n--- END REPO MAP ---"
            if codemap_text else ""
        )
        # Build the turn using PromptTurnEditor. Each append/prepend
        # owns both the wire text and the part shadow atomically.
        # PromptTurnEditor adds "\n\n" separator between segments
        # automatically so bodies must NOT carry leading "\n\n".
        _plan_editor = _PromptTurnEditor(base_turn)
        if prompt_prefix:
            _plan_editor.prepend(_text_prefix_part(prompt_prefix))
        if codemap_body:
            _plan_editor.append(_codemap_part(codemap_body))
        if suffix:
            _plan_editor.append(_hypothesis_suffix_part_from_turn(suffix))
        turn = _plan_editor.build()

        # ADR 0113 (declarative continuity): plan declares ``loop_continue``,
        # so the policy resumes the prior loop session on round 2+ (the
        # restored pre-0113 behaviour) and starts fresh on round 1. Compute the
        # disposition once, with ``round_key='plan_round'`` so the loop_continue
        # follow-on probe reads the plan-loop counter, so the invoke and the
        # reflected session meta agree on the policy value.
        plan_continue_session = decide_session_continuation(
            state, role=SessionInvocationRole.PLAN, phase="plan",
            round_key="plan_round",
        ).continue_session
        with defer_assistant_json():
            output = _session_aware_invoke(
                agent, state,
                phase="plan",
                turn=turn,
                cwd=_agent_project_dir(state),
                continue_session=plan_continue_session,
                attachments=_multimodal_attachments(state),
            )

        result = adapters.PhaseResult(
            name="plan",
            output=output,
            meta=_runtime_session_meta(
                agent, continue_session=plan_continue_session,
            ),
        )

    # REA-1 finalizer: parse exactly once into the canonical ParsedPlan.
    # Structured JSON is the machine contract and malformed schema/DAG
    # data stops here before implement. Markdown-only task sections still parse
    # through parse_plan()'s internal fallback for legacy/custom prompts.
    # M8 / M14.1: _session_aware_invoke stashed trace metadata
    # (M12 ``prompt_render`` + M14.1 ``context_growth``) under
    # state.phase_log["plan"] before parsing ran. Both the
    # success and parse-error paths overwrite that dict below;
    # _carry_trace_metadata captures the sub-keys so they survive
    # the rebuild. Always defined (empty dict on dry-run) so the
    # success-path spread does not NameError.
    _plan_carried = _carry_trace_metadata(state, "plan")
    if not state.dry_run:
        from core.contracts.plan_schema import PlanSchemaError
        from pipeline.plan_parser import PlanParseError, parse_plan
        try:
            parsed_plan = parse_plan(result.output)
        except (PlanSchemaError, PlanParseError) as e:
            state.plan_markdown = result.output
            print(_render_parse_failure(
                title="PLAN",
                error=str(e),
                raw_output=result.output,
            ))
            state.stop(
                f"plan rejected before implement: {e}"
            )
            state.phase_log["plan"] = {
                "output": result.output,
                "meta": dict(result.meta),
                "parse_error": str(e),
                **_plan_carried,
            }
            return state
        # REA-2/3: emit plan.parsed so the evidence bundle can render
        # the actual typed contract without re-reading markdown.
        _emit_plan_parsed_event(parsed_plan)
        plan_md = _render_and_store_plan_artifact(
            state, parsed_plan, attempt=plan_round,
        )
        ctx = _ensure_lifecycle_ctx(state)
        existing, missing = ctx.plan_helpers.validate_paths(
            parsed_plan, _agent_project_dir(state),
        )
    else:
        from pipeline.plan_parser import ParsedPlan
        parsed_plan = ParsedPlan(
            short_summary="Dry run plan.",
            planning_context="Dry run skipped planner execution.",
            subtasks=(),
            source="dry_run",
        )
        existing, missing = [], []
        plan_md = "[DRY RUN]"
        state.plan_markdown = plan_md

    state.parsed_plan = parsed_plan
    state.phase_log["plan"] = {
        "output":              plan_md,
        "meta":                dict(result.meta),
        "parsed_file_paths":   list(parsed_plan.file_paths),
        "existing_files":      list(existing),
        "missing_files":       list(missing),
        "total_atomic_tasks":  parsed_plan.total_atomic_tasks,
        "parse_warnings":      list(parsed_plan.parse_warnings),
        # Phase 5d-fixup: stuff per-round metadata that the legacy
        # orchestrator computed in the legacy plan loop. ``codemap`` and
        # ``validated_hypothesis`` live in state.extras (run-level setup
        # by ``run_pipeline`` / ``_run_hypothesis_block``); the handler
        # can read them now, no orchestrator-side overlay needed.
        "attempt":              plan_round,
        "codemap_injected":     bool(state.extras.get("codemap")) and plan_round == 1,
        "hypothesis_injected":  bool(state.extras.get("validated_hypothesis")) and plan_round == 1,
        # Distinct flag for the rejected-feedback path: ``hypothesis_injected``
        # claims an APPROVED direction was appended; ``hypothesis_feedback_injected``
        # claims rejected QA findings were appended as negative context. The
        # two are mutually exclusive on a single plan attempt — never both.
        "hypothesis_feedback_injected": feedback_injected,
        # replan_critique: None on round 1; replan branch above sets it.
        **(_plan_carried if not state.dry_run else {}),
    }
    _print_plan_preview(state)
    return state
