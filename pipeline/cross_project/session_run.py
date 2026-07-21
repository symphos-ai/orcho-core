"""Coordinator backing the typed cross-project orchestration boundary.

Extracted from :mod:`pipeline.cross_project.app` (ADR 0047 Phase F) so the
app module stays a thin facade. Sequences the focused setup + domain
modules and returns ``(session, output_dir, run_id)``; never calls
``sys.exit``. Decomposed into phase functions over a single typed context
(:class:`_CrossRunContext`). Presentation gating is threaded **explicitly**
via :class:`_Renderers` (no per-function local-shadow trick) so "no stdout
under SILENT" (ADR 0046 stop #9) holds across the split. The resume
project-phase-handoff decision lives in
:func:`pipeline.cross_project.handoff.resume_project_phase_handoff`.

Import discipline (ADR 0047 D2): MUST NOT import from
:mod:`pipeline.cross_project.orchestrator`, and no cross peer module may
import from :mod:`pipeline.cross_project.app`.
"""

from __future__ import annotations

import dataclasses
import os
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pipeline.cross_project.plan_parser as plan_parser
from core.infra import config
from core.observability import events as _events, phases as _pk
from core.observability.logging import log_phase
from pipeline.cross_project.agent_setup import setup_cross_agents
from pipeline.cross_project.app_types import CrossRunRequest
from pipeline.cross_project.checkpoint import (
    write_cross_checkpoint as _write_cross_checkpoint,
)
from pipeline.cross_project.contract_check import (
    ContractCheckContext,
    run_cross_contract_check,
)
from pipeline.cross_project.handoff import resume_project_phase_handoff
from pipeline.cross_project.planning_loop import (
    CrossPlanningContext as _CrossPlanningContext,
    run_cross_planning as _run_cross_planning,
)
from pipeline.cross_project.profile_setup import setup_cross_profile
from pipeline.cross_project.project_dispatch import (
    DispatchPorts as _DispatchPorts,
    ProjectDispatchContext as _ProjectDispatchContext,
    run_project_dispatch as _run_project_dispatch,
)
from pipeline.cross_project.prompts import (
    _ORCHESTRATOR_ROOT,
    set_orchestrator_root as _set_orchestrator_root,
)
from pipeline.cross_project.rendering import (
    paint,
    render_cross_final_acceptance_block,
    silent_renderers,
)
from pipeline.cross_project.run_setup import (
    _read_plan_file,
    render_cross_pipeline_header,
    setup_cross_run,
)
from pipeline.cross_project.task_plan import normalize_cross_task_plan
from pipeline.cross_project.usage import (
    _capture_invoke_usage,
    _print_cross_checks_usage,
    _print_cross_planning_usage,
    _print_usage_snapshot,
    accumulate_phase_usage as _accumulate_phase_usage,
)
from pipeline.engine import save_session as save_cross_session
from pipeline.engine.hypothesis import run_hypothesis_loop
from pipeline.presentation import PresentationPolicy
from pipeline.project.profile_dispatch import (
    hypothesis_attempts_for_step as _hypothesis_attempts_for_step,
    hypothesis_format_for_step as _hypothesis_format_for_step,
    plan_hypothesis_step as _plan_hypothesis_step,
)

__all__ = ["run_cross_pipeline_session"]


@dataclasses.dataclass
class _Renderers:
    """Presentation-gated render callables (from :func:`silent_renderers`); under SILENT stdout callables are no-ops and ``banner`` forwards ``terminal=False`` so ``log_phase`` still fires (ADR 0046 stop #9)."""
    banner: Callable
    success: Callable
    warn: Callable
    preview: Callable
    render_cross_plan_preview: Callable
    print: Callable
    C: Any


