"""
Unit tests for config.py.
No filesystem access, no subprocess — pure constant validation.
"""

import importlib

import pytest

from core.infra import config


class TestPhaseModelDefaults:
    """JSON-layer defaults reachable via ``config.phase_model(phase, default)``."""

    def test_plan_default_is_opus(self) -> None:
        assert "opus" in config.phase_model("plan", "")

    def test_implement_default_is_opus(self) -> None:
        assert config.phase_model("implement", "") == "claude-opus-4-8[1m]"

    def test_repair_escalation_default_is_opus(self) -> None:
        assert "opus" in config.phase_model("repair_escalation", "")

    def test_codex_model_nonempty(self) -> None:
        assert len(config.CODEX_MODEL) > 0

    def test_unknown_phase_returns_caller_default(self) -> None:
        assert config.phase_model("not-a-phase", "fallback-xyz") == "fallback-xyz"


class TestEnvOverrides:
    def test_codex_model_overridable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODEX_MODEL", "o3")
        importlib.reload(config)
        assert config.CODEX_MODEL == "o3"
        importlib.reload(config)

    def test_model_implement_env_override_via_appconfig(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``MODEL_<PHASE>`` is the canonical per-phase override; it
        flows through ``AppConfig.load()`` rather than the cheap
        ``phase_model()`` JSON-only lookup."""
        monkeypatch.setenv("MODEL_IMPLEMENT", "claude-custom-impl")
        config.AppConfig.load.cache_clear()
        app = config.AppConfig.load()
        assert app.phase_model_map["implement"] == "claude-custom-impl"
        monkeypatch.delenv("MODEL_IMPLEMENT")
        config.AppConfig.load.cache_clear()


class TestTimeouts:
    def test_hard_timeouts_disabled_by_default(self) -> None:
        assert config.CLAUDE_TIMEOUT is None
        assert config.CODEX_TIMEOUT is None
        assert config.GEMINI_TIMEOUT is None

    def test_idle_timeouts_enabled_by_default(self) -> None:
        assert config.CLAUDE_IDLE_TIMEOUT is not None
        assert config.CLAUDE_IDLE_TIMEOUT > 0
        assert config.CODEX_IDLE_TIMEOUT is not None
        assert config.CODEX_IDLE_TIMEOUT > 0
        assert config.GEMINI_IDLE_TIMEOUT is not None
        assert config.GEMINI_IDLE_TIMEOUT > 0

    def test_idle_timeout_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_IDLE_TIMEOUT", "7")
        importlib.reload(config)
        assert config.CLAUDE_IDLE_TIMEOUT == 7
        importlib.reload(config)

    def test_provider_timeout_helpers_include_gemini_stub(self) -> None:
        assert config.agent_timeout("claude") is None
        assert config.agent_timeout("codex") is None
        assert config.agent_timeout("gemini") is None
        assert config.agent_idle_timeout("claude") == config.CLAUDE_IDLE_TIMEOUT
        assert config.agent_idle_timeout("codex") == config.CODEX_IDLE_TIMEOUT
        assert config.agent_idle_timeout("gemini") == config.GEMINI_IDLE_TIMEOUT

    def test_app_config_timeout_properties(self) -> None:
        config.AppConfig.load.cache_clear()
        app = config.AppConfig.load()
        assert app.claude_timeout == 0
        assert app.codex_timeout == 0
        assert app.gemini_timeout == 0
        assert app.claude_idle_timeout > 0
        assert app.codex_idle_timeout > 0
        assert app.gemini_idle_timeout > 0


# ── Stage 4: pipeline section ───────────────────────────────────────────────


class TestPipelineSection:
    """AppConfig.pipeline carries plan ↔ validate_plan loop knobs."""

    def _fresh_config(self):
        # AppConfig.load is @cache'd; clear so monkeypatched env applies.
        from core.infra.config import AppConfig
        AppConfig.load.cache_clear()
        return AppConfig.load()

    def test_pipeline_section_present(self) -> None:
        cfg = self._fresh_config()
        assert isinstance(cfg.pipeline, dict)
        assert "change_handoff" in cfg.pipeline
        assert cfg.pipeline["session_split_override"] == {}

    def test_session_split_override_env_parses_phase_map(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "ORCHO_SESSION_SPLIT_OVERRIDE",
            "implement=common,repair_changes=per_role",
        )
        cfg = self._fresh_config()
        assert cfg.pipeline["session_split_override"] == {
            "implement": "common",
            "repair_changes": "per_role",
        }

    def test_session_split_override_rejects_unknown_split(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ORCHO_SESSION_SPLIT_OVERRIDE", "implement=sticky")
        config.AppConfig.load.cache_clear()
        with pytest.raises(ValueError, match="session_split_override"):
            config.AppConfig.load()


class TestAccountingSection:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ORCHO_ACCOUNTING", raising=False)
        config._reset_config()
        try:
            assert config.accounting_enabled() is False
            assert config.AppConfig.load().accounting["enabled"] is False
        finally:
            config._reset_config()

    def test_env_override_enables_accounting(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
        config._reset_config()
        try:
            assert config.accounting_enabled() is True
        finally:
            config._reset_config()


class TestPreRunDirtySection:
    def test_defaults_present_on_app_config(self) -> None:
        config.AppConfig.load.cache_clear()
        app = config.AppConfig.load()
        assert app.pre_run_dirty["enabled"] is True
        assert app.pre_run_dirty["interactive_default"] == "include"
        assert app.pre_run_dirty["non_interactive_default"] == "halt"
        assert app.pre_run_dirty["include_untracked"] == "prompt"


class TestCommitSection:
    def test_defaults_present_on_app_config(self) -> None:
        config.AppConfig.load.cache_clear()
        app = config.AppConfig.load()
        assert app.commit["enabled"] is True
        assert app.commit["default_strategy"] == "release_summary"
        assert app.commit["interactive_default"] == "apply"
        assert app.commit["auto_in_ci"] == "approve"
        assert app.commit["add_untracked"] is True
