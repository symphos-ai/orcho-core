# SPDX-License-Identifier: Apache-2.0
"""``final_acceptance`` phase handler — final_acceptance (final QA gate) phase handler.

Imports helpers from their real homes (never from the package
facade) so there is no import cycle through the builtin __init__.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from core.infra.config import AppConfig
from core.io.transcript import render_parse_failure as _render_parse_failure
from pipeline.phases import adapters
from pipeline.phases.builtin.lifecycle import (
    _agent_project_dir,
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
    _operator_waiver_text,
    _print_review_preview,
    _raise_scope_expansion_handoff,
    _render_scope_expansion,
    _required_receipt_backstop,
    _route_scope_expansion_sanction,
    _scope_expansion_assessment,
    _verification_readiness_text,
)
from pipeline.phases.builtin.session_keys import _operating_mode_for_state

if TYPE_CHECKING:
    from pipeline.runtime import PipelineState


_NO_UNCOMMITTED_CHANGES = "no uncommitted changes"
_IMPLEMENT_DELIVERY_INCOMPLETE = "implement delivery incomplete"


def _no_uncommitted_review_target(
    state: PipelineState, *, cwd: str,
) -> bool:
    """Return True when final acceptance has no diff target to inspect."""
    if state.dry_run:
        return False
    if _change_handoff_for(state) != "uncommitted":
        return False

    review = state.phase_log.get("review_changes")
    if (
        isinstance(review, Mapping)
        and review.get("skipped")
        in {_NO_UNCOMMITTED_CHANGES, _IMPLEMENT_DELIVERY_INCOMPLETE}
    ):
        return True

    ctx = _ensure_lifecycle_ctx(state)
    try:
        no_uncommitted = not ctx.git_helpers.has_uncommitted(cwd)
    except (FileNotFoundError, OSError):
        return False
    if not no_uncommitted:
        return False

    from pipeline.phases.builtin.handlers.review_changes import (
        _has_implemented_review_target,
    )

    return not _has_implemented_review_target(state, cwd)


def _implement_evidence_complete(state: PipelineState) -> bool:
    """Guard no-diff approval with an actual implement evidence entry."""
    implement = state.phase_log.get("implement")
    if not isinstance(implement, Mapping):
        return True
    if not str(implement.get("output") or "").strip():
        return False

    if implement.get("delivery_clean") is False:
        return False
    if str(implement.get("delivery_status") or "").lower() == "incomplete":
        return False
    for key in (
        "incomplete_subtasks",
        "missing_subtask_receipts",
        "attestation_incomplete",
    ):
        value = implement.get(key)
        if value:
            return False
    return not bool(str(state.last_critique or "").strip())


def _write_no_diff_final_acceptance(
    state: PipelineState, *, approved: bool,
) -> PipelineState:
    verdict = "APPROVED" if approved else "REJECTED"
    summary = (
        "No file changes were produced; final acceptance is not applicable "
        "to the diff surface and implement evidence is complete or not part "
        "of this profile."
        if approved
        else (
            "No file changes were produced, but implement evidence is "
            "missing or incomplete, so final acceptance cannot approve "
            "the verification-only run."
        )
    )
    body = (
        "# Release gate\n\n"
        f"**Verdict:** {verdict}\n\n"
        f"**Ship ready:** {'yes' if approved else 'no'}\n\n"
        f"**Summary:** {summary}\n"
    )
    verification_gaps = [] if approved else [{
        "risk": "No diff target and no complete implement evidence.",
        "missing_evidence": (
            "final_acceptance had no uncommitted diff to review and could "
            "not find a complete implement phase evidence entry."
        ),
        "required_check": (
            "Run or resume the implementation phase so it records complete "
            "evidence, or provide a real diff target for review."
        ),
    }]
    skipped_reason = (
        _NO_UNCOMMITTED_CHANGES if approved else _IMPLEMENT_DELIVERY_INCOMPLETE
    )
    state.phase_log["final_acceptance"] = {
        "output": body,
        "raw_output": "",
        "approved": approved,
        "verdict": verdict,
        "short_summary": summary,
        "findings": [],
        "ship_ready": approved,
        "release_blockers": [],
        "verification_gaps": verification_gaps,
        "contract_status": {
            "task_contract": "satisfied" if approved else "incomplete",
            "interfaces": "not_applicable",
            "persistence": "not_applicable",
            "tests": "sufficient" if approved else "missing",
        },
        "parse_warnings": [],
        "meta": {
            "skipped": skipped_reason,
            "reason": "verification_no_changes"
            if approved else "final_acceptance_no_diff",
        },
        "skipped": skipped_reason,
        "review_target": "not_applicable",
        "diff": "none",
        "no_change_outcome": approved,
    }
    _print_review_preview(state, "final_acceptance", "Final acceptance")
    if not approved:
        state.last_critique = body
    return state


def _phase_final_acceptance(state: PipelineState) -> PipelineState:
    """Final acceptance gate over the post-repair working tree.

    Reuses the same review-uncommitted call shape as plain
    ``review_changes`` but routes to ``final_acceptance_agent`` so
    deployments can pin a different (often cheaper) model for the
    closing gate.

    ADR 0025 Phase 1: this gate uses ``release_json_contract`` instead
    of ``review_json_contract``. The reviewer emits the release shape
    (``ship_ready`` / ``release_blockers`` / ``verification_gaps`` /
    ``contract_status``); the parser is :func:`parse_release`. The
    handler writes a **dual-shape** entry into
    ``state.phase_log["final_acceptance"]`` — both the review-shape
    mirror (``verdict`` / ``short_summary`` / ``findings`` projected
    from release blockers) and the release fields — so existing
    consumers (Web phase card, MCP ``orcho_run_evidence``,
    ``sdk.evidence_slices.list_findings``, golden fixtures) keep
    working without API changes.

    ADR 0022: this gate records its critique on rejection but does not
    halt the pipeline — pause semantics for the plan loop live in the
    generic phase-handoff machinery driven by each step's ``handoff``
    policy. Hard contract-parse failures still halt; well-formed
    REJECTED verdicts let the run complete.
    """
    from pipeline.release_markdown import render_release_markdown
    from pipeline.release_parser import (
        ReleaseParseError,
        ReleaseSchemaError,
        parse_release,
    )

    cwd = _agent_project_dir(state)
    if _no_uncommitted_review_target(state, cwd=cwd):
        return _write_no_diff_final_acceptance(
            state,
            approved=_implement_evidence_complete(state),
        )

    agent = _require_agent(state, "final_acceptance_agent")
    ctx = _ensure_lifecycle_ctx(state)
    prompt_spec = _prompt_from_active_step(ctx)
    # ADR 0082 (Stage 5): read-only readiness digest of the declared
    # verification receipts. The helper short-circuits dry-run itself
    # (before touching any receipt loader) and returns "" without a
    # declared contract, keeping that wire prompt byte-identical.
    readiness = _verification_readiness_text(state)
    # F2 scope-expansion gate: classify out-of-plan files once from the run's
    # durable artefacts, then reuse the same assessment for the prompt block,
    # the blocker backstop, and the canonical durable evidence write below. An
    # empty assessment (ordinary in-scope diff / no contract / dry-run) leaves
    # ``readiness`` untouched, so the wire prompt stays byte-identical.
    scope_assessment = _scope_expansion_assessment(state)
    scope_text = _render_scope_expansion(scope_assessment)
    if scope_text:
        readiness = f"{readiness}\n\n{scope_text}" if readiness else scope_text
    # Legacy mark task with [final_acceptance] prefix so reviewer focus distinguishes
    # the final pass from the per-round review_changes (project_orchestrator.py 1199).
    result = adapters.run_review(
        agent,
        f"[final_acceptance] {state.task}",
        cwd,
        state.plugin,
        plan_contract=_plan_contract_for(state),  # REA-1
        plan_tasks=_plan_tasks_for(state),
        handoff_contract=_handoff_contract_for(state),
        change_handoff=_change_handoff_for(state),
        dry_run=state.dry_run,
        label="final_acceptance",
        require_verdict=True,
        prompt_spec=prompt_spec,
        attachments=_multimodal_attachments(state),
        operator_waiver=_operator_waiver_text(state),
        output_contract="release",
        verification_part=_verification_contract_part(state, "final_acceptance"),
        readiness_summary=readiness,
    )
    raw = result.output
    try:
        parsed = parse_release(raw)
    except (ReleaseSchemaError, ReleaseParseError) as e:
        body = f"final_acceptance parse error: {e}\n\nRaw output:\n{raw}"
        state.last_critique = body
        state.phase_log["final_acceptance"] = {
            "output":      body,
            "raw_output":  raw,
            "approved":    False,
            "verdict":     "REJECTED",
            "findings":    [],
            "ship_ready":  False,
            "release_blockers":  [],
            "verification_gaps": [],
            "contract_status":   None,
            "parse_error": str(e),
            "meta":        dict(result.meta),
        }
        print(_render_parse_failure(
            title="FINAL ACCEPTANCE", error=str(e), raw_output=raw,
        ))
        # Hard contract failure always halts: a malformed JSON contract
        # from the final reviewer is a protocol break, not a soft-fail.
        state.stop(f"final_acceptance contract rejected: {e}")
        return state

    approved = parsed.approved
    verdict = parsed.verdict
    ship_ready = parsed.ship_ready
    verification_gaps = parsed.gaps_as_dicts()
    task_language = AppConfig.load().task_language
    body = render_release_markdown(parsed, language=task_language)

    # ADR 0090 engine backstop: a required delivery gate whose receipt is
    # missing / failed / stale must surface as a release gap and force a
    # REJECTED verdict — regardless of what the reviewer model emitted. The
    # helper is empty under dry-run, without a contract, or when an operator
    # waiver is active, so every other run is byte-identical.
    engine_gaps = _required_receipt_backstop(state, language=task_language)
    # F2 scope-expansion sanction (ADR 0112 §5): the route each out-of-plan fact
    # takes is mode-projected, not the old fixed ``blocker → REJECTED`` coupling.
    # Only a genuine-safety HALT_WAIVER emits a release gap (forces REJECTED, in
    # every mode); a HANDOFF marks a phase-handoff need without rejecting;
    # AUTO_ALERT / AUTO_CONTINUE never block. The operator ``continue_with_waiver``
    # disarms the whole gate (the projection returns AUTO_CONTINUE for every item),
    # and the assessment is already empty under an active waiver. The sanction
    # decision lives in the T1 projection + the focused routing helper; this
    # handler keeps only the thin routing glue.
    scope_routing = _route_scope_expansion_sanction(
        scope_assessment,
        operating_mode=_operating_mode_for_state(state),
        has_active_waiver=bool(_operator_waiver_text(state)),
    )
    scope_gaps = scope_routing.release_gaps(language=task_language)
    all_engine_gaps = engine_gaps + scope_gaps
    if all_engine_gaps:
        reviewed_checks = {
            str(g.get("required_check", "")) for g in verification_gaps
        }
        added = [
            g for g in all_engine_gaps
            if str(g.get("required_check", "")) not in reviewed_checks
        ]
        verification_gaps = verification_gaps + added
        approved = False
        verdict = "REJECTED"
        ship_ready = False
        if engine_gaps:
            body += (
                "\n\n## Engine backstop — required verification unproven\n\n"
                + "\n".join(f"- {g['risk']}" for g in engine_gaps)
            )
        if scope_gaps:
            body += (
                "\n\n## Scope expansion — genuine-safety halt\n\n"
                + "\n".join(f"- {g['risk']}" for g in scope_gaps)
            )

    # Dual-shape phase_log entry (ADR 0025):
    #   * Review-shape mirror — preserved for existing consumers.
    #   * Release fields — first-class new surface.
    findings_mirror = [b.to_finding_dict() for b in parsed.release_blockers]
    state.phase_log["final_acceptance"] = {
        # Review-shape mirror.
        "output":         body,
        "raw_output":     raw,
        "approved":       approved,
        "verdict":        verdict,
        "short_summary":  parsed.short_summary,
        "findings":       findings_mirror,
        # Release fields.
        "ship_ready":         ship_ready,
        "release_blockers":   parsed.blockers_as_dicts(),
        "verification_gaps":  verification_gaps,
        "contract_status":    parsed.contract_status.to_dict(),
        "parse_warnings":     list(parsed.parse_warnings),
        # Book-keeping.
        "meta":           dict(result.meta),
    }
    if engine_gaps:
        state.phase_log["final_acceptance"]["engine_backstop"] = {
            "reason": "required_receipts_unproven",
            "gaps": engine_gaps,
        }
    # F2 canonical durable evidence: the single source of truth is
    # ``phase_log['final_acceptance']['scope_expansion']`` — the phase-end /
    # finalize session sync projects it to
    # ``session['phases']['final_acceptance']['scope_expansion']`` for the DONE
    # summary (T3). Written only when there are out-of-plan items so an ordinary
    # in-scope diff keeps the entry shape byte-identical.
    if scope_assessment.items:
        state.phase_log["final_acceptance"]["scope_expansion"] = (
            scope_assessment.to_dict()
        )
        # ADR 0112 §5: the mode-projected route alongside the classifier fact, so
        # the DONE summary / a later phase-handoff increment can read whether the
        # run rejected (genuine-safety halt) or needs an operator handoff without
        # re-deriving the sanction. Written only when there are out-of-plan items.
        state.phase_log["final_acceptance"]["scope_expansion_sanction"] = (
            scope_routing.to_dict()
        )
    _print_review_preview(state, "final_acceptance", "Final acceptance")

    # ADR 0112 §5 (T3 lifecycle wiring): a HANDOFF-routed scope expansion (a
    # ``pro`` blocker or any ``governed`` expansion) must open a real
    # phase-handoff pause for operator sanction, not just record the route in
    # phase_log. Raising the signal on ``state.phase_handoff_request`` makes the
    # runner break out of the phase walk and the orchestrator pause tail persist
    # ``meta.phase_handoff`` + ``awaiting_phase_handoff``. Genuine-safety HALT
    # items still reject above via the release-gap path; an active waiver leaves
    # the assessment empty so this is a no-op. Thin glue — the build/raise logic
    # lives in the focused scope_expansion_support helper (architecture fitness).
    _raise_scope_expansion_handoff(state, scope_routing, last_output=body)

    if not approved:
        # Surface the critique without halting so callers can post-process.
        # ADR 0022 narrowed the soft-fail halt knob to validate_plan only;
        # final_acceptance no longer halts on a well-formed REJECTED verdict.
        # ADR 0025 Phase 1 preserves this: REJECTED release verdict does
        # not change run status — the operator decides whether to
        # relitigate via review_changes / repair_changes.
        state.last_critique = body
    return state
