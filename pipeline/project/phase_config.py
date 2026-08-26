"""Per-phase agent/runtime configuration builder.

Moved out of :mod:`pipeline.project_orchestrator` per ADR 0042
Phase H. Lives in its own module (NOT inside ``pipeline.project.cli``)
so :mod:`pipeline.cross_project` can depend on it without importing
from a CLI leaf — the leaf-layer rule in ADR 0042 forbids non-CLI
code from reaching into ``pipeline.project.cli``.

Consumed by:

* The ``orcho-run`` CLI (``pipeline.project.cli.main``) — translates
  ``--model-plan`` / ``--model-implement`` / ``--runtime-*`` flags
  into a :class:`PhaseAgentConfig`.
* The cross-project orchestrator (``pipeline.cross_project.orchestrator``)
  — builds a phase config from the same CLI-shaped overrides for the
  per-project child runs.
"""

from __future__ import annotations

from collections.abc import Mapping

from agents.registry import AgentRegistry, PhaseAgentConfig
from pipeline.plugins import PluginConfig


def resolve_phase_effort(
    phase: str,
    *,
    profile_phase_efforts: Mapping[str, str] | None,
    phase_effort_map: Mapping[str, str],
) -> str | None:
    """Return the active profile's effort for ``phase``, else AppConfig's map.

    A profile declaration is the targeted setting and therefore wins for its
    phase; phases absent from the profile retain the global AppConfig default.
    """
    if profile_phase_efforts is not None and phase in profile_phase_efforts:
        return profile_phase_efforts[phase]
    return phase_effort_map.get(phase)


def build_phase_config_from_overrides(
    *,
    plan: str | None = None,
    implement: str | None = None,
    repair_changes: str | None = None,
    review_changes: str | None = None,
    runtime_plan:           str | None = None,
    runtime_implement:      str | None = None,
    runtime_repair_changes: str | None = None,
    runtime_review_changes: str | None = None,
    plugin: PluginConfig | None = None,
    profile_phase_efforts: Mapping[str, str] | None = None,
) -> PhaseAgentConfig:
    """Build a PhaseAgentConfig from CLI ``--model-*`` / ``--runtime-*`` overrides.

    Any None field falls back to the AppConfig default. ``review_changes``
    overrides all three reviewer slots (validate_plan, review_changes,
    final_acceptance). ``repair_changes`` overrides both the round-1
    repair agent and the escalation agent — escalating to a different
    model in round 2+ is still possible by configuring
    ``MODEL_REPAIR_ESCALATION`` in env / config.local.json.

    When a provider override is omitted, the resolved provider for that phase
    is inherited from AppConfig (which already knows the per-phase default).
    Passing a model alone is therefore safe — but UIs are encouraged to send
    both halves together to avoid sending Claude models to Codex.

    ``profile_phase_efforts`` is forwarded to :func:`resolve_phase_effort`
    whenever a slot is built or rebound.
    """
    registry = AgentRegistry.default()
    cfg = PhaseAgentConfig.default(registry)

    import core.infra.config as _core_config
    app = _core_config.AppConfig.load()
    phase_models   = app.phase_model_map
    phase_runtimes = app.phase_runtime_map
    phase_efforts  = app.phase_effort_map

    def _runtime_for(phase: str, override: str | None) -> str:
        return override or phase_runtimes.get(phase, "claude")

    def _model_for(phase: str, override: str | None) -> str:
        return override or phase_models.get(phase, "")

    # Re-bind a slot whenever either half (provider or model) is overridden.
    # Single-half overrides fall back to the per-phase default for the other
    # half — this lets the dashboard send paired controls and lets CLI users
    # tweak just the provider for A/B tests. Effort is delegated to the shared
    # resolver, so an A/B model swap keeps the phase's configured budget.
    slots = (
        ("plan", "plan_agent", plan, runtime_plan),
        ("validate_plan", "validate_plan_agent", review_changes, runtime_review_changes),
        ("implement", "implement_agent", implement, runtime_implement),
        ("review_changes", "review_changes_agent", review_changes, runtime_review_changes),
        ("repair_changes", "repair_changes_agent", repair_changes, runtime_repair_changes),
        ("repair_escalation", "repair_escalation_agent", repair_changes, runtime_repair_changes),
        ("final_acceptance", "final_acceptance_agent", review_changes, runtime_review_changes),
    )
    for phase, attr, model_override, runtime_override in slots:
        effort = resolve_phase_effort(
            phase,
            profile_phase_efforts=profile_phase_efforts,
            phase_effort_map=phase_efforts,
        )
        if model_override or runtime_override or (
            profile_phase_efforts is not None and phase in profile_phase_efforts
        ):
            setattr(
                cfg,
                attr,
                registry.resolve(
                    _model_for(phase, model_override),
                    _runtime_for(phase, runtime_override),
                    effort=effort,
                ),
            )
    return cfg
