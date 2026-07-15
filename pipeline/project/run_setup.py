"""Run-level setup for the project pipeline.

The typed project entry surface (``pipeline/project/app.py``) turns a
resolved profile + runtime into a live run: it must allocate the run id
and wire logging (emitting the ``run.start`` event), print the operator
header, initialise the persisted session (``meta.json`` + atexit
graceful-exit hook), seed the metrics collector, and open the checkpoint
store. This module owns that work, calling the low-level primitives in
:mod:`pipeline.project.bootstrap` and returning typed objects so the
coordinator wires the run off structured results.

Ordering invariants the coordinator preserves (this module does not
reorder them — each function is called at its existing point in the run
body):

* :func:`setup_run_id` runs after profile resolution and before
  ``load_plugin`` / runtime setup, so the ``run.start`` event + initial
  logging carry the final projected profile name and a planning-only
  ``--from-run-plan`` refusal fires before any run-dir I/O.
* :func:`print_pipeline_header` runs after runtime setup (it needs the
  synthesized ``phase_config`` for the per-phase ``[Claude]`` / ``[Codex]``
  chips) and is a no-op unless ``presentation`` is TERMINAL.
* :func:`init_run_session` runs after runtime setup (it needs the resolved
  ``session_mode`` + ``change_handoff``) and before isolation setup.
* :func:`setup_checkpoint_and_metrics` runs after isolation/sandbox setup.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
from pathlib import Path
from typing import Any

from agents.protocols import SessionMode
from agents.registry import PhaseAgentConfig
from core.infra import config
from core.observability.logging import success
from core.observability.metrics import MetricsCollector
from pipeline.engine.session import save_session
from pipeline.plugins import PluginConfig, describe_plugin
from pipeline.project.auto_detect import AUTODETECT_DECISION_ENV
from pipeline.project.bootstrap import (
    init_checkpoint_with_resume,
    init_session_with_atexit,
    resolve_run_id_and_setup_logging,
)
from pipeline.project.profile_setup import _profile_phase_names
from pipeline.project.types import PresentationPolicy
from pipeline.runtime.work_kind_detection import AutoDetectResolution
from pipeline.verification_contract import VerificationContract


@dataclasses.dataclass(frozen=True)
class RunStateSetup:
    """Metrics collector + checkpoint store for one run.

    Built together after isolation setup: the metrics collector is seeded
    (and re-hydrated from ``metrics.json`` on resume) before the checkpoint
    store is opened, matching the run body's order.
    """

    metrics: MetricsCollector
    ckpt: Any


def setup_run_id(
    *,
    task: str,
    project_dir: str,
    resume_from: str | None,
    output_dir: Path | None,
    profile_name: str,
    parent_run_id: str | None,
    project_alias: str | None,
    plan_source: str,
    projected_profile: str | None,
    presentation: PresentationPolicy,
) -> str:
    """Allocate the run id, wire logging, and emit ``run.start``.

    Resolved before any run-dir materialization so ``ORCHO_PIPELINE``
    overrides + the projected profile name are reflected consistently in
    events, ``meta.json``, checkpoint config, and dispatch.
    """
    return resolve_run_id_and_setup_logging(
        task=task, project_dir=project_dir, resume_from=resume_from,
        output_dir=output_dir, profile_name=profile_name,
        parent_run_id=parent_run_id, project_alias=project_alias,
        plan_source=plan_source,
        projected_profile=projected_profile,
        # ADR 0046 Phase F follow-up — thread the run-level presentation
        # policy into ``setup_run_logging`` so the ``📄 Live output`` /
        # ``📡 Events`` chips suppress under SILENT. The file + event sinks
        # (``set_progress_log`` / ``init_event_store``) always fire
        # regardless (ADR 0046 stop #9).
        presentation=presentation,
    )


def init_run_session(
    *,
    task: str,
    project_path: Path,
    plugin: PluginConfig,
    model: str,
    profile_name: str,
    session_mode: SessionMode,
    change_handoff: str,
    output_dir: Path | None,
    plan_source: str,
    projected_profile: str | None,
    resume_mode: str | None,
    followup_parent_run_id: str | None,
    followup_parent_run_dir: Path | None,
    followup_parent_status: str | None,
    followup_base_task: str | None,
    plan_source_run_id: str | None,
) -> dict:
    """Initialise the persisted session dict + atexit graceful-exit hook.

    Stage C (T4): when this run started via the ``auto-detect`` selector, the
    dispatcher (T3) serialized its typed ``AutoDetectResolution`` into the
    scoped ``ORCHO_AUTODETECT_DECISION`` env channel around ``run_pipeline``.
    Read it here and persist it as the additive optional ``meta.auto_detect``
    block. A missing / empty / invalid value is ignored — a manual concrete
    profile never wrote the channel, so ``meta.auto_detect`` stays absent and a
    stale value cannot leak into a later manual run in the same process
    (second half of fix F2). The ``actual_profile`` / ``actual_mode`` recorded
    here are exactly the profile + mode the run actually starts with.

    The whole serialized payload is persisted verbatim (no point-selection of
    fields), so the topology axis added in T2 — ``recommended_topology`` /
    ``delivery_projects`` / ``topology_reason`` / ``delivery_scope`` — rides
    along automatically. ``delivery_scope`` + ``delivery_projects`` are the
    durable input that delivery-scope enforcement (T4) reads back.
    """
    session = init_session_with_atexit(
        task=task, project_path=project_path, plugin=plugin, model=model,
        profile_name=profile_name, session_mode=session_mode,
        change_handoff=change_handoff,
        output_dir=output_dir,
        plan_source=plan_source,
        projected_profile=projected_profile,
        resume_mode=resume_mode,
        followup_parent_run_id=followup_parent_run_id,
        followup_parent_run_dir=followup_parent_run_dir,
        followup_parent_status=followup_parent_status,
        followup_base_task=followup_base_task,
        plan_source_run_id=plan_source_run_id,
    )
    auto_detect = _read_autodetect_meta()
    if auto_detect is not None:
        session["auto_detect"] = auto_detect
        # init_session_with_atexit already wrote meta.json; re-persist so the
        # additive block lands. The atexit hook captured this same dict, so
        # the block is also present on an abnormal-exit re-save.
        if output_dir is not None:
            with contextlib.suppress(OSError):
                save_session(output_dir, session)
    return session


def _read_autodetect_meta() -> dict | None:
    """Read + validate the scoped auto-detect decision channel.

    Returns the JSON payload (the serialized ``AutoDetectResolution`` — see
    ``pipeline.project.auto_detect.resolution_to_payload``) to persist as
    ``meta.auto_detect``, or ``None`` when the channel is absent / empty /
    malformed / fails the resolution invariant. Validation reconstructs an
    ``AutoDetectResolution`` from the payload, so a garbage value (e.g. a
    detector-error shape that claims a recommendation) is rejected rather than
    persisted. Never raises.
    """
    raw = os.environ.get(AUTODETECT_DECISION_ENV)
    if not raw or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        # Validate shape + invariant (recommended_* present unless detector
        # error / failed); reject anything that does not round-trip.
        AutoDetectResolution(**payload)
    except (TypeError, ValueError):
        return None
    return payload


def setup_checkpoint_and_metrics(
    *,
    plan_model: str,
    implement_model: str,
    review_model: str,
    resume_from: str | None,
    output_dir: Path | None,
    session_ts: str,
    task: str,
    project_path: Path,
    model: str,
    profile_name: str,
    max_rounds: int,
    change_handoff: str,
    session: dict,
    presentation: PresentationPolicy,
) -> RunStateSetup:
    """Seed the metrics collector and open the checkpoint store.

    Resume continuity for metrics aggregation. Without re-hydration, the
    resume subprocess starts with an empty accumulator and finalize writes
    ``metrics.json`` containing only the post-resume attempts — every plan /
    validate_plan round that ran in the pre-pause subprocess disappears from
    the rollup. The pause snapshot writer in ``_apply_phase_handoff_pause``
    lands ``metrics.json`` before rc=4 exit; we read it back here and re-seed
    ``_phases`` / ``_rounds`` / ``_total_retries`` so subsequent
    ``record_phase`` calls extend (rather than replace) the prior
    subprocess's work. Best-effort: a missing or malformed snapshot must not
    break resume — ``load_from_disk`` returns ``0`` and leaves the collector
    empty, matching pre-fix behaviour.
    """
    metrics = MetricsCollector(
        plan_model=plan_model, implement_model=implement_model, review_model=review_model,
    )
    if resume_from is not None and output_dir is not None:
        metrics.load_from_disk(output_dir / "metrics.json")

    ckpt = init_checkpoint_with_resume(
        output_dir=output_dir, resume_from=resume_from, session_ts=session_ts,
        task=task, project_path=project_path, model=model,
        profile_name=profile_name, max_rounds=max_rounds,
        change_handoff=change_handoff,
        session=session,
        # ADR 0046 Phase C (site 15): gate the resume-banner ``success(...)``
        # under SILENT. Default TERMINAL preserved on direct callers.
        presentation=presentation,
    )
    return RunStateSetup(metrics=metrics, ckpt=ckpt)


def project_verification_contract(
    plugin: PluginConfig,
) -> VerificationContract | None:
    """Validate and return the declared verification contract, or ``None``.

    Thin seam the run coordinator calls once, unconditionally, between
    ``load_plugin`` and the pipeline header. Returns ``None`` when no contract
    is declared (run behaves byte-identically to before); raises
    :class:`pipeline.verification_contract.VerificationContractError` when a
    contract is declared but invalid, so the run fails fast with a clear error.

    Read-only Stage 1: this never executes ``verification.commands``, writes a
    receipt, or blocks a transition — it only loads and validates the contract.
    """
    return VerificationContract.from_plugin(plugin)


def print_pipeline_header(
    *,
    presentation: PresentationPolicy,
    project_path: Path,
    task: str,
    plan_model: str,
    implement_model: str,
    review_model: str,
    profile_name: str,
    session_mode: SessionMode,
    max_rounds: int,
    do_plan: bool,
    plugin: PluginConfig,
    output_dir: Path | None,
    parent_run_id: str | None = None,
    project_alias: str | None = None,
    followup_parent_run_id: str | None = None,
    followup_base_task: str | None = None,
    followup_parent_status: str | None = None,
    followup_child_status: str | None = None,
    followup_active_handoff_id: str | None = None,
    profile_obj: Any | None = None,
    phase_config: PhaseAgentConfig | None = None,
    phase_identities: dict[str, Any] | None = None,
    resume_from: str | None = None,
    contract: VerificationContract | None = None,
) -> None:
    """Emit the run-header banner via :mod:`core.io.transcript`.

    ADR 0046 Phase C (site 1): the pipeline header banner + ``success("Run
    dir: …")`` line are a terminal courtesy; skip the whole call under
    SILENT — the run-start structural record is in the ``run.start`` event
    (emitted by :func:`setup_run_id`) + ``meta.json`` written by
    :func:`init_run_session`.

    Renders the same data the legacy header carried — model names, effort
    levels, profile, session mode, max rounds, plan toggle, plugin line,
    run-dir path — as a scannable table block. When ``profile_obj`` is
    supplied, a static pipeline progress block is rendered under the header
    showing every phase in the profile; ``completed_phases`` (peeked from
    the checkpoint DB on a ``--resume``) highlights phases already finished
    so the operator can see where the run is about to pick up.
    """
    if presentation is not PresentationPolicy.TERMINAL:
        return

    from core.io.pipeline_block import render_pipeline_block
    from core.io.transcript import render_run_header

    completed_phases = _peek_completed_phases(output_dir, resume_from)
    current_phase = _peek_resume_current_phase(output_dir, resume_from)
    resumed = resume_from is not None

    try:
        eff_map = config.AppConfig.load().phase_effort_map or {}
    except Exception:
        eff_map = {}
    # Dispatch truth first: each row reads model/effort from the phase's
    # actual agent slot in ``phase_config`` — the same object the run
    # dispatches on — so a per-phase ``phase_model_map`` entry (e.g.
    # validate_plan pinned to a different reviewer model) shows in the
    # banner instead of the coarse plan/implement/review triple, which is
    # only the fallback for callers that resolved no config.
    slot_display = _phase_agent_display(phase_config)

    def _agent_row(role: str, fallback_model: str) -> dict[str, str]:
        phase = role.lower()
        model, effort = slot_display.get(phase, (fallback_model, ""))
        return {
            "role": role,
            "model": model,
            "effort": effort or str(eff_map.get(phase) or ""),
        }

    agents_block = [
        _agent_row("PLAN", plan_model),
        _agent_row("IMPLEMENT", implement_model),
        _agent_row("REVIEW_CHANGES", review_model),
        _agent_row("REPAIR_CHANGES", implement_model),
        _agent_row("VALIDATE_PLAN", review_model),
        _agent_row("FINAL_ACCEPTANCE", review_model),
    ]
    # Drop agent rows whose phase isn't reachable in this profile so
    # the Agents block matches the Pipeline visualization below. When
    # ``profile_obj`` is None (e.g. silent / dry-run callers that didn't
    # resolve the profile yet) fall back to the unfiltered list so we
    # don't strip a row a caller actually relied on.
    if profile_obj is not None:
        profile_phases = _profile_phase_names(profile_obj)
        agents_block = [
            row for row in agents_block
            if str(row["role"]).lower() in profile_phases
        ]
    # Attach a sanitized account-identity hint per row when a probe resolved
    # one (diagnostic only). Keyed by the phase name so a runtime running under
    # a different account on a different phase surfaces its own hint — the
    # signal that catches a wrong-account / wrong-quota-bucket run early.
    if phase_identities:
        for row in agents_block:
            identity = phase_identities.get(str(row["role"]).lower())
            hint = identity.hint() if identity is not None else ""
            if hint:
                row["account"] = hint
    plugin_summary = describe_plugin(plugin).strip()
    plugin_line = plugin_summary.splitlines()[0].removeprefix("Plugin: ").strip() if plugin_summary else None
    skills_line = _skills_header_line(plugin)
    # ``contract`` is already validated upstream (T5 in session_run.py, before
    # the header and unconditional of presentation). We only project its
    # declared names/policies into an operator-facing view here — no
    # resolution, gate execution, or receipt access inside this SILENT-gated
    # function.
    from core.io.verification_header import build_verification_header_view
    from pipeline.verification_contract import FINAL_PHASES
    # Whether the active profile has a final delivery phase decides the honest
    # ``when`` of a warn/off gate (``pre-final`` vs ``not auto-run``). Derive it
    # from the profile's phases against the single FINAL_PHASES set (never a
    # duplicated copy); ``None`` when the profile is unresolved so the banner
    # marks those gates profile-dependent rather than guessing.
    has_final_phase = (
        bool(_profile_phase_names(profile_obj) & set(FINAL_PHASES))
        if profile_obj is not None
        else None
    )
    ledger_rows = None
    if output_dir is not None:
        from pipeline.verification_ledger_store import ledger_path, load_ledger

        if ledger_path(output_dir).exists():
            ledger_rows = load_ledger(output_dir).rows
    verification_view = build_verification_header_view(
        contract, has_final_phase=has_final_phase, ledger_rows=ledger_rows,
    )
    output_log = str(output_dir / "output.log") if output_dir is not None else None
    events_log = str(output_dir / "events.jsonl") if output_dir is not None else None

    print(render_run_header(
        run_id=output_dir.name if output_dir is not None else None,
        project=str(project_path),
        task=task,
        agents=agents_block,
        profile=profile_name,
        session_mode=session_mode.value,
        rounds=max_rounds,
        plan=do_plan,
        output_log=output_log,
        events_log=events_log,
        plugin_line=plugin_line,
        skills_line=skills_line,
        verification=verification_view,
        resumed=resumed,
        completed_phases=completed_phases,
        parent_run_id=parent_run_id,
        project_alias=project_alias,
        followup_parent_run_id=followup_parent_run_id,
        followup_base_task=followup_base_task,
        followup_parent_status=followup_parent_status,
        followup_child_status=followup_child_status,
        followup_active_handoff_id=followup_active_handoff_id,
    ))
    if profile_obj is not None:
        print(render_pipeline_block(
            profile_obj,
            completed=completed_phases,
            current=current_phase,
            phase_runtimes=_phase_runtimes_from_config(phase_config),
        ))
        print()
    if output_dir is not None:
        success(f"Run dir: {output_dir}")


def _skills_header_line(plugin: PluginConfig) -> str | None:
    registry = plugin.skill_registry or {}
    if not registry:
        return None
    names = ", ".join(sorted(registry))
    return f"{len(registry)}: {names}"


def _peek_completed_phases(
    output_dir: Path | None,
    resume_from: str | None,
) -> tuple[str, ...]:
    """Read the completed-phase list from the checkpoint DB without
    opening the checkpoint store the caller will later use.

    Returns an empty tuple for fresh runs (no ``resume_from``), missing
    ``output_dir`` (silent / dry-run callers), missing DB file, or any
    read error — the pipeline header is purely informational so failing
    fast on a corrupt DB would hide the real run from the operator.
    """
    if resume_from is None or output_dir is None:
        return ()
    db_path = output_dir / "checkpoints.db"
    if not db_path.exists():
        return ()
    try:
        from pipeline.checkpoint import CheckpointStore
        with CheckpointStore(db_path, run_id=resume_from) as store:
            state = store.load(resume_from)
        return tuple(state.completed)
    except Exception:
        return ()


def _peek_resume_current_phase(
    output_dir: Path | None,
    resume_from: str | None,
) -> str | None:
    """Best-effort current-phase override for checkpoint resume banners.

    ``render_pipeline_block`` otherwise highlights the first phase that is not
    present in checkpoint.completed. That is correct for a plain checkpoint
    resume, but misleading for an already-decided phase-handoff resume:
    ``retry_feedback`` on a ``review_changes`` rejection resumes at
    ``repair_changes`` even though ``plan`` may still be the first phase missing
    from the checkpoint. This helper only peeks at durable metadata/decision
    artifacts and silently falls back to the renderer's normal inference if
    anything is absent or corrupt; the real dispatch path remains authoritative.
    """
    if resume_from is None or output_dir is None:
        return None

    try:
        meta = json.loads((output_dir / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    active = meta.get("phase_handoff")
    if not isinstance(active, dict):
        return None
    handoff_id = active.get("id")
    if not isinstance(handoff_id, str) or not handoff_id:
        return None

    try:
        from sdk.phase_handoff import load_phase_handoff_decision

        decision = load_phase_handoff_decision(
            output_dir.name,
            handoff_id,
            runs_dir=output_dir.parent,
            cwd=None,
        )
    except Exception:
        return None
    if decision is None or decision.action != "retry_feedback":
        return None

    phase = active.get("phase")
    if phase == "review_changes":
        return "repair_changes"
    if phase == "validate_plan":
        return "plan"
    if phase == "implement":
        return "implement"
    return None


def _phase_agent_display(
    phase_config: PhaseAgentConfig | None,
) -> dict[str, tuple[str, str]]:
    """Map ``phase_name -> (model, effort)`` from the actual agent slots.

    The Agents banner must report what the run will dispatch on, and the
    dispatch truth is ``phase_config`` — per-phase ``phase_model_map`` /
    ``phase_effort_map`` entries and the ADR 0101 operator override land in
    its slots, not in the coarse plan/implement/review model triple the
    header args carry. Returns ``{}`` when no config is supplied (silent /
    dry-run callers), so the header falls back to the coarse models.
    ``effort`` is ``""`` for runtimes without the attribute; the caller
    falls back to the configured effort map.
    """
    if phase_config is None:
        return {}
    from agents.registry import PHASE_AGENT_ATTRS
    out: dict[str, tuple[str, str]] = {}
    for phase, attr in PHASE_AGENT_ATTRS.items():
        agent = getattr(phase_config, attr, None)
        model = str(getattr(agent, "model", "") or "")
        if model:
            out[phase] = (model, str(getattr(agent, "effort", "") or ""))
    return out


def _phase_runtimes_from_config(
    phase_config: PhaseAgentConfig | None,
) -> dict[str, str]:
    """Map ``phase_name -> agent.runtime`` for every slot in
    ``phase_config``.

    Drives the ``[Claude]`` / ``[Codex]`` chip the Pipeline block paints
    next to each phase. Returns ``{}`` when no config is supplied (silent
    / dry-run callers), so the renderer falls back to bare phase names.
    """
    if phase_config is None:
        return {}
    from agents.registry import PHASE_AGENT_ATTRS
    out: dict[str, str] = {}
    for phase, attr in PHASE_AGENT_ATTRS.items():
        agent = getattr(phase_config, attr, None)
        runtime = getattr(agent, "runtime", None)
        if runtime:
            out[phase] = str(runtime)
    return out


def resolve_phase_identities(
    phase_config: PhaseAgentConfig | None,
    *,
    enabled: bool,
) -> dict[str, Any]:
    """Probe account identity once per distinct runtime instance.

    Returns ``phase_name -> RuntimeIdentity`` for every phase whose agent
    reports an *available* identity. Best-effort and diagnostic only: a probe
    that misses or fails just omits that phase — identity never blocks a run.

    ``enabled`` is the lazy/safety gate. When ``False`` (dry-run, profile
    listing, or any non-TERMINAL surface) this is a no-op that fires **no**
    probe, preserving the side-effect-free construction contract. The caller
    owns that decision; this function never decides to probe on its own.

    Dedup is by the actual agent **instance** (``id(agent)``), not by runtime
    name: two phases pinned to the same runtime id can still run under
    different accounts (different instances / environments), and collapsing by
    name would hide exactly the account mismatch this diagnostic exists to
    surface. Phases that genuinely share one agent instance probe once.
    """
    if not enabled or phase_config is None:
        return {}
    from agents.registry import PHASE_AGENT_ATTRS
    from agents.runtimes.identity import probe_runtime_identity

    cache: dict[int, Any] = {}
    out: dict[str, Any] = {}
    for phase, attr in PHASE_AGENT_ATTRS.items():
        agent = getattr(phase_config, attr, None)
        if agent is None:
            continue
        key = id(agent)
        if key not in cache:
            # probe_runtime_identity already swallows probe errors; the extra
            # guard keeps a pathological agent object from breaking the header.
            try:
                cache[key] = probe_runtime_identity(agent)
            except Exception:  # noqa: BLE001 — identity must never break run setup
                cache[key] = None
        identity = cache[key]
        if identity is not None and getattr(identity, "available", False):
            out[phase] = identity
    return out


def _reviewer_provider_label(phase_config: PhaseAgentConfig | None, fallback) -> str:
    """Best-effort provider label for validate_plan entries (legacy 898-901)."""
    agent = (phase_config.validate_plan_agent if phase_config is not None else fallback)
    label_fn = getattr(agent, "label", None)
    if callable(label_fn):
        try:
            return label_fn()
        except Exception:
            pass
    return agent.__class__.__name__


__all__ = [
    "RunStateSetup",
    "setup_run_id",
    "init_run_session",
    "setup_checkpoint_and_metrics",
    "print_pipeline_header",
    "project_verification_contract",
    "_peek_completed_phases",
    "_phase_runtimes_from_config",
    "_reviewer_provider_label",
]
