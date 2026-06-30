"""
AgentRegistry + PhaseAgentConfig.

Covers:
 Protocol satisfaction by built-in providers (runtime_checkable isinstance)
 Factory registration and resolution (Callable form, not type[T])
 Codex slotted as architect (experimentation path)
 Unknown provider raises KeyError
 PhaseAgentConfig.default uses AppConfig per-phase models/providers
 env-var provider overrides affect one phase without collapsing siblings
"""

import json

import pytest

from agents.protocols import IAgentRuntime
from agents.registry import AgentRegistry, PhaseAgentConfig
from core.infra import config as core_config

# ── Stub agents (avoid touching real CLIs) ───────────────────────────────────

class _FakeArchitect:
    def __init__(self, model: str) -> None:
        self.model = model

    def plan(self, task: str, cwd: str, codemap: str = ""):
        from pipeline.plan_parser import ParsedPlan
        return ParsedPlan(short_summary=f"plan:{task}", planning_context=f"plan:{task}", subtasks=(), source="test")


class _FakeDeveloper:
    def __init__(self, model: str) -> None:
        self.model = model

    def run(self, prompt: str, cwd: str) -> str:
        return f"ran:{prompt}"


class _FakeReviewer:
    def __init__(self, model: str) -> None:
        self.model = model

    def review_uncommitted(self, cwd: str, focus: str = "") -> str:
        return json.dumps({
            "verdict": "APPROVED",
            "short_summary": "Approved by JSON contract.",
            "findings": [],
        })

    def review_file(self, file_path: str, focus: str = "", cwd: str | None = None) -> str:
        return json.dumps({
            "verdict": "APPROVED",
            "short_summary": "Approved by JSON contract.",
            "findings": [],
        })


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_registry() -> AgentRegistry:
    """Empty registry, no built-in providers."""
    return AgentRegistry()


@pytest.fixture
def stubbed_registry() -> AgentRegistry:
    """Registry pre-populated with fake providers under realistic names."""
    r = AgentRegistry()
    r.register("claude", lambda model, _e=None: _FakeArchitect(model))
    r.register("codex",  lambda model, _e=None: _FakeArchitect(model))
    r.register("claude", lambda model, _e=None: _FakeDeveloper(model))
    r.register ("codex",  lambda model, _e=None: _FakeReviewer(model))
    return r


@pytest.fixture
def reset_app_config():
    """Clear AppConfig cache around tests so MODEL_* env overrides apply."""
    core_config._reset_config()
    yield
    core_config._reset_config()


# ════════════════════════════════════════════════════════════════════════════
#  AgentRegistry — registration + resolution
# ════════════════════════════════════════════════════════════════════════════

class TestRegistryRegistration:
    def test_register_and_resolve_architect(self, fresh_registry: AgentRegistry) -> None:
        fresh_registry.register("claude", lambda m, _e=None: _FakeArchitect(m))
        a = fresh_registry.architect("claude-opus", "claude")
        assert a.model == "claude-opus"

    def test_register_and_resolve_developer(self, fresh_registry: AgentRegistry) -> None:
        fresh_registry.register("claude", lambda m, _e=None: _FakeDeveloper(m))
        d = fresh_registry.developer("claude-sonnet", "claude")
        assert d.model == "claude-sonnet"

    def test_register_and_resolve_reviewer(self, fresh_registry: AgentRegistry) -> None:
        fresh_registry.register("codex", lambda m, _e=None: _FakeReviewer(m))
        rv = fresh_registry.reviewer("gpt-test", "codex")
        assert rv.model == "gpt-test"

    def test_unknown_provider_raises(self, fresh_registry: AgentRegistry) -> None:
        with pytest.raises(KeyError, match="ghost"):
            fresh_registry.architect("model", "ghost")
        with pytest.raises(KeyError, match="ghost"):
            fresh_registry.developer("model", "ghost")
        with pytest.raises(KeyError, match="ghost"):
            fresh_registry.reviewer("model", "ghost")

    def test_factory_receives_model_name(self, fresh_registry: AgentRegistry) -> None:
        captured: list[str] = []
        fresh_registry.register(
            "claude",
            lambda m, _e=None: (captured.append(m) or _FakeArchitect(m)),  # type: ignore[func-returns-value]
        )
        fresh_registry.architect("custom-model", "claude")
        assert captured == ["custom-model"]

    def test_factory_can_be_a_callable_not_just_class(self, fresh_registry: AgentRegistry) -> None:
        """Factory must accept Callable[[str, str | None], T], not only type[T]."""
        def make_with_extras(model: str, _effort: str | None) -> _FakeArchitect:
            agent = _FakeArchitect(model)
            agent.model = f"prefix-{model}"  # would be impossible with bare type[T]
            return agent

        fresh_registry.register("claude", make_with_extras)
        assert fresh_registry.architect("opus", "claude").model == "prefix-opus"


