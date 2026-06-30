"""
agents/registry.py — AgentRegistry + PhaseAgentConfig.

Two layers of indirection sit between the orchestrator and concrete agents:

    AgentRegistry      — maps runtime name -> agent factory.
                         A "runtime" is a wrapper over an external CLI
                         subprocess (Claude / Codex / Gemini) that implements
                         :class:`IAgentRuntime`.

    PhaseAgentConfig   — fixes which agent runs which pipeline phase.
                         Holds one concrete agent per built-in phase. Each
                         phase slot is bound at construction time to a concrete
                         (runtime, model) pair, so `plan` can run Claude
                         while `review_changes` runs Codex without leaking
                         runtime choice into the profile.

``PhaseAgentConfig.default()`` reads ``AppConfig`` lazily so importing this
module performs no IO. ``AppConfig.phase_runtime_map`` is the per-phase
runtime source so sibling phases (e.g. ``review_changes`` vs
``final_acceptance``) keep distinct defaults.

All seven phase slots in :class:`PhaseAgentConfig` carry the same
:class:`IAgentRuntime` Protocol — the read/write split lives at the per-call
``mutates_artifacts`` flag, not on the agent type.
"""

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from agents.protocols import IAgentRuntime

T = TypeVar("T")
AgentFactory = Callable[[str, "str | None"], T]