@dataclasses.dataclass
class _CrossRunContext:
    """Resolved cross-run state threaded across the coordinator stages."""
    terminal: bool
    r: _Renderers
    session_ts: str
    cross_mode: str
    session: dict
    run_dir: Path
    cross_ckpt: dict
    profile_setup: Any
    requested_profile: Any
    projection: Any
    child_profile: Any
    plan_agent: Any
    review_agent: Any
    code_model: Any
    provider: Any
    cross_phase_usage: dict
    aliases: list
    common_cwd: str
    cross_hypothesis: str | None = None
    cross_hypothesis_attempts: list = dataclasses.field(default_factory=list)
    global_plan_step: Any = None
    global_validate_step: Any = None
    global_plan_loop: Any = None
    has_global_plan: bool = False
    has_global_validate: bool = False
    effective_plan_rounds: int = 1
    plan_output: str = ""
    plan_approved: bool = False
    plan_review_dict: dict | None = None
    plan_rounds: list = dataclasses.field(default_factory=list)
    skipped_phase0: bool = False
    task_plan: Any = None
    contract_results: Any = None
    contract_check_failed: bool = False
    contract_check_failure_reason: Any = None
    review_projects: Any = None
    review_common_cwd: Any = None
    release_skipped_by_policy: bool = False
    cfa_result: Any = None
    cfa_outcome: Any = None
    delivery_result: Any = None
    # ADR 0112 §1 (increment B): the run-scoped, in-memory ParticipantSet seeded
    # provisionally in ``setup_cross_run`` and bound per-alias post-dispatch. Lives
    # here (not in ``session``) so it is never persisted.
    participant_set: Any = None


def _setup_cross_run(request: CrossRunRequest) -> _CrossRunContext:
    """Resolve profile / run / agents and render the pipeline header."""
    terminal = request.presentation is PresentationPolicy.TERMINAL
    r = _Renderers(*silent_renderers(terminal))
    # Cross prompt builders consult ``_ORCHESTRATOR_ROOT`` (idempotent set).
    _set_orchestrator_root(_ORCHESTRATOR_ROOT)
    _profile_setup = setup_cross_profile(profile_name=request.profile_name)
    _run_setup = setup_cross_run(
        task=request.task,
        projects=request.projects,
        model=request.model,
        output_dir=request.output_dir,
        cross_mode=request.cross_mode,
        resume_from=request.resume_from,
        resume_mode=request.resume_mode,
        followup_parent_run_id=request.followup_parent_run_id,
        followup_parent_run_dir=request.followup_parent_run_dir,
        followup_parent_status=request.followup_parent_status,
        followup_base_task=request.followup_base_task,
        resumed_meta=request.resumed_meta,
        profile_setup=_profile_setup,
        terminal=terminal,
    )
    try:
        _eff = config.AppConfig.load().phase_effort_map or {}
    except Exception:
        _eff = {}
    _agent_setup = setup_cross_agents(
        provider=request.provider,
        phase_config=request.phase_config,
        profile_setup=_profile_setup,
        cross_mode=_run_setup.cross_mode,
        model=request.model,
        projects=request.projects,
        effort_map=_eff,
    )
    render_cross_pipeline_header(
        terminal=terminal,
        run_dir=_run_setup.run_dir,
        task=request.task,
        projects=request.projects,
        agents_block=_agent_setup.agents_block,
        project_agents_block=_agent_setup.project_agents_block,
        pipeline_runtimes=_agent_setup.pipeline_runtimes,
        projection=_profile_setup.projection,
        cross_mode=_run_setup.cross_mode,
        max_rounds=request.max_rounds,
        requested_profile_name=_profile_setup.requested_profile.name,
        contract_gate_policy=_profile_setup.contract_gate_policy,
        cfa_gate_policy=_profile_setup.cfa_gate_policy,
        resume_from=request.resume_from,
        followup_parent_run_id=request.followup_parent_run_id,
        followup_base_task=request.followup_base_task,
    )
    return _CrossRunContext(
        terminal=terminal,
        r=r,
        session_ts=_run_setup.session_ts,
        cross_mode=_run_setup.cross_mode,
        session=_run_setup.session,
        run_dir=_run_setup.run_dir,
        cross_ckpt=_run_setup.cross_ckpt,
        profile_setup=_profile_setup,
        requested_profile=_profile_setup.requested_profile,
        projection=_profile_setup.projection,
        child_profile=_profile_setup.child_profile,
        plan_agent=_agent_setup.plan_agent,
        review_agent=_agent_setup.review_agent,
        code_model=_agent_setup.code_model,
        provider=_agent_setup.provider,
        cross_phase_usage={},
        aliases=list(request.projects.keys()),
        common_cwd=os.path.commonpath([str(p) for p in request.projects.values()]),
        participant_set=_run_setup.participant_set,
    )