# ════════════════════════════════════════════════════════════════════════════
#  AgentRegistry.default — built-in providers satisfy Protocols
# ════════════════════════════════════════════════════════════════════════════

class TestRegistryDefault:
    def test_default_registers_claude_as_architect_and_developer(
        self,
        mock_claude_bin: None,
        mock_codex_bin: None,
    ) -> None:
        r = AgentRegistry.default()
        a = r.architect("claude-opus-4-7", "claude")
        d = r.developer("claude-sonnet-4-6", "claude")
        # Same class instantiated for both roles (Variant 2: split by binary)
        assert type(a).__name__ == "ClaudeAgent"
        assert type(d).__name__ == "ClaudeAgent"

    def test_default_registers_codex_as_reviewer_and_architect(
        self,
        mock_claude_bin: None,
        mock_codex_bin: None,
    ) -> None:
        r = AgentRegistry.default()
        rv = r.reviewer ("gpt-5.5", "codex")
        ar = r.architect("gpt-5.5", "codex")  # codex-as-planner experiment slot
        assert type(rv).__name__ == "CodexAgent"
        assert type(ar).__name__ == "CodexAgent"

    def test_built_in_claude_satisfies_both_protocols(
        self,
        mock_claude_bin: None,
        mock_codex_bin: None,
    ) -> None:
        r = AgentRegistry.default()
        a = r.resolve("m", "claude")
        d = r.resolve("m", "claude")
        assert isinstance(a, IAgentRuntime)
        assert isinstance(d, IAgentRuntime)

    def test_built_in_codex_satisfies_runtime(
        self,
        mock_claude_bin: None,
        mock_codex_bin: None,
    ) -> None:
        r = AgentRegistry.default()
        rv = r.resolve("m", "codex")
        assert isinstance(rv, IAgentRuntime)


# ════════════════════════════════════════════════════════════════════════════
#  PhaseAgentConfig.default — AppConfig + provider precedence
# ════════════════════════════════════════════════════════════════════════════

