"""PipelineState construction + extras hydration for the project pipeline.

The typed project entry surface (``pipeline/project/app.py``) builds a
fresh :class:`PipelineState` per run. This module owns that construction:
the codemap injection, the ``state.extras`` payload (run id, handoff
strategy, models, presentation flags, checkpoint handle), the durable
cross-process extras hydration, CLI / API attachment threading, and the
``--from-run-plan`` plan/markdown hydration, and resume-time
``parsed_plan.json`` hydration.

:func:`build_pipeline_state` takes a typed :class:`StateInputs` (an
aggregate of the upstream setup results, not a long local-variable train)
and returns a :class:`StateSetup` carrying the built ``state`` plus the
resolved ``codemap`` the coordinator threads into ``_PipelineRun``.

Extras hydration: some durable, cross-process state lives only in the
persisted session / ``meta.json`` and must be lifted back into
``state.extras`` so the in-process phase handlers see it after a
fresh-process resume (MCP / Web), where the in-memory state from the
original launch is gone. :func:`hydrate_state_extras_from_session` owns
that lift — today the phase-handoff waiver written by
``continue_with_waiver`` (see ``pipeline.project.handoff``). The resume
branch in the SAME process sets ``state.extras['phase_handoff_waiver']``
directly; this hydration covers the OTHER path — a brand new process that
rehydrates the session from ``meta.json`` and never ran the resume branch
that produced the runtime copy. Both paths must end with the waiver in
``state.extras`` so downstream review gates inject it.

Parsed plan hydration: checkpoint resume can start in a fresh process after
the PLAN phase already persisted ``parsed_plan.json``. In that path the
in-memory ``state.parsed_plan`` from the original process is gone, but DAG
implementation still needs the typed plan. ``build_pipeline_state`` therefore
rehydrates the durable artifact through the generic resume-artifact bootstrap
(:mod:`pipeline.project.resume_artifacts`) when no explicit in-memory plan was
supplied. The bootstrap also writes provenance and, when ``'plan'`` is in the
resume's ``completed_phases``, sets the owned ``RESUME_PLAN_REQUIRED_KEY``
marker; it never raises on a missing/corrupt artifact — the authoritative
operator error lives in the requiring phase (``subtask_dag``), not here.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agents.protocols import SessionMode
from agents.registry import PhaseAgentConfig
from core.context import build_repo_map
from core.infra import config
from core.observability.logging import success
from pipeline.engine.declared_write_scope import (
    DECLARED_WRITE_SCOPE_EXTRAS_KEY,
    resolve_declared_write_scope,
)
from pipeline.plugins import PluginConfig
from pipeline.project.resume_artifacts import bootstrap_resume_artifacts
from pipeline.project.types import PresentationPolicy
from pipeline.runtime import PipelineState
from pipeline.runtime.run_shape import OperatingMode, coerce_operating_mode
from pipeline.verification_receipt_index import (
    VERIFICATION_PARENT_RUNS_EXTRAS_KEY,
)

_PHASE_HANDOFF_WAIVER_KEY = "phase_handoff_waiver"

#: ``state.extras`` key carrying the run's in-memory, run-scoped
#: :class:`pipeline.participants.ParticipantSet` (ADR 0112 §1, increment B). The
#: set is the single run-scoped source of truth Setup / Execution / Control read;
#: it is NOT persisted (the durable form stays ``session['worktree']`` / meta,
#: from which the resolver re-seeds the set on resume / cold paths).
PARTICIPANT_SET_EXTRAS_KEY = "participant_set"


@dataclasses.dataclass(frozen=True)
class StateInputs:
    """Aggregate of upstream setup results needed to build the run state."""

    task: str
    project_path: Path
    plugin: PluginConfig
    phase_config: PhaseAgentConfig
    agent_registry: Any
    output_dir: Path | None
    dry_run: bool
    session: dict
    session_ts: str
    git_cwd: str
    change_handoff: str
    cross_handoff_text: str
    plan_source: str
    handoff_path: str | None
    auto_waiver_allowed: bool
    followup_seed_count: int
    ckpt: Any
    attachments: Any
    session_mode: SessionMode
    implement_model: str
    repair_model: str
    repair_escalation_model: str
    chain_same_model_only: bool
    presentation: PresentationPolicy
    render_phase_outputs: bool
    from_run_plan_loaded: Any | None
    followup_parent_run_id: str | None
    from_run_plan_parent_dir: Path | None
    from_run_plan_stripped: tuple[str, ...]
    # Correction follow-up's parent run output directory (additive, default
    # None). When paired with ``followup_parent_run_id`` it is stamped into
    # ``state.extras`` so the verification readiness / delivery gate can search
    # the parent run for inherited command-receipts (ADR 0089 / T1). A
    # correction follow-up does NOT use ``from_run_plan``, so this is threaded
    # independently of the ``from_run_plan_*`` fields.
    followup_parent_run_dir: Path | str | None = None
    # Read-only Stage 1 verification-contract projection. ``None`` when no
    # contract is declared — the extras keys are then NOT added and the run
    # behaves byte-identically to before.
    verification_contract: Any | None = None
    # Phases the resume already finished, lifted from the checkpoint store's
    # append-only ``completed`` list (see ``session_run``). Empty for a fresh
    # run, which keeps the resume-artifact bootstrap a strict no-op. Drives
    # ``ResumeArtifactSpec.required_when`` — ``'plan'`` here means PLAN already
    # ran in a prior process, so its durable artifact must be recoverable.
    resume_completed_phases: frozenset[str] = frozenset()
    resume_requested: bool = False
    # Typed control field loaded from the canonical cross handoff JSON, never
    # inferred from its rendered prompt text.
    cross_declared_files: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class StateSetup:
    """Built :class:`PipelineState` + the resolved codemap for the run."""

    state: Any
    codemap: str


def build_pipeline_state(inputs: StateInputs) -> StateSetup:
    """Build the run's :class:`PipelineState` (codemap, extras, attachments,
    ``--from-run-plan`` hydration).

    The ``followup_session_seed_count`` is supplied by the caller (computed
    by ``runtime_setup.apply_session_seeds`` after the checkpoint store is
    open) so the order stays checkpoint → seeds → state extras.
    """
    codemap = "" if inputs.dry_run else _codemap_injectable(
        inputs.plugin, str(inputs.project_path),
    )
    if codemap and inputs.presentation is PresentationPolicy.TERMINAL:
        # ADR 0046 Phase C (site 3): codemap success chip is CLI courtesy;
        # silent callers (cross/UI/MCP) suppress.
        success(f"Codemap: {codemap.count(chr(10))+1} lines injected into PLAN prompt")

    state = _make_state(
        task=inputs.task, project_dir=str(inputs.project_path), plugin=inputs.plugin,
        phase_config=inputs.phase_config, registry=inputs.agent_registry,
        output_dir=inputs.output_dir, dry_run=inputs.dry_run,
        extras={
            # M12 trace foundation: every prompt_render entry the FSM
            # writes carries a PhysicalSessionKey whose run_id is read
            # from state.extras here. Before this anchor, the helper
            # fell back to the literal string ``"unknown"`` and M12
            # persistence would inherit that placeholder. The
            # orchestrator owns the run id (the run-directory name),
            # so we anchor it once at state construction.
            "run_id":             inputs.session_ts,
            "codemap":            codemap,
            "git_cwd":            inputs.git_cwd,
            "continue_session":   False,
            "change_handoff":     inputs.change_handoff,
            "cross_handoff":      inputs.cross_handoff_text,
            "plan_source":        inputs.plan_source,
            "handoff_path":       inputs.handoff_path,
            # ADR 0073: operator opt-in mirrored into state so the
            # implement-phase substance-repair handler can choose to
            # auto-waive (record a synthetic waiver and continue) instead
            # of pausing once repair attempts are exhausted.
            "auto_waiver_allowed": inputs.auto_waiver_allowed,
            "followup_session_seed_count": inputs.followup_seed_count,
            # E1: ``_session_aware_invoke`` looks here to persist
            # ``agent.session_id`` after every successful invoke. ``None``
            # for output_dir-less runs (some test fixtures) — the save
            # branch becomes a no-op.
            "_ckpt":              inputs.ckpt,
        },
    )
    # Lift durable cross-process session state into ``state.extras`` so a
    # fresh-process resume (MCP / Web) sees it. Today this rehydrates the
    # ``continue_with_waiver`` waiver from meta/session for downstream
    # review gates; the same-process resume branch sets it directly and
    # hydration does not overwrite that live copy.
    hydrate_state_extras_from_session(state, inputs.session)
    # Phase 4.5: thread CLI / API attachments through state. Handlers opt
    # in to ``render_text_block(state.attachments)`` for prompt injection;
    # IMAGE / BINARY pass through to the runtime kwarg in Phase 7.
    if inputs.attachments:
        state.attachments = tuple(inputs.attachments)
        if inputs.presentation is PresentationPolicy.TERMINAL:
            # ADR 0046 Phase C (site 4): attachments success chip is CLI
            # courtesy; silent callers suppress (state mutation above stays
            # unconditional).
            success(f"Attachments: {len(state.attachments)} threaded into state")

    # Phase 5c step 4: stuff run-level config into state.extras so
    # ``_phase_fix`` handler can self-resolve session_mode + agent
    # escalation per round. v2 dispatch reads these directly; legacy
    # ``run_review_fix_loop`` continues to drive escalation imperatively
    # for now (handler escalation logic guarded on v2 flag — Phase 5d
    # unifies both paths through the handler).
    state.extras["session_mode_initial"] = inputs.session_mode.value
    state.extras["implement_model"] = inputs.implement_model
    state.extras["repair_model"] = inputs.repair_model
    state.extras["repair_escalation_model"] = inputs.repair_escalation_model
    state.extras["chain_same_model_only"] = inputs.chain_same_model_only
    # ADR 0046 Phase F follow-up — propagate the SILENT flag into
    # ``state.extras`` so phase-handler-level transparency blocks
    # (``_print_plan_preview`` / ``_print_implement_summary`` /
    # ``_print_review_preview`` in ``pipeline/phases/builtin/``)
    # can short-circuit under SILENT. Parallel to the existing
    # ``state.dry_run`` gate — same idea, same surface. The flag
    # name has a leading underscore to mark it as orchestrator-private
    # (handlers read it but never mutate it). Set as bool so a missing
    # key defaults to False (= TERMINAL transparency block fires).
    state.extras["_silent"] = inputs.presentation is PresentationPolicy.SILENT
    state.extras["_render_phase_outputs"] = bool(inputs.render_phase_outputs)

    # ADR 0112 §5 (increment D): project the run's resolved OperatingMode ONCE
    # here — the single posture source the in-process sanction sites read via
    # ``run_shape.operating_mode_from_state``: the ``final_acceptance`` scope
    # gate (through ``session_keys``) and the participant-promotion governed
    # route. Without this stamp both sites would fall back to ``fast`` and a
    # ``pro`` / ``governed`` run would silently degrade (a pro blocker would not
    # open phase-handoff; a governed participant-add would be promoted silently
    # instead of routed to ``scope_expansion:participant_add:<repo>``). Stamped
    # as the enum's string value; the reader coerces it back.
    state.extras["operating_mode"] = _resolve_operating_mode(inputs).value

    # ADR 0112 §1/§2 (increment B): seed the run-scoped, IN-MEMORY ParticipantSet
    # with the mono run's single primary participant FIRST, so it is the single
    # source of truth the verification placeholder derivation below reads.
    # ``editable_checkout`` is the resolved run checkout (``git_cwd`` — the isolated
    # per-run worktree under isolation, the project path in degraded isolation-off),
    # ``delivery_target`` the canonical project. The set is additive on
    # ``state.extras`` and not persisted — single-checkout resolution stays
    # byte-identical.
    _seed_mono_participant_set(state, inputs)

    # Read-only Stage 1 verification-contract projection. When a contract is
    # declared, expose the validated object plus a resolved PlaceholderContext
    # under documented keys so phase handlers (T4) can render a limited,
    # per-phase block. The PlaceholderContext's isolated_source is derived from
    # the SAME run-scoped ParticipantSet seeded just above (not an independently
    # built set), so verification dependencies / env and the run-scoped set never
    # diverge. When no contract is declared, neither key is added and state.extras
    # stays byte-identical to before. Nothing here executes verification commands —
    # this is pure projection.
    if inputs.verification_contract is not None:
        state.extras["verification_contract"] = inputs.verification_contract
        state.extras["verification_placeholders"] = _verification_placeholder_context(
            inputs.verification_contract, inputs.project_path, inputs.output_dir,
            checkout=inputs.git_cwd,
            participant_set=state.extras.get(PARTICIPANT_SET_EXTRAS_KEY),
        )
        # Durable scheduled-gate declaration snapshot is created after state /
        # isolation resolution and before any phase hook can resolve selection.
        from pipeline.project.verification_ledger_runtime import initialize

        initialize(state, resume=inputs.resume_requested)

    # Correction follow-up: thread the parent run as a verification-receipt
    # search source so readiness (``build_final_acceptance_readiness`` via
    # ``review_support``) and the delivery gate (``assess_delivery_verification``
    # via ``run.py``) both inherit valid parent command-receipts from ONE
    # extras key (ADR 0089 / T1). Stamped only when BOTH the parent run id and
    # its run dir are known, and independently of the ``from_run_plan`` branch
    # (a correction follow-up does not use ``from_run_plan``). For a fresh run
    # neither is set, so the key is absent and behavior is byte-identical.
    if inputs.followup_parent_run_id and inputs.followup_parent_run_dir:
        state.extras[VERIFICATION_PARENT_RUNS_EXTRAS_KEY] = (
            (inputs.followup_parent_run_id, str(inputs.followup_parent_run_dir)),
        )

    # ``--from-run-plan`` hydration: seed the parent run's parsed plan
    # into ``state.parsed_plan`` (the canonical contract) and the
    # rendered markdown into ``state.plan_markdown`` (presentation /
    # evidence) BEFORE any phase runs. The projected profile already
    # skipped the planning block, so the first phase the runner sees
    # (typically ``implement``) finds the plan ready in state. The
    # extras stamp ``plan_source_run_id`` so evidence / dashboards
    # can correlate the child run to the parent.
    if inputs.from_run_plan_loaded is not None:
        from pipeline.plan_markdown import render_plan_markdown
        state.parsed_plan = inputs.from_run_plan_loaded
        state.plan_markdown = render_plan_markdown(inputs.from_run_plan_loaded)
        if inputs.followup_parent_run_id:
            state.extras["plan_source_run_id"] = inputs.followup_parent_run_id
        else:
            # Fall back to the parent dir name when no explicit
            # parent run id was threaded through (e.g. when an
            # embedder loaded the plan via filesystem path).
            state.extras["plan_source_run_id"] = (
                inputs.from_run_plan_parent_dir.name
            )
        state.extras["from_run_plan_stripped_phases"] = list(
            inputs.from_run_plan_stripped,
        )
        # Make the plan-artifact continuation explicit in state alongside the
        # stripped-phases record (no second hydration — this only labels the
        # already-loaded plan). True for both an explicit ``--from-run-plan``
        # run and a plan-only follow-up promoted to one: the run continues from
        # a durable plan artifact, not a parent worktree.
        state.extras["plan_artifact_continuation"] = True

    # Resume-artifact bootstrap runs AFTER the ``--from-run-plan`` branch so an
    # explicit in-memory plan is seen as already-present and skipped (never
    # overwritten). For a fresh run (empty ``resume_completed_phases``, no
    # artifact) this is a strict no-op: no marker, no provenance, no mutation.
    # A missing/corrupt required artifact is classified and stamped here but
    # NOT raised — the requiring phase (``subtask_dag``) owns that operator
    # error.
    bootstrap_resume_artifacts(
        state,
        inputs.output_dir,
        completed_phases=inputs.resume_completed_phases,
    )

    # Ownership is resolved only from durable typed inputs after every source
    # of a mono ParsedPlan has had a chance to hydrate. Cross children never
    # receive a ParsedPlan: their explicit handoff tuple is sufficient even
    # when empty, and is deliberately independent of prompt prose. A fresh
    # mono run without a plan does not stamp a misleading plugin-only scope.
    if inputs.plan_source == "cross":
        state.extras[DECLARED_WRITE_SCOPE_EXTRAS_KEY] = resolve_declared_write_scope(
            plugin_allowed_modifications=inputs.plugin.allowed_modifications,
            cross_unit_files=inputs.cross_declared_files,
        )
    elif state.parsed_plan is not None:
        state.extras[DECLARED_WRITE_SCOPE_EXTRAS_KEY] = resolve_declared_write_scope(
            state.parsed_plan,
            plugin_allowed_modifications=inputs.plugin.allowed_modifications,
        )

    return StateSetup(state=state, codemap=codemap)


def _resolve_operating_mode(inputs: StateInputs) -> OperatingMode:
    """Project the run's resolved :class:`OperatingMode` for the §5 sanction matrix.

    The single posture projection, mirroring the verification work-mode
    resolution (:func:`pipeline.project.runtime_setup.apply_default_mode_projection`).
    Priority, highest first:

    1. the effective ``verification_contract.work_mode`` — already folds the
       explicit CLI ``--mode`` override (carried via ``ORCHO_WORK_MODE``, which
       is also where an auto-detect run lands its resolved ``actual_mode``), the
       project/contract-declared work_mode, and the resolved profile default;
    2. the auto-detect ``actual_mode`` recorded on ``session['auto_detect']`` —
       the fallback for a run with no verification contract (so the work-mode
       projection above never ran) yet a resolved auto-detect posture;
    3. the conservative ``fast`` default when the posture is otherwise
       unresolved (no contract, no auto-detect) — fast stays the default when
       the mode is not explicitly resolved.
    """
    contract = inputs.verification_contract
    work_mode = (
        getattr(contract, "work_mode", "") if contract is not None else ""
    )
    mode = coerce_operating_mode(work_mode)
    if mode is not None:
        return mode
    auto = inputs.session.get("auto_detect") if isinstance(inputs.session, Mapping) else None
    actual = auto.get("actual_mode") if isinstance(auto, Mapping) else None
    mode = coerce_operating_mode(actual)
    if mode is not None:
        return mode
    return OperatingMode.FAST


def _codemap_injectable(plugin: PluginConfig, project_dir: str) -> str:
    """Return a codemap string for prompt injection, or "" when disabled.

    Reads ``AppConfig.codemap`` for the global toggle and lets the plugin
    override languages/depth via its own ``codemap`` dict (duck-typed).
    Empty string means "do not inject" — callers don't have to ``if`` on it.
    """
    try:
        app = config.AppConfig.load()
    except Exception:
        return ""
    cm = app.codemap or {}
    if not cm.get("enabled", False):
        return ""
    return build_repo_map(
        project_dir,
        plugin=None,  # plugin is the project's PluginConfig — we read codemap from AppConfig
        languages=list(cm.get("languages") or []) or None,
        max_depth=int(cm.get("max_depth", 3)),
    )


def _make_state(
    *,
    task: str,
    project_dir: str,
    plugin: PluginConfig,
    phase_config: PhaseAgentConfig,
    registry: object | None = None,
    output_dir: Path | None,
    dry_run: bool,
    extras: dict[str, Any],
):
    return PipelineState(
        task=task,
        project_dir=project_dir,
        plugin=plugin,
        registry=registry,
        phase_config=phase_config,
        output_dir=output_dir,
        dry_run=dry_run,
        extras=dict(extras),
    )


def _seed_mono_participant_set(state: Any, inputs: StateInputs) -> None:
    """Seed the run-scoped, in-memory mono :class:`ParticipantSet` onto
    ``state.extras[PARTICIPANT_SET_EXTRAS_KEY]`` (ADR 0112 §1).

    One participant for the primary repo: ``editable_checkout`` is the resolved
    run ``git_cwd`` (the isolated per-run worktree, or the project path in degraded
    isolation-off — symmetric isolation §2 keeps that the single edit/verify root),
    ``delivery_target`` the canonical project. The participant's ``base_ref`` is
    lifted from ``session['worktree']`` when present (informational; the durable
    isolation form is unchanged). Mirrors the (checkout, project) inputs the
    verification placeholder resolves with, so the seeded set and the fail-closed
    resolver derive the same source.
    """
    from pipeline.participants import ParticipantSet

    worktree = (
        inputs.session.get("worktree")
        if isinstance(inputs.session, Mapping) else None
    )
    base_ref = ""
    if isinstance(worktree, Mapping):
        base_ref = str(worktree.get("base_ref") or "")
    state.extras[PARTICIPANT_SET_EXTRAS_KEY] = ParticipantSet.for_mono(
        checkout=inputs.git_cwd or str(inputs.project_path),
        project=str(inputs.project_path),
        base_ref=base_ref,
    )


def _verification_placeholder_context(
    contract: Any, project_path: Path, output_dir: Path | None,
    *, checkout: str = "", participant_set: Any | None = None,
) -> Any:
    """Assemble a :class:`PlaceholderContext` for the declared contract.

    ``checkout`` is the run's git cwd — the worktree checkout under isolation,
    the project path otherwise — and is the **verification subject**: gate
    commands, env assertions, and touched-path selection all run against it.
    ``project`` stays the original project path (stable resources such as
    gitignored SDK dirs live there). An empty ``checkout`` falls back to the
    project path. ``run_dir`` is the output dir when known (``None`` otherwise —
    the ``{run_dir}`` token then stays literal in builders); ``workspace`` is
    inferred from the environment; ``dependencies`` maps each declared
    dependency name to its (project-relative) path. Purely syntactic — no
    filesystem access beyond path normalisation.

    ``participant_set`` is the run-scoped, in-memory set seeded onto
    ``state.extras`` (ADR 0112 §1). Threaded so the resolved ``isolated_source``
    comes from the single run-scoped set rather than an independently rebuilt one;
    the path-input signature stays unchanged for external callers that omit it.
    """
    from core.infra.platform import workspace_dir as _resolve_workspace
    from pipeline.verification_contract import placeholder_context_for

    workspace = _resolve_workspace() or project_path
    return placeholder_context_for(
        contract,
        checkout=checkout or str(project_path),
        project=str(project_path),
        workspace=str(workspace),
        run_dir=str(output_dir) if output_dir is not None else None,
        participant_set=participant_set,
    )


def refresh_verification_placeholders(
    state: Any,
    *,
    project_path: Path,
    output_dir: Path | None,
    checkout: str,
    participant_set: Any,
) -> bool:
    """Rebuild ``state.extras['verification_placeholders']`` from a mutated set.

    Discovery-time participant promotion (ADR 0112 §4) mutates the run-scoped
    :class:`pipeline.participants.ParticipantSet` IN PLACE after the initial state
    build. The verification placeholder snapshot is NOT cacheable across that
    mutation: the next gate must resolve ``{dependency:repo}`` against the
    just-added participant's worktree, not the stale snapshot taken before the
    repo joined the set. This re-runs the SAME builder
    (:func:`_verification_placeholder_context` →
    :func:`pipeline.verification_contract.placeholder_context_for`) the initial
    seeding used, threading the now-mutated ``participant_set`` so the live
    resolver and the snapshot never diverge.

    No-op (returns ``False``) when no verification contract is declared — the
    ``verification_placeholders`` key was never added and stays absent, so a
    contract-less run is byte-identical. Adds NO new persisted shape: the snapshot
    lives only on the in-memory ``state.extras`` (the durable isolation form stays
    ``session['worktree']`` / meta).
    """
    contract = state.extras.get("verification_contract")
    if contract is None:
        return False
    state.extras["verification_placeholders"] = _verification_placeholder_context(
        contract, project_path, output_dir,
        checkout=checkout, participant_set=participant_set,
    )
    return True


def hydrate_state_extras_from_session(
    state: Any, session: Mapping[str, Any] | None,
) -> None:
    """Lift durable session keys into ``state.extras`` in place.

    No-op when ``session`` is missing the key or when the runtime copy is
    already present (the same-process resume branch sets it first; we do
    not overwrite a live copy with the persisted snapshot).
    """
    if not isinstance(session, Mapping):
        return
    waiver = session.get(_PHASE_HANDOFF_WAIVER_KEY)
    if isinstance(waiver, Mapping) and _PHASE_HANDOFF_WAIVER_KEY not in state.extras:
        state.extras[_PHASE_HANDOFF_WAIVER_KEY] = dict(waiver)


def hydrate_parsed_plan_from_output_dir(state: Any, output_dir: Path | None) -> bool:
    """Compatibility wrapper: recover the parsed plan from ``output_dir``.

    Thin delegate over the shared projector
    :func:`pipeline.project.resume_artifacts.load_and_project_parsed_plan` — it
    is **not** a second loader. The bool contract is preserved exactly: True when
    a plan was loaded and projected; False when ``state.parsed_plan`` is already
    present, when ``output_dir`` is None, or when the artifact is missing/corrupt
    (``ParsedPlanArtifactError``, no markdown fallback).

    ``build_pipeline_state`` itself now routes through
    :func:`bootstrap_resume_artifacts`; this helper stays a stable public entry
    point for callers/tests that hydrate the plan directly without the resume
    marker/provenance machinery.
    """
    from pipeline.project.resume_artifacts import load_and_project_parsed_plan

    return load_and_project_parsed_plan(state, output_dir)


__all__ = [
    "PARTICIPANT_SET_EXTRAS_KEY",
    "StateInputs",
    "StateSetup",
    "build_pipeline_state",
    "hydrate_parsed_plan_from_output_dir",
    "hydrate_state_extras_from_session",
    "refresh_verification_placeholders",
]