def _run_cross_hypothesis(request: CrossRunRequest, ctx: _CrossRunContext) -> None:
    """Run the pre-plan CROSS-HYPOTHESIS gut-check loop (skipped on dry_run / no hypothesis step); persists the phase record and accumulates token usage."""
    r = ctx.r
    _global_profile = SimpleNamespace(steps=ctx.projection.global_steps)
    _hypothesis_step = _plan_hypothesis_step(
        _global_profile,
        override_enabled=request.hypothesis_enabled,
    )
    if request.dry_run or _hypothesis_step is None:
        return
    r.banner("CROSS_HYPOTHESIS", "CROSS-HYPOTHESIS — fast pre-plan gut-check",
             r.C.MAGENTA, phase_kind=_pk.HYPOTHESIS, attempt=1)
    _hyp_t0 = time.time()
    cross_hypothesis, _attempts = run_hypothesis_loop(
        ctx.plan_agent, ctx.review_agent,
        request.task, ctx.common_cwd, "",
        _hypothesis_attempts_for_step(_hypothesis_step),
        prompt_spec=getattr(_hypothesis_step, "prompt", None),
        hypothesis_format=_hypothesis_format_for_step(_hypothesis_step),
    )
    _hyp_dt = time.time() - _hyp_t0
    ctx.cross_hypothesis = cross_hypothesis
    ctx.cross_hypothesis_attempts = _attempts or []
    ctx.session["phases"]["hypothesis"] = {
        "enabled": True,
        "approved": cross_hypothesis,
        "attempts": _attempts,
    }
    _hyp_in = sum(int(a.get("plan_usage", {}).get("tokens_in", 0))
                 + int(a.get("qa_usage", {}).get("tokens_in", 0))
                 for a in _attempts)
    _hyp_out = sum(int(a.get("plan_usage", {}).get("tokens_out", 0))
                  + int(a.get("qa_usage", {}).get("tokens_out", 0))
                  for a in _attempts)
    _hyp_cost = 0.0
    if config.accounting_enabled():
        for a in _attempts:
            for slot in ("plan_usage", "qa_usage"):
                c = a.get(slot, {}).get("cost_usd")
                if c is not None:
                    _hyp_cost += float(c)
    _hyp_usage: dict = {
        "tokens_in":   _hyp_in,
        "tokens_out":  _hyp_out,
        "total_tokens": _hyp_in + _hyp_out,
        "duration_s":  _hyp_dt,
        "token_split_estimated": False,
        "token_split_source": "exact",
    }
    if _hyp_cost > 0:
        _hyp_usage["cost_usd_equivalent"] = _hyp_cost
        _hyp_usage["cost_estimated"] = False
    _accumulate_phase_usage(
        ctx.cross_phase_usage, "cross_hypothesis", _hyp_usage,
    )
    log_phase(
        "CROSS_HYPOTHESIS", "CROSS-HYPOTHESIS — fast pre-plan gut-check",
        "END",
        ("direction validated" if cross_hypothesis
         else "all rejected" if _attempts
         else "skipped"),
        phase_kind=_pk.HYPOTHESIS, attempt=1,
    )
    _print_usage_snapshot(
        "cross_hypothesis", ctx.cross_phase_usage["cross_hypothesis"],
        terminal=ctx.terminal,
    )


def _resolve_global_plan_steps(ctx: _CrossRunContext) -> None:
    """Locate the global cross_plan / cross_validate_plan steps."""
    from pipeline.runtime import LoopStep as _LoopStep, PhaseStep as _PhaseStep

    def _find_handler(steps, handler_name):
        for s in steps:
            if (isinstance(s, _PhaseStep)
                    and s.cross is not None
                    and s.cross.handler == handler_name):
                return s
        return None
    for _entry in ctx.projection.global_steps:
        if isinstance(_entry, _LoopStep):
            inner_plan = _find_handler(_entry.steps, "cross_plan")
            if inner_plan is not None:
                ctx.global_plan_loop = _entry
                ctx.global_plan_step = inner_plan
                ctx.global_validate_step = _find_handler(
                    _entry.steps, "cross_validate_plan",
                )
                break
        elif isinstance(_entry, _PhaseStep) and _entry.cross is not None:
            if _entry.cross.handler == "cross_plan":
                ctx.global_plan_step = _entry
            elif _entry.cross.handler == "cross_validate_plan":
                ctx.global_validate_step = _entry
    ctx.has_global_plan = ctx.global_plan_step is not None
    ctx.has_global_validate = ctx.global_validate_step is not None
    ctx.effective_plan_rounds = (
        ctx.global_plan_loop.max_rounds if ctx.global_plan_loop is not None else 1
    )


