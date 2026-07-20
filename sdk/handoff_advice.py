# SPDX-License-Identifier: Apache-2.0
"""Read-only SDK accessor that produces typed advice for a paused phase handoff.

When a run is paused awaiting a phase-handoff decision on a rejected/incomplete
verdict, an operator (or an MCP/Web client) can ask for a one-shot advisory
recommendation: the smallest honest way forward. This module exposes that as a
single thin accessor, :func:`request_handoff_advice`, that REUSES the existing
advisor primitives in :mod:`pipeline.project.handoff_advice` without changing any
of their policy (eligibility, parse, safety, classification).

What it does, in order:

1. Resolve the run (``find_run`` → ``RunNotFound`` / ``NoWorkspace``) and load the
   active ``meta.phase_handoff`` payload (``load_active_phase_handoff``). Validate
   ``handoff_id`` against the active payload and the run's paused status —
   mismatch / absent / not-paused raises :class:`InvalidPhaseHandoffState`.
2. Reconstruct the ``PhaseHandoffRequested`` signal from the payload and check
   eligibility through the existing ``advice_actions_available`` predicate
   (trigger rejected/incomplete, rejected-equivalent verdict, ``retry_feedback``
   offered, a finding or ``last_output`` present). Ineligible → typed error.
3. Rebuild a read-only run with ``state.phase_config`` REUSING the same builders
   start/resume use (``setup_runtime`` for the phase config / agent registry,
   ``build_pipeline_state`` for the state) — but WITHOUT the disk-mutating
   isolation / checkpoint / session-init the live coordinator runs, because this
   accessor must not advance the run.
4. Invoke the read-only advisor (``build_advice_context`` + ``invoke_advisor``,
   ``mutates_artifacts=False``). The ONLY durable write is the advice artifact
   (``write_advice_artifact``); its returned relpath builds the provenance note
   (``build_provenance_note``). Safety comes from ``classify_advice_safety``.

It NEVER writes a ``phase_handoff_decisions/`` artifact, never flips
``meta.status``, never auto-applies the recommendation, and adds no new decision
verb. The returned ``provenance_note`` is the note a follow-up
``phase_handoff_decide(action='retry_feedback', feedback=retry_feedback,
note=provenance_note)`` must carry — but issuing that decision stays the caller's
explicit, separate step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sdk.errors import InvalidPhaseHandoffState
from sdk.phase_handoff import _is_decidable_handoff_status, load_active_phase_handoff
from sdk.runs import _CWD_DEFAULT, find_run, load_meta


@dataclass(frozen=True, slots=True)
class HandoffAdviceSafety:
    """Safety classification copied verbatim from ``classify_advice_safety``."""

    auto_apply_ok: bool
    needs_confirmation: bool
    blocked_reason: str
    waiver_blocked: bool


@dataclass(frozen=True, slots=True)
class HandoffAdviceConflict:
    """Typed projection of an assessment conflict or ambiguity detail."""

    detail: str


@dataclass(frozen=True, slots=True)
class HandoffAdviceResult:
    """Typed advisory recommendation for a paused phase handoff.

    A 1:1 typed view of the advisor's normalised recommendation plus the safety
    classification, the durable advice artifact's relpath, and the provenance
    note a follow-up ``retry_feedback`` decision must carry. ``advice_artifact``
    and ``provenance_note`` are empty ONLY when the advisor response was
    unparseable (handled like the existing dispatch / CI paths: no durable advice
    write, never auto-applied).
    """

    run_id: str
    handoff_id: str
    phase: str
    recommended_action: str
    confidence: str
    rationale: str
    retry_feedback: str
    risks: tuple[str, ...]
    expected_files: tuple[str, ...]
    operator_note: str
    parse_warnings: tuple[str, ...]
    safety: HandoffAdviceSafety
    disposition: str
    conflicts: tuple[HandoffAdviceConflict, ...]
    advice_artifact: str
    provenance_note: str
    usage: dict[str, Any] = field(default_factory=dict)


def request_handoff_advice(
    run_id: str,
    handoff_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
    provider: Any | None = None,
    agent: Any | None = None,
) -> HandoffAdviceResult:
    """Produce a typed advisory recommendation for a run's paused phase handoff.

    ``provider`` threads into the runtime build so the advisor resolves through
    the same provider the run used (a ``MockAgentProvider`` under ``--mock``, the
    real provider otherwise); ``agent`` injects the advisor agent directly
    (tests). Exactly one durable write happens — the advice artifact — and no
    decision is recorded.

    Raises:
        RunNotFound / NoWorkspace: from ``find_run`` (propagated).
        InvalidPhaseHandoffState: no active handoff, a mismatched ``handoff_id``,
            a run that is not paused on a decidable handoff, or an ineligible
            handoff (wrong trigger/verdict, no ``retry_feedback`` offered, no
            finding/last_output).
    """
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("request_handoff_advice: run_id must be a non-empty string")

    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    resolved_run_id = ref.run_dir.name
    meta = load_meta(ref.run_dir)
    status = meta.get("status") if isinstance(meta, dict) else None
    payload = load_active_phase_handoff(
        run_id,
        workspace=workspace,
        runs_dir=runs_dir,
        cwd=cwd,
    )

    if payload is None:
        raise InvalidPhaseHandoffState(
            f"request_handoff_advice: run {resolved_run_id} has no active phase "
            f"handoff (meta.status={status!r}). Advice is only available while a "
            "run is paused awaiting a phase-handoff decision."
        )
    active_id = payload.get("id")
    if not isinstance(active_id, str) or not active_id:
        raise InvalidPhaseHandoffState(
            f"request_handoff_advice: run {resolved_run_id} active phase_handoff "
            "payload is missing a non-empty 'id'. Manual repair of meta.json is "
            "required."
        )
    if handoff_id is not None and handoff_id != active_id:
        raise InvalidPhaseHandoffState(
            f"request_handoff_advice: run {resolved_run_id} active handoff id is "
            f"{active_id!r}; refusing to advise on {handoff_id!r}. Read the "
            "current handoff payload before requesting advice."
        )
    # Reuse the canonical decidability predicate (the decide path's owner) so
    # this read-only accessor never re-derives the (status, active) form: the
    # normal pause, plus a torn ``interrupted`` run that still carries the
    # active payload (validated above to be a dict).
    if not _is_decidable_handoff_status(status, payload):
        raise InvalidPhaseHandoffState(
            f"request_handoff_advice: run {resolved_run_id} is not paused on a "
            f"decidable handoff (meta.status={status!r}). The pause must be in "
            "effect before advice can be produced."
        )

    signal = _reconstruct_signal(payload, resolved_run_id)

    from pipeline.project.handoff_advice import (
        advice_actions_available,
        build_advice_context,
        build_provenance_note,
        hygiene_gate_advice,
        invoke_advisor,
        write_advice_artifact,
    )
    from pipeline.project.handoff_advice_assessment import assess_advice

    if not advice_actions_available(signal):
        raise InvalidPhaseHandoffState(
            f"request_handoff_advice: run {resolved_run_id} handoff "
            f"{active_id!r} is not eligible for advice. An advisory pass requires "
            "a rejected/incomplete trigger with a rejected-equivalent verdict, "
            "'retry_feedback' in available_actions, and a finding or last_output."
        )

    run = _rebuild_readonly_run(
        ref.run_dir,
        meta,
        run_id=resolved_run_id,
        provider=provider,
    )

    ctx = build_advice_context(run, signal)
    hygiene_advice = hygiene_gate_advice(signal)
    if hygiene_advice is not None:
        advice = hygiene_advice
        usage: dict[str, Any] = {}
    else:
        result = invoke_advisor(run, ctx, agent=agent)
        advice = result.advice
        usage = dict(result.usage or {})
    assessment = assess_advice(advice, ctx.contract_snapshot, findings=ctx.findings)

    # Unparseable advisor output is handled like the existing dispatch / CI
    # paths: no durable advice artifact is written and nothing is auto-applied.
    # The typed recommendation (normalised to halt/low by ``parse_advice``) and
    # its safety are still returned so the caller sees why no retry was offered.
    if "advice_unparseable" in advice.parse_warnings:
        relpath = ""
        provenance_note = ""
    else:
        relpath = write_advice_artifact(
            ref.run_dir,
            signal.handoff_id,
            advice,
            ctx,
            usage=usage,
            assessment=assessment,
        )
        provenance_note = build_provenance_note(relpath)

    return HandoffAdviceResult(
        run_id=resolved_run_id,
        handoff_id=signal.handoff_id,
        phase=signal.phase,
        recommended_action=advice.recommended_action,
        confidence=advice.confidence,
        rationale=advice.rationale,
        retry_feedback=advice.retry_feedback,
        risks=tuple(advice.risks),
        expected_files=tuple(advice.expected_files),
        operator_note=advice.operator_note,
        parse_warnings=tuple(advice.parse_warnings),
        safety=HandoffAdviceSafety(
            auto_apply_ok=assessment.auto_apply_ok,
            needs_confirmation=assessment.disposition == "operator_review_required",
            blocked_reason=assessment.blocked_reason,
            waiver_blocked=assessment.blocked_reason == "waiver",
        ),
        disposition=assessment.disposition,
        conflicts=tuple(HandoffAdviceConflict(detail=item) for item in assessment.conflict_details),
        advice_artifact=relpath,
        provenance_note=provenance_note,
        usage=usage,
    )


def _reconstruct_signal(payload: dict[str, Any], run_id: str) -> Any:
    """Rebuild the ``PhaseHandoffRequested`` signal from the active payload.

    The payload keys mirror the signal fields one-for-one (see
    ``pipeline.run_state.handoff.build_handoff_payload``). Any malformed payload
    surfaces as a typed :class:`InvalidPhaseHandoffState`, never a raw
    ValueError/TypeError traceback.
    """
    from pipeline.runtime.handoff import PhaseHandoffRequested
    from pipeline.runtime.roles import PhaseHandoffType

    try:
        handoff_type = PhaseHandoffType(str(payload.get("type") or ""))
        return PhaseHandoffRequested(
            handoff_id=str(payload.get("id") or ""),
            phase=str(payload.get("phase") or ""),
            type=handoff_type,
            trigger=str(payload.get("trigger") or ""),
            verdict=str(payload.get("verdict") or ""),
            approved=bool(payload.get("approved", False)),
            round_extras_key=str(payload.get("round_extras_key") or ""),
            round=int(payload.get("round") or 1),
            loop_max_rounds=int(payload.get("loop_max_rounds") or 1),
            available_actions=tuple(payload.get("available_actions") or ()),
            artifacts=payload.get("artifacts") or {},
            last_output=str(payload.get("last_output") or ""),
        )
    except (TypeError, ValueError) as e:
        raise InvalidPhaseHandoffState(
            f"request_handoff_advice: run {run_id} active phase_handoff payload "
            f"is malformed and cannot be reconstructed: {e}. Manual repair of "
            "meta.json is required."
        ) from e


def _rebuild_readonly_run(
    run_dir: Path,
    meta: dict[str, Any],
    *,
    run_id: str,
    provider: Any | None,
) -> Any:
    """Rebuild a read-only run carrying ``state.phase_config`` from ``run_dir``.

    Reuses the SAME side-effect-free builders the live coordinator composes:
    ``setup_runtime`` (phase config + agent registry, idempotent / IO-free) and
    ``build_pipeline_state`` (state construction + extras hydration). It
    deliberately SKIPS the coordinator's disk-mutating stages (isolation,
    checkpoint/metrics, session init) — those advance the run, which a read-only
    advisory pass must not do. ``ckpt`` is ``None`` so the advisor invoke's only
    optional durable write (a checkpoint session-id) is a no-op.
    """
    from agents.protocols import SessionMode
    from pipeline.plugins import load_plugin
    from pipeline.project.runtime_setup import setup_runtime
    from pipeline.project.state_setup import StateInputs, build_pipeline_state
    from pipeline.project.types import PresentationPolicy

    project_dir = str(meta.get("project") or "")
    project_path = Path(project_dir).resolve() if project_dir else run_dir
    task = str(meta.get("task") or "")
    model = str(meta.get("model") or "")
    # git_cwd only feeds the best-effort ``git status --short`` diff and the
    # invoke cwd; the project path is the read-only-safe checkout to use here.
    git_cwd = str(project_path)

    plugin = load_plugin(str(project_path))

    runtime = setup_runtime(phase_config=None, provider=provider, model=model)

    try:
        session_mode = SessionMode(str(meta.get("session_mode_requested") or ""))
    except ValueError:
        session_mode = SessionMode.AUTO

    state_setup = build_pipeline_state(
        StateInputs(
            task=task,
            project_path=project_path,
            plugin=plugin,
            phase_config=runtime.phase_config,
            agent_registry=runtime.agent_registry,
            output_dir=run_dir,
            dry_run=False,
            session=meta,
            session_ts=run_id,
            git_cwd=git_cwd,
            change_handoff=str(meta.get("change_handoff") or ""),
            cross_handoff_text="",
            plan_source=str(meta.get("plan_source") or "local"),
            handoff_path=None,
            auto_waiver_allowed=False,
            followup_seed_count=0,
            ckpt=None,
            attachments=(),
            session_mode=session_mode,
            implement_model=runtime.implement_model,
            repair_model=runtime.repair_model,
            repair_escalation_model=runtime.repair_escalation_model,
            chain_same_model_only=runtime.chain_same_model_only,
            presentation=PresentationPolicy.SILENT,
            render_phase_outputs=False,
            from_run_plan_loaded=None,
            followup_parent_run_id=None,
            from_run_plan_parent_dir=None,
            from_run_plan_stripped=(),
        )
    )
    try:
        from pipeline.plan_artifacts import load_parsed_plan_artifact

        state_setup.state.parsed_plan = load_parsed_plan_artifact(run_dir)
    except Exception:
        # An absent durable plan remains an explicit no-plan assessment, not an
        # inferred markdown reconstruction.
        pass

    return SimpleNamespace(
        state=state_setup.state,
        git_cwd=git_cwd,
        session_ts=run_id,
        output_dir=run_dir,
    )


__all__ = [
    "HandoffAdviceResult",
    "HandoffAdviceSafety",
    "HandoffAdviceConflict",
    "request_handoff_advice",
]
