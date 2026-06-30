"""Cross-level agent setup + display metadata for the cross pipeline.

The typed cross entry surface (``pipeline/cross_project/app.py``) resolves
the cross-level plan / review agents and the operator-facing display
metadata before the run header is painted. This module owns that work:

* cross-level model selection (``plan`` / ``code`` / ``review``);
* the agent provider (caller-supplied or a fresh ``RealAgentProvider``);
* the cross-level ``plan`` / ``review`` agent instances, resolved through
  :func:`_resolve_cross_level_agent` (metadata-only precedence over
  ``phase_config`` / ``AppConfig`` maps, fresh instance via
  ``provider.resolve``);
* the ``agents_block`` / ``project_agents_block`` / ``pipeline_runtimes``
  display metadata consumed by the cross run header.

:func:`setup_cross_agents` returns a typed :class:`CrossAgentSetup` so the
coordinator wires the run off a single structured object. Provider-
specific behaviour stays in the runtime adapter behind
``provider.resolve``; this module never imports a concrete runtime.

This module is a leaf peer: it MUST NOT import from
:mod:`pipeline.cross_project.orchestrator`.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from agents.registry import (
    PHASE_AGENT_ATTRS as _PHASE_AGENT_ATTRS,
    PhaseAgentConfig,
)
from agents.runtimes import AgentProvider, RealAgentProvider
from core.infra import config
from pipeline.cross_project.profile_setup import (
    CrossProfileSetup,
    _flatten_profile_entries,
    _gate_will_run,
)


@dataclasses.dataclass(frozen=True)
class CrossAgentSetup:
    """Resolved cross-level agents + display metadata for one run.

    ``plan_agent`` / ``review_agent`` are fresh runtime instances (never
    aliased from ``phase_config``); the ``*_block`` / ``pipeline_runtimes``
    fields are the operator-facing display metadata the cross run header
    renders.
    """

    provider: AgentProvider
    plan_agent: Any
    review_agent: Any
    plan_model: str
    code_model: str
    review_model: str
    agents_block: list
    project_agents_block: list
    pipeline_runtimes: dict[str, str]


def setup_cross_agents(
    *,
    provider: AgentProvider | None,
    phase_config: PhaseAgentConfig | None,
    profile_setup: CrossProfileSetup,
    cross_mode: str,
    model: str,
    projects: Mapping[str, Any],
    effort_map: Mapping[str, Any],
) -> CrossAgentSetup:
    """Resolve cross-level agents and build the header display metadata.

    Provider-specific behaviour stays inside the runtime adapter reached
    through ``provider.resolve``; the cross level only reads metadata.
    """
    if phase_config is not None:
        plan_model   = phase_config.plan_agent.model
        code_model   = phase_config.implement_agent.model
        review_model = phase_config.review_changes_agent.model
    else:
        plan_model   = config.phase_model("plan", "claude-opus-4-8[1m]")
        code_model   = model
        review_model = config.CODEX_MODEL

    _provider: AgentProvider = provider if provider is not None else RealAgentProvider()
    # Cross-level slots resolve through the generic helper so a new
    # runtime id flows through ``phase_runtime_map`` (or a supplied
    # ``phase_config`` slot's ``runtime`` attribute) without code change.
    # Local variable names (``claude_plan`` / ``codex``) are retained
    # because every downstream call site reads them — renaming is a
    # separate cleanup pass.
    claude_plan = _resolve_cross_level_agent(
        _provider,
        phase="plan",
        phase_config=phase_config,
        fallback_model=plan_model,
        fallback_runtime="claude",
    )
    codex = _resolve_cross_level_agent(
        _provider,
        phase="review_changes",
        phase_config=phase_config,
        fallback_model=review_model,
        fallback_runtime="codex",
    )
    from core.observability.trace import vtrace
    vtrace("provider", type(_provider).__name__,
           extra="projects=" + ",".join(projects.keys()))

    projection = profile_setup.projection
    global_handlers = profile_setup.global_handlers
    contract_gate_policy = profile_setup.contract_gate_policy
    cfa_gate_policy = profile_setup.cfa_gate_policy

    agents_block: list = []
    if "cross_plan" in global_handlers:
        agents_block.extend([
            {"role": "CROSS_HYPOTHESIS", "model": plan_model,
             "effort": str(effort_map.get("plan") or "")},
            {"role": "CROSS_PLAN",       "model": plan_model,
             "effort": str(effort_map.get("plan") or "")},
        ])
    if "cross_validate_plan" in global_handlers:
        agents_block.append({
            "role": "CROSS_VALIDATE_PLAN", "model": review_model,
            "effort": str(effort_map.get("validate_plan") or ""),
        })
    if cross_mode == "full" and _gate_will_run(contract_gate_policy):
        agents_block.append({
            "role": "CONTRACT_CHECK", "model": review_model,
            "effort": str(effort_map.get("review_changes") or ""),
        })
    if cross_mode == "full" and _gate_will_run(cfa_gate_policy):
        agents_block.append({
            "role": "CROSS_FINAL_ACCEPTANCE", "model": review_model,
            "effort": str(effort_map.get("final_acceptance") or ""),
        })
    project_agents_block = (
        _agent_entries_for_project_steps(
            projection.project_steps,
            phase_config=phase_config,
            plan_model=plan_model,
            code_model=code_model,
            review_model=review_model,
            effort_map=effort_map,
        )
        if cross_mode == "full" else []
    )
    pipeline_runtimes = {
        **_global_phase_runtimes(
            projection.global_steps,
            claude_plan=claude_plan,
            codex=codex,
        ),
        **_project_phase_runtimes(phase_config),
    }

    return CrossAgentSetup(
        provider=_provider,
        plan_agent=claude_plan,
        review_agent=codex,
        plan_model=plan_model,
        code_model=code_model,
        review_model=review_model,
        agents_block=agents_block,
        project_agents_block=project_agents_block,
        pipeline_runtimes=pipeline_runtimes,
    )


def _global_phase_runtimes(
    global_steps,
    *,
    claude_plan: Any,
    codex: Any,
) -> dict[str, str]:
    """Map ``step.phase`` to the runtime that will handle each global
    cross step, for the Pipeline-block ``[Claude]`` / ``[Codex]`` chip.

    Keyed by the **semantic** phase name (``step.phase``) rather than
    by ``step.cross.handler``: the renderer looks up by ``step.phase``,
    and a profile may alias a custom semantic name onto a known cross
    handler (e.g. ``"replan"`` → ``cross_plan``). The handler stays
    the source of truth for runtime selection.

    Steps without a recognised ``cross.handler`` are omitted from the
    map — the renderer falls back to a bare phase name for those, which
    is the right thing for steps the cross runner won't dispatch
    through ``claude_plan`` / ``codex`` directly.
    """
    handler_runtime = {
        "cross_plan": getattr(claude_plan, "runtime", "claude"),
        "cross_validate_plan": getattr(codex, "runtime", "codex"),
    }
    out: dict[str, str] = {}
    for step in _flatten_profile_entries(global_steps):
        handler = getattr(getattr(step, "cross", None), "handler", None)
        runtime = handler_runtime.get(handler)
        if runtime:
            out[step.phase] = runtime
    return out


def _project_phase_runtimes(
    phase_config: PhaseAgentConfig | None,
) -> dict[str, str]:
    """Map ``phase -> runtime`` for the per-project sub-pipeline chips.

    Mirrors the mono header's runtime resolution: every slot in
    ``phase_config`` contributes its ``[Claude]`` / ``[Codex]`` chip to
    the ``Per project`` section of the cross pipeline block. Returns
    ``{}`` when no config is available (silent / dry-run callers), so
    those phases render bare.
    """
    if phase_config is None:
        return {}
    out: dict[str, str] = {}
    for phase, attr in _PHASE_AGENT_ATTRS.items():
        agent = getattr(phase_config, attr, None)
        runtime = getattr(agent, "runtime", None)
        if runtime:
            out[phase] = str(runtime)
    return out


def _agent_model_for_phase(
    phase: str,
    *,
    phase_config: PhaseAgentConfig | None,
    plan_model: str,
    code_model: str,
    review_model: str,
) -> str:
    if phase_config is not None:
        attr = _PHASE_AGENT_ATTRS.get(phase)
        agent = getattr(phase_config, attr, None) if attr else None
        model = getattr(agent, "model", None)
        if model:
            return str(model)
    if phase == "plan":
        return plan_model
    if phase in {"implement", "repair_changes"}:
        return code_model
    return review_model


def _agent_entries_for_project_steps(
    entries,
    *,
    phase_config: PhaseAgentConfig | None,
    plan_model: str,
    code_model: str,
    review_model: str,
    effort_map: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Render only project-projected phases.

    The profile projection has already removed cross-owned and skipped
    phases, so this list tells the operator what will actually run
    inside each child project.
    """
    agents: list[dict[str, str]] = []
    seen: set[str] = set()
    for step in _flatten_profile_entries(entries):
        phase = str(getattr(step, "phase", "") or "")
        if not phase or phase in seen:
            continue
        seen.add(phase)
        agents.append({
            "role": phase.upper(),
            "model": _agent_model_for_phase(
                phase,
                phase_config=phase_config,
                plan_model=plan_model,
                code_model=code_model,
                review_model=review_model,
            ),
            "effort": str(effort_map.get(phase) or ""),
        })
    return agents