def _run_planning(request: CrossRunRequest, ctx: _CrossRunContext) -> bool:
    """Run the cross planning loop and materialize plan artifacts; returns ``True`` when the coordinator should stop (planning paused/halted, or ``plan``-mode terminal return)."""
    r = ctx.r
    pre_existing_plan = _read_plan_file(request.plan_file, terminal=ctx.terminal)
    from pipeline.runtime.roles import PhaseHandoffType as _PhaseHandoffType
    _validate_handoff_fires = (
        ctx.global_validate_step is not None
        and ctx.global_validate_step.handoff is not None
        and ctx.global_validate_step.handoff.type
            is _PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT
    )
    _planning_ctx = _CrossPlanningContext(
        task=request.task,
        projects=request.projects,
        aliases=ctx.aliases,
        run_dir=ctx.run_dir,
        output_dir=request.output_dir,
        common_cwd=ctx.common_cwd,
        dry_run=request.dry_run,
        resume_from=request.resume_from,
        resumed_meta=request.resumed_meta,
        plan_file_label=request.plan_file,
        pre_existing_plan_text=pre_existing_plan,
        plan_agent=ctx.plan_agent,
        review_agent=ctx.review_agent,
        cross_hypothesis=ctx.cross_hypothesis,
        cross_hypothesis_attempts=ctx.cross_hypothesis_attempts,
        has_global_plan=ctx.has_global_plan,
        has_global_validate=ctx.has_global_validate,
        plan_loop_max_rounds=ctx.effective_plan_rounds,
        validate_handoff_fires=_validate_handoff_fires,
        requested_profile_name=ctx.requested_profile.name,
        orchestrator_root=_ORCHESTRATOR_ROOT,
        cross_ckpt=ctx.cross_ckpt,
        session=ctx.session,
        cross_phase_usage=ctx.cross_phase_usage,
        terminal=ctx.terminal,
    )
    _planning_result = _run_cross_planning(_planning_ctx)
    if _planning_result.status in ("paused", "halted"):
        return True
    ctx.plan_output = _planning_result.plan_output
    ctx.plan_approved = _planning_result.plan_approved
    ctx.plan_review_dict = _planning_result.plan_review_dict
    ctx.plan_rounds = _planning_result.plan_rounds
    ctx.skipped_phase0 = _planning_result.skipped_phase0
    if ctx.has_global_plan:
        ctx.session["phases"]["cross_plan"] = {
            "output":   ctx.plan_output,
            "run_dir":  str(ctx.run_dir),
            "rounds":   ctx.plan_rounds,
            "approved": ctx.plan_approved,
        }
    # ADR 0054: render .md from JSON (fallback for paths planning_loop missed).
    if (ctx.has_global_plan and not request.dry_run and not ctx.skipped_phase0
            and not (ctx.run_dir / "cross_plan.md").exists()):
        _fallback = plan_parser.parse_cross_plan(ctx.plan_output, ctx.aliases)
        plan_parser.write_cross_plan_artifacts(
            ctx.run_dir, _fallback, task=request.task,
            projects=request.projects, aliases=ctx.aliases,
        )
    ctx.cross_ckpt["phase0_done"] = True
    _write_cross_checkpoint(ctx.run_dir, ctx.cross_ckpt)
    r.success(f"Run dir: {ctx.run_dir}")
    _print_cross_planning_usage(ctx.cross_phase_usage, terminal=ctx.terminal)
    if request.dry_run or not ctx.has_global_plan:
        ctx.task_plan = None
    else:
        ctx.task_plan = normalize_cross_task_plan(
            plan_parser.parse_cross_plan(ctx.plan_output, ctx.aliases), ctx.aliases,
        )
    distribution: list[tuple[str, str | None]] = (
        [(u.alias, (u.spec or None)) for u in ctx.task_plan.units]
        if ctx.task_plan else [(a, None) for a in ctx.aliases]
    )
    r.print()
    r.print(paint(
        f"  Distribution: {len(distribution)} subtasks → "
        f"{len(ctx.aliases)} projects",
        r.C.MAGENTA, r.C.BOLD,
    ))
    for alias, subtask in distribution:
        if subtask:
            line = subtask.replace("\n", " ")[:140]
            r.print(f"  {paint(f'[{alias}]', r.C.CYAN)}  {line}…")
        else:
            r.warn(f"  Could not parse subtask for [{alias}] — will use full task")
    _plan_path = ctx.run_dir / "cross_plan.md"
    r.print(f"  {paint(f'cross_plan.md: {_plan_path}', r.C.GREY)}")
    if ctx.cross_mode == "plan":
        ctx.session["status"] = "awaiting_human_review"
        r.banner("PLAN COMPLETE", "Cross-plan ready for human review", r.C.GREEN)
        r.success(f"Subtasks extracted for {len(ctx.aliases)} projects")
        log_phase("PLAN", "Cross-plan complete — awaiting human review", "DONE")
        if request.output_dir:
            sf = save_cross_session(ctx.run_dir, ctx.session)
            r.success(f"Session: {sf}")
        return True
    return False


