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

from agents.registry import AgentRegistry, PhaseAgentConfig
from pipeline.plugins import PluginConfig


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

    def _effort_for(phase: str) -> str | None:
        return phase_efforts.get(phase)

    # Re-bind a slot whenever either half (provider or model) is overridden.
    # Single-half overrides fall back to the per-phase default for the other
    # half — this lets the dashboard send paired controls and lets CLI users
    # tweak just the provider for A/B tests. Effort always comes from the
    # per-phase default (no CLI override yet) so an A/B model swap doesn't
    # silently lose the configured reasoning budget.
    if plan or runtime_plan:
        cfg.plan_agent = registry.resolve(
            _model_for("plan", plan),
            _runtime_for("plan", runtime_plan),
            effort=_effort_for("plan"),
        )
    if implement or runtime_implement:
        cfg.implement_agent = registry.resolve(
            _model_for("implement", implement),
            _runtime_for("implement", runtime_implement),
            effort=_effort_for("implement"),
        )
    if repair_changes or runtime_repair_changes:
        cfg.repair_changes_agent = registry.resolve(
            _model_for("repair_changes", repair_changes),
            _runtime_for("repair_changes", runtime_repair_changes),
            effort=_effort_for("repair_changes"),
        )
        cfg.repair_escalation_agent = registry.resolve(
            _model_for("repair_escalation", repair_changes),
            _runtime_for("repair_escalation", runtime_repair_changes),
            effort=_effort_for("repair_escalation"),
        )
    if review_changes or runtime_review_changes:
        cfg.validate_plan_agent = registry.resolve(
            _model_for("validate_plan", review_changes),
            _runtime_for("validate_plan", runtime_review_changes),
            effort=_effort_for("validate_plan"),
        )
        cfg.review_changes_agent = registry.resolve(
            _model_for("review_changes", review_changes),
            _runtime_for("review_changes", runtime_review_changes),
            effort=_effort_for("review_changes"),
        )
        cfg.final_acceptance_agent = registry.resolve(
            _model_for("final_acceptance", review_changes),
            _runtime_for("final_acceptance", runtime_review_changes),
            effort=_effort_for("final_acceptance"),
        )
    return cfg
