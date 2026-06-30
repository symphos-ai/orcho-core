"""
Runtime-neutrality regression tests.

Pin the construction-side invariants from the zazzy-deer refactor:

* ``RealAgentProvider.resolve(...)`` delegates to ``AgentRegistry.resolve``.
* ``MockAgentProvider.resolve("codex", ...)`` returns the same singleton
  the legacy ``.codex(...)`` shim returns (validate-plan reject counter
  contract). ``.resolve("gemini", ...)`` returns a ``_MockClaude`` whose
  ``runtime`` attribute is stamped with the requested id.
* ``_synthesize_phase_config`` constructs every slot via
  ``provider.resolve(runtime, model, effort=...)`` — including custom
  runtime ids pulled from ``AppConfig.phase_runtime_map``.
* ``build_phase_config_from_overrides`` honours CLI ``--runtime-*``
  overrides via ``registry.resolve``.
* ``_resolve_cross_level_agent`` always returns a fresh instance
  (no session bleed with ``phase_config`` slots) and reads
  runtime/model/effort metadata from the supplied ``phase_config``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ──────────────────────────────────────────────────────────────────────────
# Recording fixtures
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class _RecordedCall:
    runtime: str
    model: str
    effort: str | None


class _FakeAgent:
    """Minimal IAgentRuntime-shaped object — the resolution tests only
    care about identity / stamped runtime / model attributes."""

    def __init__(self, runtime: str, model: str, effort: str | None) -> None:
        self.runtime = runtime
        self.model = model
        self.effort = effort
        self.session_id: str | None = None
        self._followup_resume_pending = False
        self._last_resumed_session_id = None
        self._last_followup_parent_session_id = None

    def invoke(self, *_args, **_kwargs) -> str:
        return ""

    def reset_session(self) -> None:
        self.session_id = None


class _RecordingProvider:
    """Records every ``.resolve(...)`` call and returns a fresh
    ``_FakeAgent`` each time."""

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []
        self.returned: list[_FakeAgent] = []

    def resolve(self, runtime: str, model: str, *, effort: str | None = None):
        self.calls.append(_RecordedCall(runtime, model, effort))
        agent = _FakeAgent(runtime, model, effort)
        self.returned.append(agent)
        return agent

    # Shims so any test path still using the legacy names also routes
    # through resolve (mirrors the production providers).
    def claude(self, model: str, *, effort: str | None = None):
        return self.resolve("claude", model, effort=effort)

    def codex(self, model: str, *, effort: str | None = None):
        return self.resolve("codex", model, effort=effort)

    def gemini(self, model: str, *, effort: str | None = None):
        return self.resolve("gemini", model, effort=effort)

    def run_tests(self, _cwd: str, _plugin: Any) -> None:
        return None


# ──────────────────────────────────────────────────────────────────────────
# Provider Strategy — Step 2 contract
# ──────────────────────────────────────────────────────────────────────────


class TestProviderResolveContract:

    def test_real_provider_resolve_delegates_to_registry(self) -> None:
        from agents.registry import AgentRegistry
        from agents.runtimes import RealAgentProvider
        from agents.runtimes.claude import ClaudeAgent

        provider = RealAgentProvider()
        # ``resolve`` should land on AgentRegistry.resolve — exercise the
        # real entry-point table built by ``AgentRegistry.default()``.
        agent = provider.resolve("claude", "any-model")
        assert isinstance(agent, ClaudeAgent)
        # Registry cache: a second resolve doesn't re-build the registry.
        provider.resolve("claude", "any-model-2")
        assert isinstance(provider._get_registry(), AgentRegistry)

    def test_real_provider_named_shims_route_through_resolve(self) -> None:
        from agents.runtimes import RealAgentProvider
        from agents.runtimes.claude import ClaudeAgent
        from agents.runtimes.codex import CodexAgent

        provider = RealAgentProvider()
        assert isinstance(provider.claude("any"), ClaudeAgent)
        assert isinstance(provider.codex("any"), CodexAgent)

    def test_mock_codex_singleton_via_resolve(self) -> None:
        from agents.runtimes._strategy import MockAgentProvider

        p = MockAgentProvider(latency=0.0, validate_plan_reject_rounds=2)
        first = p.resolve("codex", "any")
        second = p.resolve("codex", "different")
        # ``.codex()`` shim must reach the same singleton — that's the
        # reject-counter invariant.
        third = p.codex("yet-another")
        assert first is second is third

    def test_mock_resolve_stamps_requested_runtime(self) -> None:
        """A `_MockClaude` returned for runtime='gemini' must report
        ``runtime == 'gemini'`` so pipeline chips / metrics / evidence /
        prompt-session keys stay aligned with the requested id."""
        from agents.runtimes._strategy import MockAgentProvider

        p = MockAgentProvider(latency=0.0)
        gemini_agent = p.resolve("gemini", "any-model")
        assert gemini_agent.runtime == "gemini"
        custom_agent = p.resolve("acme-runtime", "any-model")
        assert custom_agent.runtime == "acme-runtime"
        # And the codex singleton must report 'codex' regardless of order
        # in which it was first resolved.
        codex_agent = p.resolve("codex", "any-model")
        assert codex_agent.runtime == "codex"

    def test_failing_mock_resolve_stamps_runtime(self) -> None:
        from agents.runtimes import FailingMockProvider

        p = FailingMockProvider(fail_times=0, error_type="rate_limit")
        assert p.resolve("claude", "any").runtime == "claude"
        assert p.resolve("codex", "any").runtime == "codex"
        assert p.resolve("gemini", "any").runtime == "gemini"
        assert p.resolve("acme", "any").runtime == "acme"


# ──────────────────────────────────────────────────────────────────────────
# Step 3a — _synthesize_phase_config
# ──────────────────────────────────────────────────────────────────────────


class TestSynthesizePhaseConfigGeneric:

    def test_resolve_called_for_every_slot_with_phase_map_runtimes(
        self, monkeypatch
    ) -> None:
        """Setting a custom runtime in ``phase_runtime_map`` must route
        the matching slot through ``provider.resolve(<custom>, ...)`` —
        no Claude/Codex hardcoding."""
        from core.infra import config as core_config
        from pipeline.project.runtime_setup import _synthesize_phase_config

        # Override only what the helper reads off AppConfig.
        class _FakeApp:
            phase_runtime_map = {
                "plan":              "claude",
                "validate_plan":     "codex",
                "implement":         "acme",  # custom runtime id
                "review_changes":    "codex",
                "repair_changes":    "claude",
                "repair_escalation": "claude",
                "final_acceptance":  "codex",
            }
            phase_model_map = {
                "plan":              "plan-m",
                "validate_plan":     "vp-m",
                "implement":         "build-m",
                "review_changes":    "rev-m",
                "repair_changes":    "rep-m",
                "repair_escalation": "rep-esc-m",
                "final_acceptance":  "fa-m",
            }
            phase_effort_map = {"plan": "high", "implement": "medium"}

        monkeypatch.setattr(core_config.AppConfig, "load",
                            staticmethod(lambda: _FakeApp()))

        provider = _RecordingProvider()
        cfg = _synthesize_phase_config(
            None,
            _provider=provider,
            plan_model="fallback-plan",
            implement_model="fallback-implement",
            repair_model="fallback-repair",
            repair_escalation_model="fallback-rep-esc",
            review_model="fallback-review",
        )

        # Every slot got a fresh agent via resolve.
        by_runtime = {(c.runtime, c.model) for c in provider.calls}
        assert ("claude", "plan-m") in by_runtime
        assert ("codex", "vp-m") in by_runtime
        assert ("acme", "build-m") in by_runtime, (
            "implement slot must resolve via the custom runtime id from "
            "phase_runtime_map — no Claude/Codex hardcoding"
        )
        assert ("codex", "rev-m") in by_runtime
        assert ("claude", "rep-m") in by_runtime
        assert ("claude", "rep-esc-m") in by_runtime
        assert ("codex", "fa-m") in by_runtime

        # Effort is forwarded per phase.
        plan_call = next(c for c in provider.calls if c.runtime == "claude" and c.model == "plan-m")
        assert plan_call.effort == "high"
        implement_call = next(c for c in provider.calls if c.runtime == "acme")
        assert implement_call.effort == "medium"

        # The returned config holds the recorder's returned agents.
        assert cfg.implement_agent.runtime == "acme"
        assert cfg.plan_agent.runtime == "claude"

    def test_empty_string_runtime_falls_back_to_claude(
        self, monkeypatch
    ) -> None:
        """An empty-string entry in phase_runtime_map must fall back —
        ``or`` semantics, not ``.get(..., default)``."""
        from core.infra import config as core_config
        from pipeline.project.runtime_setup import _synthesize_phase_config

        class _EmptyApp:
            phase_runtime_map = {"plan": ""}
            phase_model_map = {}
            phase_effort_map = {}

        monkeypatch.setattr(core_config.AppConfig, "load",
                            staticmethod(lambda: _EmptyApp()))

        provider = _RecordingProvider()
        _synthesize_phase_config(
            None, _provider=provider,
            plan_model="p", implement_model="b",
            repair_model="r", repair_escalation_model="re",
            review_model="rv",
        )
        plan_call = next(c for c in provider.calls if c.model == "p")
        assert plan_call.runtime == "claude"

    def test_phase_config_passes_through_unchanged(self) -> None:
        from agents.registry import PhaseAgentConfig
        from pipeline.project.runtime_setup import _synthesize_phase_config

        # When the caller supplies a phase_config, no resolve calls fire.
        sentinel = PhaseAgentConfig(
            plan_agent=_FakeAgent("x", "x", None),
            validate_plan_agent=_FakeAgent("x", "x", None),
            implement_agent=_FakeAgent("x", "x", None),
            review_changes_agent=_FakeAgent("x", "x", None),
            repair_changes_agent=_FakeAgent("x", "x", None),
            repair_escalation_agent=_FakeAgent("x", "x", None),
            final_acceptance_agent=_FakeAgent("x", "x", None),
        )
        provider = _RecordingProvider()
        out = _synthesize_phase_config(
            sentinel, _provider=provider,
            plan_model="p", implement_model="b",
            repair_model="r", repair_escalation_model="re",
            review_model="rv",
        )
        assert out is sentinel
        assert provider.calls == []


# ──────────────────────────────────────────────────────────────────────────
# Step 3b — build_phase_config_from_overrides
# ──────────────────────────────────────────────────────────────────────────


class TestBuildPhaseConfigFromOverrides:

    def test_custom_runtime_override_routes_through_resolve(self) -> None:
        # Register a custom runtime under "acme" via a one-off registry
        # the helper uses. Since the helper calls
        # ``AgentRegistry.default()`` internally, we replace the default
        # discovery with a small registry by monkey-patching the entry
        # points isn't necessary here — instead we register through the
        # existing default and add "acme" on top.
        # The helper builds its own registry via ``AgentRegistry.default()``,
        # so we patch that single call to return our augmented registry.
        import pipeline.project.phase_config as pc_mod
        from agents.registry import AgentRegistry
        from pipeline.project.phase_config import (
            build_phase_config_from_overrides,
        )

        class _AcmeAgent:
            runtime = "acme"

            def __init__(self, model: str, effort: str | None = None) -> None:
                self.model = model
                self.effort = effort

            def invoke(self, *a, **kw):  # noqa: ARG002
                return ""

            def reset_session(self):
                pass

        real_default = AgentRegistry.default

        def _augmented_default():
            r = real_default()
            r.register("acme", lambda m, e=None: _AcmeAgent(m, e))
            return r

        original = pc_mod.AgentRegistry.default
        pc_mod.AgentRegistry.default = staticmethod(_augmented_default)
        try:
            cfg = build_phase_config_from_overrides(
                implement="custom-model",
                runtime_implement="acme",
            )
        finally:
            pc_mod.AgentRegistry.default = original

        assert cfg.implement_agent.runtime == "acme"
        assert cfg.implement_agent.model == "custom-model"


# ──────────────────────────────────────────────────────────────────────────
# Step 3c — cross-level resolution
# ──────────────────────────────────────────────────────────────────────────


class TestResolveCrossLevelAgent:

    def test_uses_fallbacks_without_phase_config(self, monkeypatch) -> None:
        from core.infra import config as core_config
        from pipeline.cross_project.agent_setup import _resolve_cross_level_agent

        class _BareApp:
            # No phase maps at all → fallbacks win.
            pass

        monkeypatch.setattr(core_config.AppConfig, "load",
                            staticmethod(lambda: _BareApp()))

        provider = _RecordingProvider()
        agent = _resolve_cross_level_agent(
            provider,
            phase="plan",
            phase_config=None,
            fallback_model="plan-default-model",
            fallback_runtime="claude",
        )
        assert provider.calls == [
            _RecordedCall("claude", "plan-default-model", None)
        ]
        assert agent.runtime == "claude"
        assert agent.model == "plan-default-model"

    def test_phase_config_metadata_drives_fresh_resolve(self) -> None:
        """When phase_config is supplied, runtime/model/effort come from
        the slot — but the returned agent must NOT be the slot itself
        (no session bleed)."""
        from agents.registry import PhaseAgentConfig
        from pipeline.cross_project.agent_setup import _resolve_cross_level_agent

        plan_slot = _FakeAgent("custom-plan-runtime", "custom-plan-model", "high")
        review_slot = _FakeAgent("custom-review-runtime", "custom-review-model", "low")
        cfg = PhaseAgentConfig(
            plan_agent=plan_slot,
            validate_plan_agent=_FakeAgent("x", "x", None),
            implement_agent=_FakeAgent("x", "x", None),
            review_changes_agent=review_slot,
            repair_changes_agent=_FakeAgent("x", "x", None),
            repair_escalation_agent=_FakeAgent("x", "x", None),
            final_acceptance_agent=_FakeAgent("x", "x", None),
        )
        provider = _RecordingProvider()

        plan_agent = _resolve_cross_level_agent(
            provider, phase="plan", phase_config=cfg,
            fallback_model="ignored", fallback_runtime="claude",
        )
        review_agent = _resolve_cross_level_agent(
            provider, phase="review_changes", phase_config=cfg,
            fallback_model="ignored", fallback_runtime="codex",
        )

        assert provider.calls[0] == _RecordedCall(
            "custom-plan-runtime", "custom-plan-model", "high",
        )
        assert provider.calls[1] == _RecordedCall(
            "custom-review-runtime", "custom-review-model", "low",
        )
        # No session bleed: fresh instances, not aliases of slot agents.
        assert plan_agent is not plan_slot
        assert review_agent is not review_slot

    def test_appconfig_phase_runtime_map_consulted_without_phase_config(
        self, monkeypatch
    ) -> None:
        from core.infra import config as core_config
        from pipeline.cross_project.agent_setup import _resolve_cross_level_agent

        class _App:
            phase_runtime_map = {"plan": "acme"}
            phase_model_map = {"plan": "acme-plan-model"}
            phase_effort_map = {"plan": "max"}

        monkeypatch.setattr(core_config.AppConfig, "load",
                            staticmethod(lambda: _App()))

        provider = _RecordingProvider()
        _resolve_cross_level_agent(
            provider, phase="plan", phase_config=None,
            fallback_model="fb-model", fallback_runtime="claude",
        )
        assert provider.calls == [_RecordedCall("acme", "acme-plan-model", "max")]