def _run_dispatch_and_contract(request: CrossRunRequest, ctx: _CrossRunContext) -> bool:
    """Dispatch per-project child runs and run the cross contract gate; returns ``True`` when the coordinator should stop (dispatch pause, or contract-check pause/abort)."""
    r = ctx.r
    _dispatch_ports = _DispatchPorts(banner=r.banner, success=r.success, warn=r.warn)
    _dispatch_ctx = _ProjectDispatchContext(
        task=request.task,
        projects=request.projects,
        task_plan=ctx.task_plan,
        resume_from=request.resume_from,
        dry_run=request.dry_run,
        max_rounds=request.max_rounds,
        code_model=ctx.code_model,
        phase_config=request.phase_config,
        child_profile=ctx.child_profile,
        requested_profile_name=ctx.requested_profile.name,
        has_global_plan=ctx.has_global_plan,
        provider=ctx.provider,
        hypothesis_enabled=request.hypothesis_enabled,
        followup_session_seeds_per_alias=request.followup_session_seeds_per_alias,
        run_dir=ctx.run_dir,
        output_dir=request.output_dir,
        plan_output=ctx.plan_output,
        plan_review_dict=ctx.plan_review_dict,
        cross_ckpt=ctx.cross_ckpt,
        session=ctx.session,
        cross_phase_usage=ctx.cross_phase_usage,
        ports=_dispatch_ports,
        terminal=ctx.terminal,
        participant_set=ctx.participant_set,
    )
    _dispatch_result = _run_project_dispatch(_dispatch_ctx)
    if _dispatch_result.paused:
        return True
    if _dispatch_result.blocking_aliases:
        from pipeline.cross_project.gate_entries import child_readiness_contract_entry

        readiness_entries: dict[str, dict] = {}
        children = ctx.session.get("phases", {}).get("projects", {})
        for alias in _dispatch_result.blocking_aliases:
            child = children.get(alias) if isinstance(children, dict) else None
            child_status = (
                child.get("status")
                if isinstance(child, dict) and isinstance(child.get("status"), str)
                else "missing"
            )
            child_reason = (
                str(child.get("halt_reason") or child.get("error") or child_status)
                if isinstance(child, dict)
                else "child_session_missing"
            )
            readiness_entries[alias] = child_readiness_contract_entry(
                alias=alias,
                child_status=child_status,
                child_reason=child_reason,
            )
        ctx.session["phases"]["contract_check"] = readiness_entries
        ctx.contract_results = readiness_entries
        # Readiness is a CFA precondition, not an interface compatibility
        # failure.  Do not trigger contract-rejection finalization semantics.
        ctx.contract_check_failed = False
        ctx.contract_check_failure_reason = None
        # CFA still needs safe, total review targets even though its
        # precondition path will avoid an agent call.
        ctx.review_projects = {alias: Path(path) for alias, path in request.projects.items()}
        ctx.review_common_cwd = (
            os.path.commonpath([str(path) for path in ctx.review_projects.values()])
            if ctx.review_projects else ctx.common_cwd
        )
        return False
    _cc_result = run_cross_contract_check(
        ContractCheckContext(
            task=request.task,
            projects=request.projects,
            session=ctx.session,
            cross_ckpt=ctx.cross_ckpt,
            run_dir=ctx.run_dir,
            output_dir=request.output_dir,
            dry_run=request.dry_run,
            resume_from=request.resume_from,
            terminal=ctx.terminal,
            common_cwd=ctx.common_cwd,
            plan_output=ctx.plan_output,
            codex=ctx.review_agent,
            contract_policy=ctx.profile_setup.contract_gate_policy,
            operator_decisions=request.operator_decisions,
            no_interactive=request.no_interactive,
            cross_phase_usage=ctx.cross_phase_usage,
        )
    )
    ctx.contract_results = _cc_result.contract_results
    ctx.contract_check_failed = _cc_result.contract_check_failed
    ctx.contract_check_failure_reason = _cc_result.contract_check_failure_reason
    ctx.review_projects = _cc_result.review_projects
    ctx.review_common_cwd = _cc_result.review_common_cwd
    return _cc_result.control in ("paused", "aborted")