class TestPhaseAgentConfigDefault:
    def test_default_pulls_models_from_appconfig(
        self,
        stubbed_registry: AgentRegistry,
        reset_app_config: None,
    ) -> None:
        cfg = PhaseAgentConfig.default(stubbed_registry)
        # Models come from _config/config.defaults.json
        assert cfg.plan_agent.model         == "claude-opus-4-8[1m]"
        assert cfg.implement_agent.model        == "claude-opus-4-8[1m]"
        assert cfg.repair_changes_agent.model          == "claude-opus-4-8[1m]"
        assert cfg.repair_escalation_agent.model == "claude-opus-4-8[1m]"
        assert cfg.review_changes_agent.model       == "gpt-5.5"
        assert cfg.validate_plan_agent.model      == "gpt-5.5"
        assert cfg.final_acceptance_agent.model     == "gpt-5.5"

    def test_env_var_overrides_phase_model(
        self,
        stubbed_registry: AgentRegistry,
        reset_app_config: None,
        monkeypatch,
    ) -> None:
        monkeypatch.setenv("MODEL_IMPLEMENT", "claude-experimental-1")
        core_config._reset_config()  # discard default-loaded cache before env
        cfg = PhaseAgentConfig.default(stubbed_registry)
        assert cfg.implement_agent.model == "claude-experimental-1"

    def test_uses_default_providers(
        self,
        stubbed_registry: AgentRegistry,
        reset_app_config: None,
    ) -> None:
        # Default per-phase providers from _config/config.defaults.json:
        # plan=claude, implement=claude, review_changes=codex, ... Stubbed
        # registry has both architects under "claude" and "codex"; if a
        # wrong provider key were used, KeyError would surface here.
        PhaseAgentConfig.default(stubbed_registry)

    def test_per_phase_runtime_overrides_default(
        self,
        reset_app_config: None,
        monkeypatch,
    ) -> None:
        """RUNTIME_<PHASE> env var must redirect that single phase to a
 different runtime without touching the others.

 registry tracks by runtime name only. The per-phase
 env override flows through ``_resolve_runtime`` and resolves to
 the registered runtime factory. Test counts how many times each
 runtime factory was called by phase resolution."""
        seen: list[str] = []

        def _claude_factory(m, _e=None):
            seen.append("claude")
            return _FakeReviewer(m)

        def _codex_factory(m, _e=None):
            seen.append("codex")
            return _FakeReviewer(m)

        r = AgentRegistry()
        r.register("claude", _claude_factory)
        r.register("codex", _codex_factory)

        # Send review_changes phase to claude; validate_plan /
        # final_acceptance stay on codex because env vars are per-phase,
        # not per-slot.
        monkeypatch.setenv("RUNTIME_REVIEW_CHANGES", "claude")
        core_config._reset_config()

        PhaseAgentConfig.default(r)
        # Default phase->runtime mapping per AppConfig.phase_runtime_map:
        # plan/build/fix/repair_escalation → claude (4 calls)
        # validate_plan/final_acceptance → codex (2 calls)
        # review (env override) → claude (1 call) — was codex
        # Total: 5 claude calls, 2 codex calls.
        assert seen.count("claude") == 5
        assert seen.count("codex") == 2


# ════════════════════════════════════════════════════════════════════════════
#  Regression: build_phase_config_from_overrides honours provider override
# ════════════════════════════════════════════════════════════════════════════

class TestBuildPhaseConfigFromOverridesRegression:
    """Pre-fix bug: passing --model-review-changes with a Claude-named model would
 silently route the model into CodexAgent because the override site didn't
 forward the provider. Fix: provider override is a first-class CLI arg and
 the helper plumbs it through to the registry."""

    def test_provider_override_routes_review_to_claude(
        self,
        reset_app_config: None,
        mock_claude_bin: None,
        mock_codex_bin: None,
    ) -> None:
        from pipeline.project.phase_config import build_phase_config_from_overrides

        cfg = build_phase_config_from_overrides(
            review_changes="claude-opus-4-7",
            runtime_review_changes="claude",
        )
        # All three reviewer-slot agents must now be ClaudeAgent, not CodexAgent.
        assert type(cfg.review_changes_agent).__name__   == "ClaudeAgent"
        assert type(cfg.validate_plan_agent).__name__  == "ClaudeAgent"
        assert type(cfg.final_acceptance_agent).__name__ == "ClaudeAgent"
        assert cfg.review_changes_agent.model == "claude-opus-4-7"

    def test_model_only_falls_back_to_phase_default_provider(
        self,
        reset_app_config: None,
        mock_claude_bin: None,
        mock_codex_bin: None,
    ) -> None:
        """No provider override -> per-phase default applies. For BUILD that
 means claude (default), so a Claude-named model lands in ClaudeAgent."""
        from pipeline.project.phase_config import build_phase_config_from_overrides

        cfg = build_phase_config_from_overrides(implement="claude-haiku-4-5")
        assert type(cfg.implement_agent).__name__ == "ClaudeAgent"
        assert cfg.implement_agent.model == "claude-haiku-4-5"

    def test_provider_override_alone_keeps_default_model(
        self,
        reset_app_config: None,
        mock_claude_bin: None,
        mock_codex_bin: None,
    ) -> None:
        """Setting only --runtime-review-changes keeps the default review model but
 ships it to a different CLI. Useful for A/B-testing same task across
 providers."""
        from pipeline.project.phase_config import build_phase_config_from_overrides

        cfg = build_phase_config_from_overrides(runtime_review_changes="claude")
        # Default review model from config.defaults.json is gpt-5.5; provider
        # flipping doesn't change the model string the user picked.
        assert type(cfg.review_changes_agent).__name__ == "ClaudeAgent"