def _resolve_cross_level_agent(
    provider: AgentProvider,
    *,
    phase: str,
    phase_config: PhaseAgentConfig | None,
    fallback_model: str,
    fallback_runtime: str,
):
    """Resolve a fresh runtime for a cross-level slot.

    Generic over runtime id: a third-party runtime registered under
    ``orcho.agent_runtimes`` and pinned via
    ``AppConfig.phase_runtime_map[phase]`` (or referenced by a
    ``phase_config`` slot's ``runtime`` attribute) routes through the
    same ``provider.resolve(...)`` call.

    Precedence:

    * When ``phase_config`` is supplied, read **metadata only**
      (``runtime`` / ``model`` / ``effort``) from the matching slot and
      construct a fresh runtime through ``provider.resolve``. The
      ``phase_config`` agent instance itself is NOT aliased into the
      cross level — that would share ``session_id`` / telemetry /
      prompt-session state between the cross-level surface and the
      per-project phase that owns it.
    * Otherwise, derive runtime/model/effort from ``AppConfig`` with
      caller-supplied fallbacks.
    """
    attr = _PHASE_AGENT_ATTRS.get(phase)
    slot = getattr(phase_config, attr, None) if (phase_config and attr) else None

    # A genuine config-load failure must propagate (fail fast) — do not
    # swallow it and silently run cross-level planning/review with
    # fallback metadata. The ``getattr(..., None) or {}`` below is the
    # only defensive layer: test fixtures stub ``AppConfig.load()`` with a
    # ``SimpleNamespace`` that omits the phase_*_map attributes, and those
    # missing maps fall back to the caller-supplied ``fallback_*`` values.
    app = config.AppConfig.load()
    phase_runtimes = getattr(app, "phase_runtime_map", None) or {}
    phase_models   = getattr(app, "phase_model_map", None) or {}
    phase_efforts  = getattr(app, "phase_effort_map", None) or {}

    if slot is not None:
        runtime = getattr(slot, "runtime", None) or fallback_runtime
        model   = getattr(slot, "model", None) or fallback_model
        effort  = getattr(slot, "effort", None)
        if effort is None:
            effort = phase_efforts.get(phase)
    else:
        runtime = phase_runtimes.get(phase) or fallback_runtime
        model   = phase_models.get(phase) or fallback_model
        effort  = phase_efforts.get(phase)

    return provider.resolve(runtime, model, effort=effort)


__all__ = [
    "CrossAgentSetup",
    "setup_cross_agents",
    "_global_phase_runtimes",
    "_project_phase_runtimes",
    "_agent_model_for_phase",
    "_agent_entries_for_project_steps",
    "_resolve_cross_level_agent",
]