def _run_release_gate(request: CrossRunRequest, ctx: _CrossRunContext) -> bool:
    """Run the CFA / system release gate: policy-skip entry or CFA evaluation, persisting the verdict (override preserves the REJECTED audit entry; approved/retry persist the real result). Returns ``True`` on terminal ``halted``; the observability tail / pause is in :func:`_finalize_release_verdict`."""
    r = ctx.r
    from pipeline.cross_project.gate_entries import skipped_release_entry
    from pipeline.runtime import CrossGateRunPolicy
    _release_policy = ctx.profile_setup.cfa_gate_policy
    ctx.release_skipped_by_policy = (
        not _release_policy.enabled
        or _release_policy.run is CrossGateRunPolicy.NEVER
    )
    if ctx.release_skipped_by_policy:
        _reason = (
            "policy_disabled"
            if not _release_policy.enabled
            else "policy_never"
        )
        ctx.session["phases"]["cross_final_acceptance"] = skipped_release_entry(
            reason=_reason,
            source="policy",
        )
        r.banner(
            "CROSS_FINAL_ACCEPTANCE",
            f"CROSS-FINAL-ACCEPTANCE — skipped by policy ({_reason})",
            r.C.MAGENTA,
        )
        ctx.cfa_result = None
        return False
    from pipeline.cross_project.cfa_gate import (
        CFA_DEFAULT_MAX_ROUNDS,
        evaluate_cfa_gate,
    )
    from pipeline.cross_project.final_acceptance import (
        build_context,
        result_to_phase_log_entry,
    )
    r.banner(
        "CROSS_FINAL_ACCEPTANCE",
        "CROSS-FINAL-ACCEPTANCE — system release gate",
        r.C.MAGENTA, phase_kind=None, attempt=1,
    )
    _cfa_ctx = build_context(
        cross_plan_markdown=ctx.plan_output,
        aliases=tuple(request.projects.keys()),
        session_phases=ctx.session["phases"],
        common_cwd=str(ctx.review_common_cwd),
        review_paths={a: str(p) for a, p in ctx.review_projects.items()},
    )
    _cfa_outcome = evaluate_cfa_gate(
        cfa_ctx=_cfa_ctx,
        codex=ctx.review_agent,
        dry_run=request.dry_run,
        run_dir=ctx.run_dir,
        session=ctx.session,
        cross_ckpt=ctx.cross_ckpt,
        cross_phase_usage=ctx.cross_phase_usage,
        resume_from=request.resume_from,
        output_dir=bool(request.output_dir),
        terminal=ctx.terminal,
        max_rounds=CFA_DEFAULT_MAX_ROUNDS,
    )
    ctx.cfa_outcome = _cfa_outcome
    # ``halted`` already wrote terminal state; skip the tail and return.
    if _cfa_outcome.outcome == "halted":
        return True
    ctx.cfa_result = _cfa_outcome.cfa_result
    if _cfa_outcome.outcome == "override_continue":
        # Operator override: PRESERVE the REJECTED audit entry, only stamp
        # the override marker (finalizer reads the synthetic result).
        cfa_entry = ctx.session.setdefault("phases", {}).setdefault(
            "cross_final_acceptance", {},
        )
        if isinstance(cfa_entry, dict) and (
            _cfa_outcome.override_marker is not None
        ):
            cfa_entry["override"] = dict(_cfa_outcome.override_marker)
    elif _cfa_outcome.outcome != "paused":
        # approved_terminal / retry_consumed — persist the real verdict.
        ctx.session["phases"]["cross_final_acceptance"] = result_to_phase_log_entry(
            ctx.cfa_result,
        )
    return False