class AgentRegistry:
    """Single registry of agent runtimes (Phase 7a — R10 backend
    neutrality). A runtime is a wrapper over an external CLI subprocess
    (Claude / Codex / Gemini / etc.) that implements all three role
    Protocols — registry doesn't track role, just runtime name.

    Factory contract: ``Callable[[str, str | None], Agent]`` — (model,
    effort) → instance. ``effort=None`` means "let the underlying CLI
    use its own default" (see AppConfig.phase_effort_map).
    """

    def __init__(self) -> None:
        self._runtimes: dict[str, AgentFactory] = {}

    # ── registration ─────────────────────────────────────────────────────────
    def register(self, name: str, factory: AgentFactory) -> None:
        """Register a runtime factory under ``name``. The same factory
        serves all three roles (architect / developer / reviewer)
        because real CLI runtimes implement every role Protocol.
        Re-registration replaces — that's the customer-plugin override
        mechanism.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("AgentRegistry.register: name must be non-empty string")
        self._runtimes[name.strip()] = factory

    # ── resolution ───────────────────────────────────────────────────────────
    def _instantiate(self, runtime: str, model: str, effort: str | None) -> object:
        if runtime not in self._runtimes:
            raise KeyError(
                f"No agent runtime registered: {runtime!r}. "
                f"Registered: {sorted(self._runtimes)}"
            )
        instance = self._runtimes[runtime](model, effort)
        # Tag the instance with the resolved runtime id so downstream
        # code (lifecycle, telemetry) can recover it without re-resolving
        # against the registry. ``setattr`` instead of constructor wiring
        # keeps the factory contract narrow (``(model, effort) -> agent``).
        # ``__slots__``-locked agent classes (rare) refuse the tag —
        # callers must accept ``None`` and re-resolve.
        with contextlib.suppress(AttributeError):
            instance.runtime = runtime
        return instance

    def resolve(self, model: str, runtime: str = "claude", *, effort: str | None = None) -> IAgentRuntime:
        """Instantiate the runtime by name. The single resolver after Phase 7."""
        return self._instantiate(runtime, model, effort)  # type: ignore[return-value]

    # Backwards-compat shims for code paths the orchestrator hasn't migrated
    # yet. Each just defers to :meth:`resolve` with sensible per-role defaults.
    def architect(self, model: str, runtime: str = "claude", *, effort: str | None = None) -> IAgentRuntime:
        return self._instantiate(runtime, model, effort)  # type: ignore[return-value]

    def developer(self, model: str, runtime: str = "claude", *, effort: str | None = None) -> IAgentRuntime:
        return self._instantiate(runtime, model, effort)  # type: ignore[return-value]

    def reviewer(self, model: str, runtime: str = "codex", *, effort: str | None = None) -> IAgentRuntime:
        return self._instantiate(runtime, model, effort)  # type: ignore[return-value]

    # ── introspection (test seam + diagnostics) ──────────────────────────────
    def names(self) -> list[str]:
        """Sorted list of registered runtime names."""
        return sorted(self._runtimes)

    def has(self, name: str) -> bool:
        return name in self._runtimes

    # ── defaults ─────────────────────────────────────────────────────────────
    @classmethod
    def default(cls) -> "AgentRegistry":
        """Build a registry pre-populated via importlib.metadata entry_points.

        Phase 7a: single ``orcho.agent_runtimes`` group replaces the three
        role-keyed groups (``orcho.providers.architect`` / ``.developer`` /
        ``.reviewer``). Each entry's value is an agent class implementing
        all three role Protocols; this loader wraps it in the
        ``(model, effort) -> instance`` factory shape.

        Built-in runtimes (Claude / Codex / Gemini) ship via orcho-core's
        own ``pyproject.toml``. Any third-party package may register
        additional runtimes under the same group name without core edits.

        Re-registration is allowed: a third-party entry named ``"claude"``
        replaces the built-in. Supported override mechanism for plugin
        authors who want to ship a custom runtime variant under a
        familiar name.

        Every registered entry-point is loaded — there is no PATH-based
        availability filter. Construction is side-effect free; a missing
        CLI binary surfaces lazily at the first ``invoke()`` (via
        :func:`core.infra.lazy.lazy_cli_binary`), which is the same
        surface used by every other runtime adapter.
        """
        r = cls()

        for name, agent_cls in _discover_runtimes(
            "orcho.agent_runtimes", skip=set(),
        ).items():
            r.register(name, _make_runtime_factory(agent_cls))

        if not r._runtimes:
            raise RuntimeError(
                "AgentRegistry.default(): no agent runtimes discovered "
                "via importlib.metadata entry_points (group: "
                "``orcho.agent_runtimes``). Run `pip install -e .` from "
                "orcho-core to register built-ins."
            )
        return r


def _make_runtime_factory(agent_cls: type):
    """Wrap an agent class in the ``(model, effort) -> instance`` factory shape.

    Captured class via default arg to defeat late-binding when this is called
    in a loop — every factory binds its own class, not whatever ``agent_cls``
    happened to be at the end of the loop.
    """
    def factory(model: str, effort: str | None = None, _cls=agent_cls):
        return _cls(model=model, effort=effort)
    return factory


def _discover_runtimes(group: str, skip: set[str]) -> dict[str, type]:
    """Load all entry_points in ``group`` minus ``skip``, return name→class.

    Lazy import of ``importlib.metadata`` so unrelated tests don't pay for
    metadata scan. Failures inside ``ep.load()`` (e.g. broken third-party
    entry whose module raises at import) are surfaced as warnings — one bad
    plugin must not break discovery for the rest.
    """
    from importlib.metadata import entry_points
    out: dict[str, type] = {}
    for ep in entry_points(group=group):
        if ep.name in skip:
            continue
        try:
            out[ep.name] = ep.load()
        except Exception as exc:
            print(f"  ! orcho.registry: failed to load {group} '{ep.name}': {exc}")
    return out


def _resolve_runtime(phase: str, phase_runtime_map: dict[str, str]) -> str:
    """Resolve which runtime should serve *phase*.

    Precedence (highest first):

      1. ``phase_runtime_map[phase]`` — AppConfig per-phase setting,
         which preserves distinctions between sibling phases (e.g.
         ``review`` vs ``final_acceptance``) that a flat role-map would
         collapse.
      2. ``"claude"`` — last-resort default.

    ``PhaseStep.overrides["runtime"]`` is intentionally **not** consulted
    here: phase-level step override would require ``PhaseStepExecutor``
    to rebuild ``PhaseAgentConfig`` per step, which is out of scope for
    this refactor. The DAG subtask path consults
    ``PhaseStep.overrides["runtime"]`` directly via
    :func:`pipeline.agent_resolver.resolve_subtask_agent`.
    """
    return phase_runtime_map.get(phase, "claude")


#: Maps a phase name to the :class:`PhaseAgentConfig` slot that owns its
#: agent. Co-located with the dataclass so the two definitions never
#: drift apart. ``compliance_check`` reuses the review slot because the
#: built-in handler dispatches the same reviewer for the check pass.
PHASE_AGENT_ATTRS: dict[str, str] = {
    "plan":              "plan_agent",
    "validate_plan":     "validate_plan_agent",
    "implement":         "implement_agent",
    "review_changes":    "review_changes_agent",
    "repair_changes":    "repair_changes_agent",
    "final_acceptance":  "final_acceptance_agent",
    "compliance_check":  "review_changes_agent",
    # Correction triage is a read-only reviewer pass (ADR 0085) — it reuses
    # the review slot, same precedent as ``compliance_check``.
    "correction_triage": "review_changes_agent",
}


@dataclass
class PhaseAgentConfig:
    """Concrete agents bound to each pipeline phase. Field names mirror
    workflow-semantic phase IDs (ADR 0022)."""

    plan_agent:                IAgentRuntime
    validate_plan_agent:       IAgentRuntime
    implement_agent:           IAgentRuntime
    review_changes_agent:      IAgentRuntime
    repair_changes_agent:      IAgentRuntime
    repair_escalation_agent:   IAgentRuntime
    final_acceptance_agent:    IAgentRuntime

    @classmethod
    def default(
        cls,
        registry: AgentRegistry,
    ) -> "PhaseAgentConfig":
        """Build a default config from AppConfig.

        AppConfig is imported lazily so test suites that call
        ``importlib.reload(core.config)`` — replacing the AppConfig class
        object in ``sys.modules`` — still see the current class on each call.
        """
        import core.infra.config as _core_config
        app = _core_config.AppConfig.load()
        models    = app.phase_model_map
        # AppConfig still calls these values "providers" in JSON/env for this
        # substep; AgentRegistry interprets each value as a runtime id.
        phase_runtimes = app.phase_runtime_map
        efforts   = app.phase_effort_map

        def _slot(phase: str) -> IAgentRuntime:
            return registry.resolve(
                models[phase],
                _resolve_runtime(phase, phase_runtimes),
                effort=efforts.get(phase),
            )

        return cls(
            plan_agent              = _slot("plan"),
            validate_plan_agent     = _slot("validate_plan"),
            implement_agent         = _slot("implement"),
            review_changes_agent    = _slot("review_changes"),
            repair_changes_agent    = _slot("repair_changes"),
            repair_escalation_agent = _slot("repair_escalation"),
            final_acceptance_agent  = _slot("final_acceptance"),
        )
