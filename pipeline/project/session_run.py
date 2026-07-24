"""Coordinator backing the typed project orchestration boundary.

Extracted from :mod:`pipeline.project.app` so that the app module stays a
thin typed facade. This module owns the run wiring: it resolves the
profile / runtime, builds the run state, constructs the
:class:`pipeline.project.run._PipelineRun`, dispatches it, and returns
``(session, output_dir, session_ts)`` so the typed boundary can surface
real run identifiers without guesswork.

The body is decomposed into focused stages threaded through a single
typed context object (:class:`_ProjectRunContext`) instead of a long
local-variable train:

* :func:`_resolve_profile_runtime` — profile resolution, ``project_path``
  check, run id + logging, ``load_plugin``, runtime/model selection, and
  the pipeline header.
* :func:`_resolve_state` — isolation inputs, session init, isolation
  (with its early-halt return), checkpoint/metrics, session-seed
  application, and :class:`PipelineState` construction.
* :func:`_build_and_dispatch` — :class:`_PipelineRun` construction and
  dispatch via the v2 profile.

``load_plugin`` lives here (re-exported from :mod:`pipeline.plugins`) so
the ``pipeline.project.session_run.load_plugin`` test-patch surface works
against the site that actually calls it.

Setup responsibility itself stays in the focused setup modules this
module composes (``profile_setup`` / ``runtime_setup`` / ``run_setup`` /
``isolation_setup`` / ``state_setup``); this coordinator only sequences
them.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from pipeline.plugins import load_plugin
from pipeline.project.isolation_setup import (
    resolve_isolation_inputs,
    setup_isolation,
)
from pipeline.project.profile_dispatch import (
    dispatch_via_v2_profile as _dispatch_via_v2_profile,
)
from pipeline.project.profile_setup import setup_profile
from pipeline.project.run import _PipelineRun
from pipeline.project.run_setup import (
    init_run_session,
    print_pipeline_header,
    project_verification_contract,
    resolve_phase_identities,
    setup_checkpoint_and_metrics,
    setup_run_id,
)
from pipeline.project.runtime_setup import (
    apply_default_mode_projection,
    apply_session_seeds,
    setup_runtime,
)
from pipeline.project.state_setup import (
    StateInputs,
    build_pipeline_state,
)
from pipeline.project.types import PresentationPolicy, ProjectRunRequest

__all__ = ["load_plugin", "run_project_pipeline_session"]


@dataclasses.dataclass
class _ProjectRunContext:
    """Resolved run state threaded across the coordinator stages.

    Built incrementally: :func:`_resolve_profile_runtime` fills the
    profile/project/runtime fields; :func:`_resolve_state` fills the
    session/isolation/checkpoint/state fields (or flips ``halted``).
    """

    # ── profile_setup ────────────────────────────────────────────────
    v2_profile: Any
    resolved_profile_name: str
    projected_profile_name: str | None
    from_run_plan_loaded: Any
    from_run_plan_stripped: Any
    plan_source: str
    cross_handoff_text: Any
    cross_declared_files: tuple[str, ...]
    change_handoff: Any
    do_plan: bool
    do_build: bool
    do_review: bool
    max_rounds: int
    # ── project + runtime ────────────────────────────────────────────
    project_path: Path
    session_ts: str
    plugin: Any
    provider: Any
    plan_model: str
    implement_model: str
    repair_model: str
    repair_escalation_model: str
    review_model: str
    chain_same_model_only: bool
    phase_config: Any
    agent_registry: Any
    # ── verification contract (read-only Stage 1 projection) ─────────
    verification_contract: Any = None
    # ── isolation / session / checkpoint / state ─────────────────────
    session: dict | None = None
    git_cwd: Any = None
    worktree_ctx: Any = None
    wt_cvar_token: Any = None
    sb_cvar_token: Any = None
    metrics: Any = None
    ckpt: Any = None
    state: Any = None
    codemap: Any = None
    halted: bool = False


def _read_persisted_runtime_override(
    output_dir: Path | None,
) -> dict[str, str] | None:
    """Read a persisted operator runtime/model override (ADR 0101 / T2).

    The override is fixed into the run dir's ``meta.json`` by
    :meth:`sdk.run_control.service.RunService.resume` *before* resume. Read it
    here — at the very top of runtime resolution, ahead of ``setup_run_id``
    (which rewrites ``meta.json`` from the fresh session dict) — so a CHECKPOINT
    resume that re-enters its own run dir still sees the operator's decision.

    Tolerant + opt-in: a missing run dir / ``meta.json`` / record yields
    ``None``, so a fresh run (no meta yet) and a plain resume (no record) are
    both strict no-ops — the override activates only when a record exists.
    """
    if output_dir is None:
        return None
    meta_file = Path(output_dir) / "meta.json"
    if not meta_file.is_file():
        return None
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    from sdk.run_control.runtime_override import read_runtime_override

    return read_runtime_override(meta)


def _is_fresh_explicit_start(request: ProjectRunRequest) -> bool:
    """True only for a brand-new top-level run (no resume / follow-up lineage).

    The ``ORCHO_PIPELINE`` A/B env override is honoured exclusively on a
    fresh explicit start. A checkpoint resume (``resume_from``), a
    follow-up (``resume_mode == 'followup'`` / ``followup_parent_run_id``),
    or a cross sub-pipeline / lineage child (``parent_run_id``) all inherit
    their durable profile via ``resolve_resume_profile`` upstream, so an
    ambient env value must not silently displace it here.
    """
    return (
        request.resume_from is None
        and request.parent_run_id is None
        and request.followup_parent_run_id is None
        and request.resume_mode != "followup"
    )


def _resolve_profile_runtime(request: ProjectRunRequest) -> _ProjectRunContext:
    """Resolve profile, run id, plugin, runtime/models, and the header.

    Resolves the run's profile and every value derived from it (profile
    name, ``--from-run-plan`` parent plan + planning-block projection,
    session-split override, cross/change handoff, mode gates) BEFORE
    ``run.start`` logging — the projection runs upstream of the
    ``run.start`` emit so the projected profile name reaches both the
    event and ``meta.json`` together, and a planning-only profile is
    refused before any run-dir I/O. ``plan_source`` may be overridden
    (``local`` → ``run``) on the ``--from-run-plan`` path; the resolved
    value is threaded forward into logging, session init, and dispatch.

    ``phase_config`` is synthesized (idempotent for a supplied config)
    before the header so the Pipeline block can surface the ``[Claude]``
    / ``[Codex]`` chip per phase. The header banner + ``Run dir`` line
    render only under TERMINAL (the gate lives in
    ``print_pipeline_header``).
    """
    # ADR 0101 / T2: read the persisted operator runtime/model override BEFORE
    # ``setup_run_id`` rewrites ``meta.json`` from the fresh session dict, so a
    # resume into this run dir still sees the decision. ``None`` on a fresh run
    # or a plain resume keeps behaviour unchanged.
    runtime_override = _read_persisted_runtime_override(request.output_dir)

    _profile = setup_profile(
        profile_name=request.profile_name,
        profile_obj=request.profile_obj,
        from_run_plan_parent_dir=request.from_run_plan_parent_dir,
        plan_source=request.plan_source,
        handoff_path=request.handoff_path,
        max_rounds=request.max_rounds,
        presentation=request.presentation,
        allow_env_override=_is_fresh_explicit_start(request),
    )

    project_path = Path(request.project_dir).resolve()
    if not project_path.exists():
        from sdk.errors import ProjectNotFound
        raise ProjectNotFound(f"Project not found: {project_path}")

    session_ts = setup_run_id(
        task=request.task, project_dir=request.project_dir,
        resume_from=request.resume_from,
        output_dir=request.output_dir,
        profile_name=_profile.resolved_profile_name,
        parent_run_id=request.parent_run_id,
        project_alias=request.project_alias,
        plan_source=_profile.plan_source,
        projected_profile=_profile.projected_profile_name,
        presentation=request.presentation,
        preallocated_output_dir=request.preallocated_output_dir,
    )

    plugin = load_plugin(str(project_path))
    if request.ma_artifacts_dir_override is not None:
        plugin = dataclasses.replace(
            plugin, ma_artifacts_dir=request.ma_artifacts_dir_override,
        )
    elif request.output_dir is not None:
        plugin = dataclasses.replace(plugin, ma_artifacts_dir=str(request.output_dir))

    _runtime = setup_runtime(
        phase_config=request.phase_config,
        provider=request.provider,
        model=request.model,
        runtime_override=runtime_override,
        skill_trust=plugin.skill_trust,
    )

    # Read-only Stage 1 verification-contract projection. Validate ONCE,
    # unconditionally, here — between load_plugin and the header, and NOT under
    # the presentation gate (so a declared-but-invalid contract fails fast under
    # SILENT too, where the header never prints). Returns None when no contract
    # is declared, keeping the no-contract path byte-identical.
    verification_contract = project_verification_contract(plugin)
    # Default-mode projection (T6): once the resolved profile is known,
    # fill the contract's ``work_mode`` from ``profile.default_mode`` when no
    # explicit work_mode was declared. An explicit project/contract work_mode
    # is preserved; the per-run CLI override (``orcho run --mode``, carried via
    # ``ORCHO_WORK_MODE``) wins over both. No-op when no contract is declared.
    import os
    verification_contract = apply_default_mode_projection(
        verification_contract,
        profile=_profile.v2_profile,
        cli_mode=os.environ.get("ORCHO_WORK_MODE") or None,
    )

    # Account-identity diagnostics (best-effort, diagnostic only). Probe ONLY
    # on the real TERMINAL run-setup path — never on dry-run or non-TERMINAL
    # surfaces — so runtime construction / profile listing / dry-run stay
    # side-effect free. The probe swallows its own failures; a miss just omits
    # the hint and never blocks the run.
    _identity_enabled = (
        request.presentation is PresentationPolicy.TERMINAL
        and not request.dry_run
    )
    phase_identities = resolve_phase_identities(
        _runtime.phase_config, enabled=_identity_enabled,
    )

    print_pipeline_header(
        presentation=request.presentation,
        project_path=project_path, task=request.task,
        plan_model=_runtime.plan_model,
        implement_model=_runtime.implement_model,
        review_model=_runtime.review_model,
        profile_name=_profile.resolved_profile_name,
        session_mode=request.session_mode,
        max_rounds=_profile.max_rounds, do_plan=_profile.do_plan, plugin=plugin,
        output_dir=request.output_dir,
        parent_run_id=request.parent_run_id,
        project_alias=request.project_alias,
        followup_parent_run_id=request.followup_parent_run_id,
        followup_base_task=request.followup_base_task,
        followup_parent_status=request.followup_parent_status,
        followup_child_status=request.followup_child_status,
        followup_active_handoff_id=request.followup_active_handoff_id,
        profile_obj=_profile.v2_profile,
        phase_config=_runtime.phase_config,
        phase_identities=phase_identities,
        resume_from=request.resume_from,
        contract=verification_contract,
    )

    return _ProjectRunContext(
        v2_profile=_profile.v2_profile,
        resolved_profile_name=_profile.resolved_profile_name,
        projected_profile_name=_profile.projected_profile_name,
        from_run_plan_loaded=_profile.from_run_plan_loaded,
        from_run_plan_stripped=_profile.from_run_plan_stripped,
        plan_source=_profile.plan_source,
        cross_handoff_text=_profile.cross_handoff_text,
        cross_declared_files=_profile.cross_declared_files,
        change_handoff=_profile.change_handoff,
        do_plan=_profile.do_plan,
        do_build=_profile.do_build,
        do_review=_profile.do_review,
        max_rounds=_profile.max_rounds,
        project_path=project_path,
        session_ts=session_ts,
        plugin=plugin,
        provider=_runtime.provider,
        plan_model=_runtime.plan_model,
        implement_model=_runtime.implement_model,
        repair_model=_runtime.repair_model,
        repair_escalation_model=_runtime.repair_escalation_model,
        review_model=_runtime.review_model,
        chain_same_model_only=_runtime.chain_same_model_only,
        phase_config=_runtime.phase_config,
        agent_registry=_runtime.agent_registry,
        verification_contract=verification_contract,
    )


def _resolve_state(request: ProjectRunRequest, ctx: _ProjectRunContext) -> None:
    """Resolve isolation, session, checkpoint/metrics, and run state.

    Mutates ``ctx`` in place. ``resolve_isolation_inputs`` runs before
    session init — the correlation id is stamped on the session and
    git_root / parent worktree feed the worktree resolver after.

    ``setup_isolation`` mutates ``session`` in place + ``save_session``
    at the same points as before; ``halted`` reproduces the early
    ``return`` for the pre-run dirty halt / seed_failed branches.
    Worktree / sandbox config errors raise under SILENT and
    print_error + sys.exit(2) under TERMINAL inside the helper. When the
    isolation halts, ``ctx.halted`` is set and the remaining setup is
    skipped.
    """
    _iso_inputs = resolve_isolation_inputs(
        project_path=ctx.project_path,
        from_run_plan_loaded=ctx.from_run_plan_loaded,
        followup_parent_run_id=request.followup_parent_run_id,
        from_run_plan_parent_dir=request.from_run_plan_parent_dir,
        followup_parent_run_dir=request.followup_parent_run_dir,
        resume_from=request.resume_from,
        output_dir=request.output_dir,
    )

    session = init_run_session(
        task=request.task, project_path=ctx.project_path, plugin=ctx.plugin,
        model=request.model,
        profile_name=ctx.resolved_profile_name, session_mode=request.session_mode,
        change_handoff=ctx.change_handoff,
        output_dir=request.output_dir,
        plan_source=ctx.plan_source,
        projected_profile=ctx.projected_profile_name,
        resume_mode=request.resume_mode,
        followup_parent_run_id=request.followup_parent_run_id,
        followup_parent_run_dir=request.followup_parent_run_dir,
        followup_parent_status=request.followup_parent_status,
        followup_base_task=request.followup_base_task,
        plan_source_run_id=_iso_inputs.plan_source_run_id,
    )
    ctx.session = session

    _iso = setup_isolation(
        session=session,
        output_dir=request.output_dir,
        session_ts=ctx.session_ts,
        git_root=_iso_inputs.git_root,
        followup_parent_worktree=_iso_inputs.followup_parent_worktree,
        worktree_config_override=request.worktree_config_override,
        v2_profile=ctx.v2_profile,
        resume_mode=request.resume_mode,
        resume_from=request.resume_from,
        no_interactive=request.no_interactive,
        parent_run_id=request.parent_run_id,
        project_alias=request.project_alias,
        followup_parent_run_id=request.followup_parent_run_id,
        followup_parent_run_dir=request.followup_parent_run_dir,
        worktree_bootstrap_config=ctx.plugin.worktree_bootstrap,
        presentation=request.presentation,
        resume_worktree_decision=_iso_inputs.resume_worktree_decision,
        from_run_plan_parent_dir=request.from_run_plan_parent_dir,
    )
    if _iso.halted:
        ctx.halted = True
        return
    ctx.git_cwd = _iso.git_cwd
    ctx.worktree_ctx = _iso.worktree_ctx
    ctx.wt_cvar_token = _iso.wt_cvar_token
    ctx.sb_cvar_token = _iso.sb_cvar_token

    _run_state = setup_checkpoint_and_metrics(
        plan_model=ctx.plan_model, implement_model=ctx.implement_model,
        review_model=ctx.review_model,
        resume_from=request.resume_from, output_dir=request.output_dir,
        session_ts=ctx.session_ts,
        task=request.task, project_path=ctx.project_path, model=request.model,
        profile_name=ctx.resolved_profile_name, max_rounds=ctx.max_rounds,
        change_handoff=ctx.change_handoff,
        session=session,
        presentation=request.presentation,
    )
    ctx.metrics = _run_state.metrics
    ctx.ckpt = _run_state.ckpt

    # Seed ``agent.session_id`` from parent-meta followup seeds + this run's
    # checkpoint store (checkpoint wins). Called after ``_ckpt`` is built and
    # before state extras so the resolved count lands in ``state.extras``.
    followup_seed_count = apply_session_seeds(
        ctx.phase_config,
        request.followup_session_seeds,
        ctx.ckpt,
    )

    # Resume-only signal for the resume-artifact bootstrap. ``pipeline/
    # checkpoint.py`` persists only the append-only ``completed`` list (there is
    # no separate ``skipped`` set — ``should_skip == in completed``), so
    # ``'plan'`` in ``completed`` means "this resume already finished PLAN" and
    # its durable ``parsed_plan.json`` must be recoverable. Built best-effort and
    # ONLY for an actual resume; a fresh run carries an empty set so the
    # bootstrap stays a strict no-op.
    resume_completed_phases: frozenset[str] = frozenset()
    if request.resume_from and ctx.ckpt is not None:
        try:
            resume_completed_phases = frozenset(
                ctx.ckpt.load(ctx.session_ts).completed or (),
            )
        except Exception:
            resume_completed_phases = frozenset()

    _state_setup = build_pipeline_state(StateInputs(
        task=request.task, project_path=ctx.project_path, plugin=ctx.plugin,
        phase_config=ctx.phase_config, agent_registry=ctx.agent_registry,
        output_dir=request.output_dir, dry_run=request.dry_run, session=session,
        session_ts=ctx.session_ts, git_cwd=ctx.git_cwd,
        change_handoff=ctx.change_handoff, cross_handoff_text=ctx.cross_handoff_text,
        cross_declared_files=ctx.cross_declared_files,
        plan_source=ctx.plan_source, handoff_path=request.handoff_path,
        auto_waiver_allowed=request.auto_waiver_allowed,
        followup_seed_count=followup_seed_count, ckpt=ctx.ckpt,
        attachments=request.attachments, session_mode=request.session_mode,
        implement_model=ctx.implement_model, repair_model=ctx.repair_model,
        repair_escalation_model=ctx.repair_escalation_model,
        chain_same_model_only=ctx.chain_same_model_only,
        presentation=request.presentation,
        render_phase_outputs=request.render_phase_outputs,
        from_run_plan_loaded=ctx.from_run_plan_loaded,
        followup_parent_run_id=request.followup_parent_run_id,
        followup_parent_run_dir=request.followup_parent_run_dir,
        from_run_plan_parent_dir=request.from_run_plan_parent_dir,
        from_run_plan_stripped=ctx.from_run_plan_stripped,
        verification_contract=ctx.verification_contract,
        resume_completed_phases=resume_completed_phases,
        resume_requested=bool(request.resume_from),
    ))
    ctx.state = _state_setup.state
    ctx.codemap = _state_setup.codemap


def _build_and_dispatch(request: ProjectRunRequest, ctx: _ProjectRunContext) -> dict:
    """Build the encapsulated run and dispatch the phase blocks.

    ``default_registry`` is imported lazily to mirror the legacy call
    site. ``_presentation`` threads the run-level presentation policy
    into ``_PipelineRun`` so dispatch / phase / failure / finalize sites
    can consult it; default TERMINAL is preserved for any direct test
    instantiations that don't go through ``run_project_pipeline``.
    """
    from pipeline.phases.builtin import default_registry
    registry = default_registry()

    run = _PipelineRun(
        task=request.task, project_path=ctx.project_path, git_cwd=ctx.git_cwd,
        plugin=ctx.plugin,
        output_dir=request.output_dir, dry_run=request.dry_run,
        profile_name=ctx.resolved_profile_name,
        session_mode=request.session_mode, max_rounds=ctx.max_rounds,
        no_interactive=request.no_interactive,
        unattended=request.unattended,
        plan_model=ctx.plan_model, implement_model=ctx.implement_model,
        repair_model=ctx.repair_model,
        repair_escalation_model=ctx.repair_escalation_model,
        review_model=ctx.review_model,
        do_plan=ctx.do_plan, do_build=ctx.do_build, do_review=ctx.do_review,
        _provider=ctx.provider, phase_config=ctx.phase_config, state=ctx.state,
        registry=registry, session=ctx.session, session_ts=ctx.session_ts,
        codemap=ctx.codemap, _metrics=ctx.metrics, _ckpt=ctx.ckpt,
        _chain_same_model_only=ctx.chain_same_model_only,
        parent_run_id=request.parent_run_id, project_alias=request.project_alias,
        hypothesis_enabled=request.hypothesis_enabled,
        checkpoint_resume=bool(request.resume_from),
        worktree_context=ctx.worktree_ctx,
        _worktree_cvar_token=ctx.wt_cvar_token,
        _sandbox_cvar_token=ctx.sb_cvar_token,
        _presentation=request.presentation,
    )
    return _dispatch_via_v2_profile(run, ctx.v2_profile)


def _promote_plan_only_followup(request: ProjectRunRequest) -> ProjectRunRequest:
    """Promote a plan-only follow-up into a ``--from-run-plan`` continuation.

    Single request-assembly chokepoint shared by every transport (CLI, typed,
    MCP) — they all funnel through :func:`run_project_pipeline_session`, so this
    one call decides the promotion uniformly and ahead of ``setup_profile``
    (which loads the parent plan and strips the planning block). When the parent
    left a durable plan artifact but no undelivered diff and the child profile
    keeps phases after planning, ``from_run_plan_parent_dir`` is stamped onto the
    request so the existing ``--from-run-plan`` machinery hydrates the plan and
    starts a fresh worktree from ``implement``. A no-op otherwise.

    Raises :class:`FollowupPlanContinuationError` when the parent IS a plan-only
    continuation candidate but the selected child profile is contradictory
    (plan-only or review-only) — the run is blocked here, before ``setup_profile``,
    rather than proceeding as a false plan-artifact continuation.
    """
    from pipeline.project.followup_worktree import resolve_followup_plan_promotion

    # A rejected/fix parent is a retained-change recovery candidate, never a
    # plan-artifact continuation.  Keep the generic plan-only promotion below
    # unchanged for genuine plan parents, but do not let a missing/clean
    # correction worktree silently turn into ``from_run_plan``.
    if request.resume_mode == "followup" and request.followup_parent_run_dir:
        from pipeline.control.continuation import resolve_continuation_decision

        parent_dir = Path(request.followup_parent_run_dir)
        try:
            parent_meta = json.loads((parent_dir / "meta.json").read_text())
        except (OSError, ValueError):
            parent_meta = None
        decision = resolve_continuation_decision(
            run_id=parent_dir.name, meta=parent_meta, parent_run_dir=parent_dir,
        )
        if decision.continuation_subject == "retained_change":
            return request

    promoted = resolve_followup_plan_promotion(
        resume_mode=request.resume_mode,
        explicit_from_run_plan_parent_dir=request.from_run_plan_parent_dir,
        followup_parent_run_dir=request.followup_parent_run_dir,
        profile_name=request.profile_name,
        profile_obj=request.profile_obj,
        project_dir=request.project_dir,
    )
    if promoted is None:
        return request
    return dataclasses.replace(request, from_run_plan_parent_dir=promoted)


def run_project_pipeline_session(
    request: ProjectRunRequest,
) -> tuple[dict, Path | None, str]:
    """Coordinator backing the typed orchestration boundary.

    Wires the focused setup modules in run order, builds the
    :class:`pipeline.project.run._PipelineRun`, dispatches it, and
    returns the persisted session plus the **actual** run identifiers
    (``output_dir`` may have been auto-resolved from ``runs_dir`` /
    workspace inference; ``session_ts`` comes from
    :func:`pipeline.project.run_setup.setup_run_id`) so the typed
    boundary surfaces them in :class:`ProjectRunResult` without
    guesswork. The isolation early-halt path returns the persisted
    session with the resolved identifiers, mirroring the pre-extraction
    body.

    The run output dir is materialised here — once, idempotently — so
    every transport (CLI, MCP, direct library calls) shares one creation
    point ahead of the first ``run.start`` / ``phase.start`` emit and the
    ``meta.json`` / ``events.jsonl`` writes that follow. A follow-up run
    that reuses the parent's physical worktree never materialises a child
    ``checkout``; this still guarantees its own child run dir exists for
    run metadata regardless of that worktree reuse.
    """
    if request.output_dir is not None:
        request.output_dir.mkdir(parents=True, exist_ok=True)
    request = _promote_plan_only_followup(request)
    ctx = _resolve_profile_runtime(request)
    from pipeline.project.resume_control import (
        ResumeControlError,
        materialize_resume_control_refusal,
        read_resume_refusal_provenance,
    )

    provenance = read_resume_refusal_provenance(
        request.output_dir if request.resume_from else None,
    )
    try:
        _resolve_state(request, ctx)
        if ctx.halted:
            return ctx.session, request.output_dir, ctx.session_ts
        session = _build_and_dispatch(request, ctx)
    except ResumeControlError as error:
        session = materialize_resume_control_refusal(
            session=ctx.session,
            output_dir=request.output_dir,
            checkpoint=ctx.ckpt,
            state=ctx.state,
            provenance=provenance,
            error=error,
            task=request.task,
            project_dir=request.project_dir,
        )
    return session, request.output_dir, ctx.session_ts