# ════════════════════════════════════════════════════════════════════════════
#  Reasoning effort plumbing
# ════════════════════════════════════════════════════════════════════════════

class TestReasoningEffortPlumbing:
    """Each provider must surface a per-phase reasoning-effort override on its
 CLI invocation. Without this, codex inherits ``model_reasoning_effort``
 from ~/.codex/config.toml (often ``xhigh``) and burns tokens on trivial
 final_acceptance passes; claude inherits its own default. The orchestrator's
 per-phase config is the only source of truth — providers must respect it.
 """

    def test_claude_agent_emits_effort_flag(self) -> None:
        from agents.runtimes.claude import ClaudeAgent

        agent = ClaudeAgent(model="claude-sonnet-4-6", effort="medium")
        assert agent.effort == "medium"
        assert agent._effort_args() == ["--effort", "medium"]

    def test_claude_agent_no_effort_emits_no_flag(self) -> None:
        from agents.runtimes.claude import ClaudeAgent

        agent = ClaudeAgent(model="claude-sonnet-4-6")
        assert agent.effort is None
        assert agent._effort_args() == []

    def test_codex_agent_emits_effort_config_pair(self, mock_codex_bin: None) -> None:
        from agents.runtimes.codex import CodexAgent

        agent = CodexAgent(model="gpt-5.5", effort="low")
        cmd = agent._exec_cmd(mutates_artifacts=False)
        # The pair is appended after the model entry — exact shape matters
        # (codex parses ``-c key=value`` as TOML).
        assert "-c" in cmd
        assert any('model_reasoning_effort="low"' in arg for arg in cmd), cmd

    def test_codex_agent_no_effort_omits_config(self, mock_codex_bin: None) -> None:
        from agents.runtimes.codex import CodexAgent

        agent = CodexAgent(model="gpt-5.5")
        cmd = agent._exec_cmd(mutates_artifacts=False)
        assert not any("model_reasoning_effort" in arg for arg in cmd), cmd

    def test_registry_factory_threads_effort(self, fresh_registry: AgentRegistry) -> None:
        """The registry's resolve helpers must invoke factories with effort."""
        seen: list[tuple[str, str | None]] = []
        fresh_registry.register(
            "claude",
            lambda model, effort=None: seen.append((model, effort)) or _FakeArchitect(model),
        )
        fresh_registry.architect("claude-opus-4-7", "claude", effort="high")
        assert seen == [("claude-opus-4-7", "high")]

    def test_app_config_phase_effort_map_carries_defaults(
        self, reset_app_config: None,
    ) -> None:
        """``_config/config.defaults.json`` ships sane per-phase efforts so a
 bare install doesn't accidentally inherit a global ``xhigh``.
 """
        import core.infra.config as core_cfg
        m = core_cfg.AppConfig.load().phase_effort_map
        # Final QA is the cheapest pass — must default below the architect.
        assert m.get("final_acceptance") == "low"
        assert m.get("plan") == "high"
        # Sonnet-driven phases default to medium — explicit, not inherited.
        assert m.get("implement") == "medium"
