# SPDX-License-Identifier: Apache-2.0
"""``review_changes`` phase handler — review_changes (post-build review) phase handler.

Imports helpers from their real homes (never from the package
facade) so there is no import cycle through the builtin __init__.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.contracts.review_schema import ReviewSchemaError
from core.io.transcript import render_parse_failure as _render_parse_failure
from pipeline.phases import adapters
from pipeline.phases.builtin.lifecycle import (
    _agent_project_dir,
    _carry_trace_metadata,
    _change_handoff_for,
    _ensure_lifecycle_ctx,
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
    _current_change_review_subject,
    _operator_waiver_text,
    _print_review_preview,
    _repair_receipt_text,
    _verification_receipt_text,
)
from pipeline.phases.builtin.session_invoke import _session_aware_invoke
from pipeline.phases.builtin.session_keys import (
    _runtime_session_meta,
    _should_resume,
    decide_session_continuation,
)
from pipeline.phases.review_contract_recovery import retry_review_contract_once
from pipeline.review_markdown import (
    render_fix_critique,
    render_review_markdown,
)
from pipeline.review_parser import (
    ReviewParseError,
    parse_review,
)
from pipeline.runtime.roles import SessionInvocationRole

if TYPE_CHECKING:
    from pipeline.runtime import PipelineState


def _implement_delivery_blocked(state: PipelineState) -> bool:
    """True when implement produced a blocking delivery outcome."""
    implement = state.phase_log.get("implement")
    if not isinstance(implement, dict):
        return False
    if implement.get("delivery_clean") is False:
        return True
    if str(implement.get("delivery_status") or "").lower() == "incomplete":
        return True
    for key in (
        "incomplete_subtasks",
        "missing_subtask_receipts",
        "attestation_incomplete",
    ):
        if implement.get(key):
            return True
    return False


def _phase_review_changes(state: PipelineState) -> PipelineState:
    """Wrap ``adapters.run_review`` for the post-build review step.

    Phase 5d step 1: write ``phase_log["review_changes"]["clean"]`` so the v2
    LoopStep ``until: review.clean`` predicate exits the loop without
    running repair_changes after an APPROVED JSON review.

    Also mirrors legacy ``has_uncommitted`` pre-loop short-circuit:
    when there's nothing to review (no uncommitted changes), skip the
    agent call entirely and signal RoundAdapter to NOT append a round
    entry. Without this v2 dispatch produced spurious round entries on
    review-only modes where implement made no commits.
    """
    # Phase 5e-5 substep 4 + 6b: read git/text helpers from ctx. The
    # FSM always populates ``state.lifecycle_ctx`` for v2
    # Profile dispatch (substep 6b's ctx=None collapse). The default
    # ``default_lifecycle_context`` factory wires
    # ``GitHelpers.has_uncommitted`` / ``TextHelpers.critique_is_empty``
    # via ``pipeline.project_orchestrator`` re-exports so existing
    # ``@patch("pipeline.project_orchestrator.has_uncommitted")`` test
    # mocks continue to take effect.
    ctx = _ensure_lifecycle_ctx(state)
    _git_helpers = ctx.git_helpers
    _text_helpers = ctx.text_helpers

    cwd = _agent_project_dir(state)

    # Pre-condition: no uncommitted changes → nothing to review. Mirror
    # legacy ``run_review_fix_loop`` early-break (line 1245-1247).
    # Defensive: ``has_uncommitted`` shells out to git; missing cwd or
    # non-git project should fall through to the agent rather than
    # raising (test fixtures may use non-existent cwd).
    change_handoff = _change_handoff_for(state)
    effective_change_handoff = change_handoff
    if change_handoff == "uncommitted" and not state.dry_run:
        try:
            no_uncommitted = not _git_helpers.has_uncommitted(cwd)
        except (FileNotFoundError, OSError):
            no_uncommitted = False
        committed_review_target = (
            no_uncommitted and _has_implemented_review_target(state, cwd)
        )
        if committed_review_target:
            effective_change_handoff = "commit_set"
        elif no_uncommitted and _implement_delivery_blocked(state):
            state.last_critique = (
                "implement delivery is incomplete; review_changes has no "
                "diff target and must wait for the implement handoff"
            )
            state.phase_log["review_changes"] = {
                "output": state.last_critique,
                "meta": {},
                "clean": False,
                "approved": False,
                "verdict": "REJECTED",
                "short_summary": state.last_critique,
                "findings": [],
                "skipped": "implement delivery incomplete",
            }
            state.phase_log["rounds_pending"] = {"_skip_adapter": True}
            return state
        elif no_uncommitted:
            state.last_critique = ""
            state.phase_log["review_changes"] = {
                "output":  "",
                "meta":    {},
                "clean":   True,
                "skipped": "no uncommitted changes",
            }
            # Tell RoundAdapter to skip appending — legacy's no-uncommitted
            # path didn't write any round entry.
            state.phase_log["rounds_pending"] = {"_skip_adapter": True}
            return state

    agent = _require_agent(state, "review_changes_agent")
    prompt_spec = _prompt_from_active_step(ctx)
    operator_waiver = _operator_waiver_text(state)
    verification_part = _verification_contract_part(state, "review_changes")
    # ADR 0113: review is non-edit-shaped → the session-disposition policy
    # always resolves it FRESH. The compact review handoff (repair receipt +
    # current review subject, alongside the always-present plan/handoff
    # contracts) is assembled on every follow-on round — round 2+ or the
    # post-repair re-verify pass — INDEPENDENT of session continuation, so a
    # fresh reviewer still audits "did you fix what I asked" without amnesia.
    review_disposition = decide_session_continuation(
        state, role=SessionInvocationRole.REVIEW, phase="review_changes",
    )
    review_followon = _should_resume(
        state, role=SessionInvocationRole.REVIEW, round_key="repair_round",
    )
    repair_receipt = _repair_receipt_text(state) if review_followon else ""
    current_subject = (
        _current_change_review_subject(state) if review_followon else ""
    )
    if state.dry_run:
        result = adapters.run_review(
            agent,
            state.task,
            cwd,
            state.plugin,
            plan_contract=_plan_contract_for(state),
            plan_tasks=_plan_tasks_for(state),
            handoff_contract=_handoff_contract_for(state),
            change_handoff=effective_change_handoff,
            dry_run=state.dry_run,
            label="review_changes",
            prompt_spec=prompt_spec,
            attachments=_multimodal_attachments(state),
            operator_waiver=operator_waiver,
            repair_receipt=repair_receipt,
            current_review_subject=current_subject,
            continue_session=review_disposition.continue_session,
            verification_part=verification_part,
        )
    else:
        # M9: replace adapters.run_review's invoke step with the
        # session-aware helper under phase="review_changes" so the
        # reviewer loop opts into per_phase delta on round 2+.
        # Mirrors adapters.run_review's prompt assembly exactly so the
        # wire prompt is byte-identical to the legacy path on any
        # single call.
        from pipeline.prompts import review_focus
        from pipeline.prompts.builders import (
            runtime_review_uncommitted_prompt,
        )

        focus = review_focus(
            state.task,
            state.plugin,
            change_handoff=effective_change_handoff,
            prompt_spec=prompt_spec,
            verification_part=verification_part,
        )
        # ADR 0076 (T7): brief verification-environment receipt summary so
        # the reviewer trusts substantiated developer-side checks. Empty
        # when no phase wrote a receipt — adds no block.
        verification_receipt = _verification_receipt_text(state)
        # The wrapping runtime prompt is what actually publishes the
        # M2 envelope (see prompts.builders), so build it after the
        # focus assembly. No out-of-builder prefix for review_changes
        # (AGENTS.md injection removed — ADR 0059). ``focus`` is a
        # PromptTurn (from review_focus); the builder normalizes
        # str|PromptTurn at its boundary (single seam).
        turn = runtime_review_uncommitted_prompt(
            focus,
            project_dir=cwd,
            plan_contract=_plan_contract_for(state),
            plan_tasks=_plan_tasks_for(state),
            handoff_contract=_handoff_contract_for(state),
            change_handoff=effective_change_handoff,
            repair_receipt=repair_receipt,
            current_review_subject=current_subject,
            verification_receipt=verification_receipt,
            operator_waiver=operator_waiver,
        )
        raw = _session_aware_invoke(
            agent, state,
            phase="review_changes",
            turn=turn,
            cwd=cwd,
            continue_session=review_disposition.continue_session,
            attachments=_multimodal_attachments(state),
        )
        result = adapters.PhaseResult(
            name="review_changes",
            output=raw,
            meta=_runtime_session_meta(
                agent, continue_session=review_disposition.continue_session,
            ),
        )
    raw = result.output
    # M9 / M14.1: _session_aware_invoke stashed trace metadata
    # (M12 prompt_render + M14.1 context_growth) under
    # state.phase_log["review_changes"] before the parser ran.
    # _carry_trace_metadata preserves both keys through the
    # rebuild.
    _review_carried = _carry_trace_metadata(state, "review_changes")
    contract_repair: dict[str, Any] | None = None
    try:
        parsed = parse_review(raw)
    except (ReviewSchemaError, ReviewParseError) as e:
        original_raw = raw
        retry_raw = ""
        try:
            contract_result = retry_review_contract_once(
                agent,
                phase="review_changes",
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
                    agent, continue_session=review_disposition.continue_session,
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
                    agent, continue_session=review_disposition.continue_session,
                ),
            }
            if retry_raw:
                repair_meta["retry_raw_output"] = retry_raw
            body = (
                f"review parse error: {retry_error}\n\n"
                f"Raw output:\n{raw_for_failure}"
            )
            state.last_critique = body
            state.phase_log["review_changes"] = {
                "output":      body,
                "raw_output":  raw_for_failure,
                "meta":        dict(result.meta),
                "clean":       False,
                "approved":    False,
                "verdict":     "REJECTED",
                "critique":    body,
                "parse_error": str(retry_error),
                "contract_repair": repair_meta,
                **_review_carried,
            }
            print(_render_parse_failure(
                title="review_changes",
                error=str(retry_error),
                raw_output=raw_for_failure,
            ))
            state.stop(
                "review contract rejected before repair_changes: "
                f"{retry_error}"
            )
            return state

    clean = parsed.approved
    rendered = render_review_markdown(parsed)
    state.last_critique = "" if clean else render_fix_critique(parsed)
    entry = {
        "output":        rendered,
        "raw_output":    raw,
        "meta":          dict(result.meta),
        "clean":         clean,
        "approved":      parsed.approved,
        "verdict":       parsed.verdict,
        "critique":      state.last_critique,
        "short_summary": parsed.short_summary,
        "findings":      parsed.findings_as_dicts(),
        "parse_warnings": list(parsed.parse_warnings),
        **_review_carried,
    }
    if contract_repair is not None:
        entry["contract_repair"] = contract_repair
    state.phase_log["review_changes"] = entry
    review_meta = result.meta or {}
    rounds_pending = dict(state.phase_log.get("rounds_pending", {}) or {})
    review_session_id = review_meta.get("session_id")
    if review_session_id is not None:
        rounds_pending["review_session_id"] = review_session_id
    review_continue_session = review_meta.get("continue_session")
    if review_continue_session is not None:
        rounds_pending["review_continue_session"] = review_continue_session
    followup_parent_review_session_id = review_meta.get("followup_parent_session_id")
    if followup_parent_review_session_id is not None:
        rounds_pending["followup_parent_review_session_id"] = (
            followup_parent_review_session_id
        )
    if rounds_pending:
        state.phase_log["rounds_pending"] = rounds_pending
    _print_review_preview(state, "review_changes", "Review")
    return state


def _has_implemented_review_target(state: PipelineState, cwd: str) -> bool:
    """True when implement changed the tree despite a clean worktree.

    ``change_handoff=uncommitted`` normally reviews the working tree and skips
    when there is no dirty state. If an authoring runtime accidentally commits
    during implement, the working tree is clean but the run still produced a
    review target. The implement handler records a pre-implement tree snapshot;
    compare it with the current tree so review sees that committed delta instead
    of silently skipping.
    """
    implement_log = state.phase_log.get("implement")
    if not isinstance(implement_log, dict):
        return False
    baseline = implement_log.get("change_baseline_ref")
    if not isinstance(baseline, str) or not baseline.strip():
        return False
    return _git_tree_changed_since(cwd, baseline.strip())


def _git_tree_changed_since(cwd: str, baseline_ref: str) -> bool:
    try:
        from pipeline.engine.run_diff import resolve_git_root, snapshot_worktree

        git_root = resolve_git_root(Path(cwd))
        if git_root is None:
            return False
        current_tree = snapshot_worktree(git_root)
        right_ref = current_tree or "HEAD"
        result = subprocess.run(
            [
                "git", "diff", "--quiet", "--no-ext-diff",
                baseline_ref, right_ref, "--",
            ],
            cwd=str(git_root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 1