def _finalize_release_verdict(request: CrossRunRequest, ctx: _CrossRunContext) -> bool:
    """Emit the CFA observability tail (token capture + preview + verdict event + log_phase) and handle the pause path; returns ``True`` on pause, re-flushing ``metrics.json`` with the CFA token spend the gate's earlier snapshot omitted."""
    r = ctx.r
    _cfa_result = ctx.cfa_result
    _cfa_outcome = ctx.cfa_outcome
    if _cfa_result.prompt_text:
        _cfa_usage = _capture_invoke_usage(
            ctx.review_agent, _cfa_result.duration_s,
            prompt=_cfa_result.prompt_text,
            output=_cfa_result.raw_output,
            model=getattr(ctx.review_agent, "model", None),
            terminal=ctx.terminal,
        )
        _accumulate_phase_usage(
            ctx.cross_phase_usage, "cross_final_acceptance", _cfa_usage,
        )
        _print_usage_snapshot(
            "cross_final_acceptance", _cfa_usage, terminal=ctx.terminal,
        )
    _cfa_language = str(config.AppConfig.load().task_language or "")
    _cfa_label = (
        "Вердикт cross-final-acceptance"
        if _cfa_language.strip().lower().startswith(("ru", "rus", "russian", "рус"))
        else "Cross-final-acceptance verdict"
    )
    # Render the verdict as a structured, ANSI-styled block (parity with
    # the single-project final_acceptance path) rather than dumping the
    # raw release markdown. ``_cfa_result.rendered`` (the markdown) is
    # still persisted to the phase log / evidence unchanged; only the
    # terminal presentation changes here.
    from pipeline.cross_project.final_acceptance import (
        result_to_phase_log_entry,
    )
    _cfa_block = render_cross_final_acceptance_block(
        result_to_phase_log_entry(_cfa_result),
    )
    r.preview(_cfa_label, _cfa_block, r.C.MAGENTA)
    _print_cross_checks_usage(ctx.cross_phase_usage, terminal=ctx.terminal)
    _events.emit(
        "cross_final_acceptance.verdict",
        approved=_cfa_result.parsed.approved,
        verdict=_cfa_result.parsed.verdict,
        ship_ready=_cfa_result.parsed.ship_ready,
        source=_cfa_result.source,
        short_summary=_cfa_result.parsed.short_summary,
    )
    log_phase(
        "CROSS_FINAL_ACCEPTANCE",
        "CROSS-FINAL-ACCEPTANCE — system release gate",
        "END",
        (f"{_cfa_result.parsed.verdict} (source={_cfa_result.source})"),
        phase_kind=None, attempt=1,
    )
    # Pause already persisted via the gate; exit WITHOUT finalize (no
    # ``run.end``). Re-flush metrics to include the CFA tokens the gate's
    # earlier snapshot omitted (best-effort).
    if _cfa_outcome.outcome == "paused":
        if request.output_dir:
            try:
                import json as _json

                from core.observability.metrics import cross_metrics_dict
                _paused_metrics = cross_metrics_dict({}, ctx.cross_phase_usage)
                (ctx.run_dir / "metrics.json").write_text(
                    _json.dumps(
                        _paused_metrics, indent=2, ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            except Exception:  # noqa: BLE001
                pass
        return True
    return False


def _cross_delivery_plan(ctx: _CrossRunContext) -> tuple[bool, bool]:
    """Classify whether cross-level delivery runs now, and under what override.

    Mirrors the intent of mono ``_session_allows_commit_delivery``
    (:mod:`pipeline.project.run`, ADR 0128): mono gates delivery on the run
    having *finished* (``status == "done"``, or a REJECTED verdict the operator
    is overriding), NOT on whether a release gate was configured. Cross reaches
    :func:`_run_delivery_and_finalize` only after the release gate neither
    halted (``_run_release_gate`` returns early) nor paused
    (``_finalize_release_verdict`` returns early), so a rejected run is already
    intercepted upstream. Both the approved path and the policy-disabled path
    (no ``cross_gates`` in the profile, ``ctx.cfa_outcome is None``) are
    therefore finished and not rejected — so ``should_deliver`` is ``True`` for
    both. A disabled release gate BYPASSES gating; it never suppresses delivery.

    ``override`` is null-safe: ``True`` only on a real CFA override-continue
    outcome. On the policy-skip path ``ctx.cfa_outcome is None`` so ``override``
    is ``False`` — no ``AttributeError`` on ``ctx.cfa_outcome.outcome``.
    """
    from pipeline.cross_project.gate_entries import (
        child_readiness_blocking_aliases,
    )

    # Child readiness is an admission precondition, independent of the CFA
    # policy.  A disabled/never CFA writes a SKIPPED audit entry, but it must
    # not allow delivery to begin for a required child that never reached a
    # terminal-success state.  Finalization repeats this fail-closed decision
    # when setting the durable parent status.
    if child_readiness_blocking_aliases(
        getattr(ctx, "contract_results", {}),
    ):
        return False, False

    override = (
        ctx.cfa_outcome is not None
        and ctx.cfa_outcome.outcome == "override_continue"
    )
    return True, override


def _run_delivery_and_finalize(request: CrossRunRequest, ctx: _CrossRunContext) -> None:
    """Run cross-level commit delivery (BEFORE the sole ``run.end`` emitter) then finalize: SILENT via the silent service, TERMINAL via the wrapper — either emits ``run.end`` once.

    Delivery is decoupled from release-gate policy (ADR 0128): a run reaching
    here is finished and not rejected, so both the approved path and the
    policy-disabled path (gate-less profile, ``ctx.cfa_outcome is None``)
    deliver. The 'deliver now?' decision lives in :func:`_cross_delivery_plan`;
    this stays a thin sequencer.
    """
    should_deliver, override = _cross_delivery_plan(ctx)
    if should_deliver:
        from core.infra import config as _delivery_config
        from pipeline.cross_project.cross_delivery import run_cross_delivery
        ctx.delivery_result = run_cross_delivery(
            session=ctx.session,
            projects=request.projects,
            app_cfg=_delivery_config.AppConfig.load(),
            cross_run_dir=ctx.run_dir,
            terminal=ctx.terminal,
            override=override,
            cross_ckpt=ctx.cross_ckpt,
            release_agent=ctx.review_agent,
        )
    from pipeline.cross_project.finalization import (
        CrossFinalizationContext,
        finalize_cross_run,
        finalize_cross_with_terminal_output,
    )
    _finalization_ctx = CrossFinalizationContext(
        run_dir=ctx.run_dir,
        output_dir=bool(request.output_dir),
        session=ctx.session,
        projects=request.projects,
        max_rounds=request.max_rounds,
        cfa_result=ctx.cfa_result,
        contract_results=ctx.contract_results,
        contract_check_failed=ctx.contract_check_failed,
        contract_check_failure_reason=ctx.contract_check_failure_reason,
        cross_phase_usage=ctx.cross_phase_usage,
        delivery_result=ctx.delivery_result,
        cross_ckpt=ctx.cross_ckpt,
    )
    if ctx.terminal:
        finalize_cross_with_terminal_output(_finalization_ctx)
    else:
        finalize_cross_run(_finalization_ctx)


def run_cross_pipeline_session(
    request: CrossRunRequest,
) -> tuple[dict, Path | None, str]:
    """Coordinator backing the typed cross orchestration boundary; sequences the focused setup + domain modules, returning ``(session, output_dir, run_id)`` for :class:`CrossRunResult`. Never calls ``sys.exit``."""
    ctx = _setup_cross_run(request)
    _run_cross_hypothesis(request, ctx)
    _resolve_global_plan_steps(ctx)
    if (
        request.resume_from
        and ctx.cross_ckpt.get("phase_handoff_pending")
        and ctx.cross_ckpt.get("phase_handoff_kind") == "project"
        and resume_project_phase_handoff(
            cross_ckpt=ctx.cross_ckpt,
            run_dir=ctx.run_dir,
            output_dir=request.output_dir,
            session=ctx.session,
            success=ctx.r.success,
        )
    ):
        return ctx.session, ctx.run_dir, ctx.session_ts
    if _run_planning(request, ctx):
        return ctx.session, ctx.run_dir, ctx.session_ts
    if _run_dispatch_and_contract(request, ctx):
        return ctx.session, ctx.run_dir, ctx.session_ts
    if _run_release_gate(request, ctx):
        return ctx.session, ctx.run_dir, ctx.session_ts
    if not ctx.release_skipped_by_policy and _finalize_release_verdict(request, ctx):
        return ctx.session, ctx.run_dir, ctx.session_ts
    _run_delivery_and_finalize(request, ctx)
    return ctx.session, ctx.run_dir, ctx.session_ts
