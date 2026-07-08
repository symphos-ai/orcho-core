# SPDX-License-Identifier: Apache-2.0
"""Plan artifact and validate-plan review helpers for builtin phases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.io.transcript import render_plan_block as _render_plan_block
from pipeline.phases.builtin.prompt_parts import _multimodal_attachments

if TYPE_CHECKING:
    from pipeline.runtime import PipelineState, PromptSpec


def _approved_review_json(short_summary: str) -> str:
    return json.dumps({
        "verdict":       "APPROVED",
        "short_summary": short_summary,
        "findings":      [],
        "risks":         [],
        "checks":        [],
    })


def _parsed_plan_to_render_dict(parsed_plan) -> dict:
    """Convert a :class:`pipeline.plan_parser.ParsedPlan` into the mapping
    shape :func:`core.io.transcript.render_plan_block` consumes."""
    tasks: list[dict] = []
    for st in parsed_plan.subtasks:
        tasks.append({
            "id":            st.id,
            "goal":          st.goal,
            "spec":          st.spec,
            "files":         list(st.files),
            "skill":         st.skill,
            "model":         st.model,
            "depends_on":    list(st.depends_on),
            "done_criteria": list(st.done_criteria),
            "owned_files":   list(getattr(st, "owned_files", ()) or ()),
        })
    return {
        "short_summary":       parsed_plan.short_summary,
        "planning_context":    parsed_plan.planning_context,
        "goal":                parsed_plan.goal or "",
        "acceptance_criteria": list(parsed_plan.acceptance_criteria),
        "owned_files":         list(parsed_plan.owned_files),
        "commands_to_run":     list(parsed_plan.commands_to_run),
        "risks":               list(parsed_plan.risks),
        "review_focus":        list(parsed_plan.review_focus),
        "mcp_context":         list(parsed_plan.mcp_context),
        "tasks":               tasks,
    }


def _print_plan_preview(state: PipelineState) -> None:
    """Emit the structured plan block on stdout after a successful parse.

    ADR 0046 Phase F follow-up (sites 19/20/21 inventory miss caught
    by the Phase F boundary tests): suppress under SILENT in addition
    to dry_run unless the typed request explicitly opts into parsed
    phase output previews. The structured plan is already persisted to
    ``session.json`` + ``events.jsonl``; this block is pure CLI
    transparency for the operator. ``state.extras["_silent"]`` is set
    by ``pipeline.project.app`` after request destructuring; absent
    key (e.g. direct test instantiations) falls back to TERMINAL."""
    if _suppress_phase_output_preview(state):
        return
    parsed = getattr(state, "parsed_plan", None)
    if parsed is None or parsed.source == "dry_run":
        return
    print(_render_plan_block(_parsed_plan_to_render_dict(parsed)))


def _suppress_phase_output_preview(state: PipelineState) -> bool:
    if state.dry_run:
        return True
    return bool(
        state.extras.get("_silent")
        and not state.extras.get("_render_phase_outputs")
    )


def _emit_plan_parsed_event(parsed_plan) -> None:
    """Emit the typed PLAN contract after successful parsing.

    Counts are kept for compact dashboards; full contract lists are
    included so evidence.json is portable and does not have to invent
    placeholder entries.
    """
    from core.observability import events as _events

    _events.emit(
        "plan.parsed",
        source=parsed_plan.source,
        short_summary=parsed_plan.short_summary,
        planning_context=parsed_plan.planning_context,
        subtask_count=len(parsed_plan.subtasks),
        has_contract=parsed_plan.has_contract,
        goal=parsed_plan.goal or "",
        acceptance_criteria=list(parsed_plan.acceptance_criteria),
        acceptance_criteria_count=len(parsed_plan.acceptance_criteria),
        owned_files=list(parsed_plan.owned_files),
        owned_files_count=len(parsed_plan.owned_files),
        commands_to_run=list(parsed_plan.commands_to_run),
        commands_to_run_count=len(parsed_plan.commands_to_run),
        risks=list(parsed_plan.risks),
        review_focus=list(parsed_plan.review_focus),
        mcp_context=list(parsed_plan.mcp_context),
        subtasks=_parsed_plan_to_render_dict(parsed_plan)["tasks"],
    )


def _render_and_store_plan_artifact(
    state: PipelineState,
    parsed_plan,
    *,
    attempt: int,
) -> str:
    """Render validated plan markdown and write the run-local plan artifacts.

    PLAN stdout is the machine contract. This helper creates the human artifact
    from the parsed object so real and mock runtimes share one path.

    Two siblings are persisted per attempt:

    * ``plan_<run_id>_r<attempt>.md`` — human projection (this function
      historically only wrote this file).
    * ``plan_<run_id>_r<attempt>.json`` + ``parsed_plan.json`` — durable
      machine artefact and latest-pointer, written via
      :func:`pipeline.plan_artifacts.write_parsed_plan_artifact`. This
      is the source of truth for future ``--from-run-plan`` hydration
      and cross-project hand-off; the markdown sibling is presentation
      only. See the "ParsedPlan is canonical / markdown is projection"
      invariants in the over-run-plan follow-up and change-semantics planning record (internal).
    """
    from core.observability import events as _events
    from pipeline.plan_artifacts import write_parsed_plan_artifact
    from pipeline.plan_markdown import render_plan_markdown

    plan_md = render_plan_markdown(parsed_plan)
    state.plan_markdown = plan_md

    if state.dry_run or state.output_dir is None:
        return plan_md

    plan_dir = Path(state.output_dir)
    plan_dir.mkdir(parents=True, exist_ok=True)
    run_id = plan_dir.name or "run"
    plan_file = plan_dir / f"plan_{run_id}_r{attempt}.md"
    plan_file.write_text(plan_md, encoding="utf-8")
    state.extras["plan_artifact_path"] = str(plan_file)
    _events.emit(
        "artifact.created",
        path=str(plan_file),
        artifact_kind="plan",
        size_bytes=plan_file.stat().st_size,
        attempt=int(attempt),
    )

    # Durable machine artefact. Written after the markdown sibling so
    # ``plan_artifact_path`` stays the human-facing pointer (the existing
    # validate_plan / cross-handoff paths use it as the reviewer surface)
    # and the new machine pointer travels separately. Failure here would
    # be a real I/O regression for the JSON layer; we deliberately do not
    # swallow it — the markdown was already persisted, but the run is
    # missing its canonical machine source and the operator needs to
    # know now, not at the next ``--from-run-plan`` attempt.
    json_attempt_path = write_parsed_plan_artifact(
        plan_dir, parsed_plan, attempt=int(attempt),
    )
    state.extras["parsed_plan_json_path"] = str(plan_dir / "parsed_plan.json")
    _events.emit(
        "artifact.created",
        path=str(json_attempt_path),
        artifact_kind="parsed_plan",
        size_bytes=json_attempt_path.stat().st_size,
        attempt=int(attempt),
    )
    return plan_md


def _finalize_replan_phase_log(
    state: PipelineState, *, base: dict[str, Any],
) -> None:
    """Stamp invariant replan fields onto ``phase_log['plan']`` and clear feedback.

    Every replan-branch exit (dry-run, parse-error, success) must record
    both reviewer critique and operator feedback in the persisted plan
    attempt, then clear ``state.human_feedback`` so a subsequent retry
    without new operator input does not re-inject stale text. Reviewer
    critique reset stays on the existing approval path
    (``_phase_validate_plan`` clears ``state.last_critique`` on approve).

    ``base`` is the per-exit dict; this helper merges the invariants
    onto it, installs the result, and performs the clear.
    """
    meta = dict(base.get("meta") or {})
    meta.setdefault("replan", True)
    meta["human_directed"] = bool(state.human_feedback)
    base["meta"] = meta
    base["replan_critique"] = state.last_critique
    base["human_feedback"] = state.human_feedback
    state.phase_log["plan"] = base
    state.human_feedback = ""


def _plan_contract_for(state: PipelineState) -> str:
    """REA-1: render the typed plan contract for the current run.

    ``state.parsed_plan`` is canonical across linear and DAG profiles.
    Empty contracts render to ``""`` so phase handlers can compose this
    unconditionally.
    """
    parsed_plan = getattr(state, "parsed_plan", None)
    if parsed_plan is None:
        return ""
    from pipeline.plan_contract import render_plan_contract
    return render_plan_contract(parsed_plan)


def _plan_tasks_for(state: PipelineState) -> str:
    """Render the current parsed plan's executable subtask view.

    The task decomposition is part of the canonical plan handoff, not
    a validate_plan-only detail. Downstream implement/review/repair
    phases need it on the wire so work is executed against the planner's
    slices rather than only the plan-level acceptance contract.
    """
    parsed_plan = getattr(state, "parsed_plan", None)
    if parsed_plan is None:
        return ""
    from pipeline.plan_markdown import render_validate_plan_tasks
    return render_validate_plan_tasks(parsed_plan)


def _plan_prompt_prefix(state: PipelineState) -> str:
    """Shared PLAN / REPLAN context prefix — Phase 4.5 TEXT attachments.

    Rendered ahead of the base PLAN / REPLAN prompt assembled by the
    caller. Returns ``""`` when the run carries no TEXT attachments.

    Project rules (AGENTS.md / CLAUDE.md) are intentionally NOT injected
    here: the native agent runtimes (Claude Code, Codex) discover their
    own project-instruction file in the worktree cwd, so re-pasting it
    would only duplicate tokens. See ADR 0059.
    """
    if not state.attachments:
        return ""
    from pipeline.attachment_inject import render_text_block
    return render_text_block(state.attachments)


def _review_plan_artifact(
    agent: Any, state: PipelineState, focus: str, cwd: str,
    *,
    prompt_spec: PromptSpec | None = None,
    continue_session: bool = False,
) -> str:
    """Drive the reviewer runtime against the plan output.

    Routes between two surfaces based on whether the run owns a
    parsed plan:

    * ``state.parsed_plan is not None`` — normal validate_plan path.
      Renders two typed views from the parsed plan
      (``plan_contract:typed_plan`` + ``plan_tasks:execution_plan``)
      via :func:`pipeline.prompts.builders.plan_file_review_prompt`.
      No markdown round-trip: the on-disk ``plan_*.md`` stays as
      human-readable evidence but the reviewer reads typed views,
      not parsed prose. See the "ParsedPlan is canonical, plan.md is
      projection" invariants in the over-run-plan follow-up and change-semantics planning record (internal).

    * ``state.parsed_plan is None`` — diff-only fallback. The phase
      handler already pre-rendered the diff-focus prompt via
      ``plan_review_focus``; we ship it through
      ``runtime_review_uncommitted_prompt`` (the only legitimate
      no-plan branch — e.g. a plan-less profile aimed at review-only
      flows).

    Hard-fail invariant: when the run produced a plan artefact path
    (``state.extras["plan_artifact_path"]``) but ``state.parsed_plan``
    is absent, that is a runtime corruption — the plan markdown was
    persisted but the canonical parsed object got lost. We surface a
    clear exception rather than silently re-parsing the markdown from
    disk (the old fallback): markdown is a projection, not a
    round-trip source, and re-parsing would produce a partial plan
    without typed contract fields. See PR2 (parsed_plan.json) for
    the durable machine source.

    ``continue_session=True`` resumes the reviewer's prior bridge —
    used for round 2+ of the plan/validate_plan loop so the reviewer
    doesn't re-explain context from round 1.

    M7: invocations route through :func:`_session_aware_invoke`, which
    consumes the M2 prompt-trace render envelope, runs the M6 delta
    selector under :data:`PromptSessionSplit.PER_PHASE`, and either
    sends the full prompt or a delta wire prompt depending on whether
    the per-run :class:`PromptSessionState` has prior sent parts.
    Other validate_plan-adjacent surfaces stay on full rendering until
    M9 wires reviewer/repair policy.
    """
    from pipeline.phases.builtin.review_support import (
        _current_plan_review_subject,
        _repair_receipt_text,
    )
    from pipeline.phases.builtin.session_invoke import _session_aware_invoke
    from pipeline.prompts.builders import (
        plan_file_review_prompt,
        runtime_review_uncommitted_prompt,
    )

    attachments = _multimodal_attachments(state)
    parsed_plan = state.parsed_plan
    plan_artifact_path = state.extras.get("plan_artifact_path") or ""

    if parsed_plan is not None:
        plan_round = int(
            state.extras.get("plan_round")
            or state.extras.get("loop_round")
            or 1
        )
        repair_receipt = _repair_receipt_text(state) if plan_round >= 2 else ""
        current_subject = (
            _current_plan_review_subject(state)
            if repair_receipt else ""
        )
        turn = plan_file_review_prompt(
            parsed_plan, state.task, state.plugin,
            project_dir=cwd,
            repair_receipt=repair_receipt,
            current_review_subject=current_subject,
            prompt_spec=prompt_spec,
        )
        return _session_aware_invoke(
            agent, state,
            phase="validate_plan",
            turn=turn,
            cwd=cwd,
            continue_session=continue_session,
            attachments=attachments,
            # ADR 0026 delta: on a resumed validate retry the original task
            # is already in the reviewer's history (sent round 1). Drop it
            # from the wire so the retry carries only the new plan views.
            # The selector ignores this on a full render (round 1 / stateless).
            delta_droppable_part_ids=("turn_input:validate_plan_task",),
        )

    # No parsed plan. If a plan markdown was nonetheless persisted,
    # the run is in a torn state — fail loud instead of silently
    # falling back to a markdown re-parse that would lose typed
    # contract fields.
    if plan_artifact_path:
        raise RuntimeError(
            "validate_plan: plan artifact present at "
            f"{plan_artifact_path} but state.parsed_plan is None — "
            "refusing to reconstruct plan from markdown. The parsed "
            "object is the canonical contract; an upstream phase "
            "produced the markdown without setting state.parsed_plan, "
            "which is a runtime bug. See pipeline.plan_artifacts for "
            "the durable machine source.",
        )

    # Genuine no-plan branch (e.g. profile without a plan phase).
    # ``focus`` is a PromptTurn (from plan_review_focus); the builder
    # normalizes str|PromptTurn at its boundary (single seam), so pass
    # it through directly.
    turn = runtime_review_uncommitted_prompt(focus, project_dir=cwd)
    return _session_aware_invoke(
        agent, state,
        phase="validate_plan",
        turn=turn,
        cwd=cwd,
        continue_session=continue_session,
        attachments=attachments,
    )
