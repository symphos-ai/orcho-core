"""
Unit tests for config.py.
No filesystem access, no subprocess — pure constant validation.
"""

import importlib
from unittest.mock import MagicMock

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
        assert config.agent_timeout("claude-glm") is None
        assert config.agent_timeout("codex") is None
        assert config.agent_timeout("gemini") is None
        assert config.agent_idle_timeout("claude") == config.CLAUDE_IDLE_TIMEOUT
        assert config.agent_idle_timeout("claude-glm") == config.CLAUDE_IDLE_TIMEOUT
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

    def test_startup_stall_timeout_defaults_to_120_seconds(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ORCHO_STARTUP_STALL_TIMEOUT", raising=False)
        monkeypatch.setenv("ORCHO_DISABLE_LOCAL_CONFIG", "1")
        config.AppConfig.load.cache_clear()
        assert config.AppConfig.load().startup_stall_seconds == 120

    def test_startup_stall_timeout_default_json_overlay_and_env_precedence(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path,
    ) -> None:
        overlay = tmp_path / "config.local.json"
        overlay.write_text('{"timeouts": {"startup_stall_seconds": 37}}')
        monkeypatch.setattr(
            config, "_iter_local_config_paths", lambda **_: iter((overlay,)),
        )
        monkeypatch.delenv("ORCHO_DISABLE_LOCAL_CONFIG", raising=False)
        monkeypatch.delenv("ORCHO_STARTUP_STALL_TIMEOUT", raising=False)
        config.AppConfig.load.cache_clear()
        assert config.AppConfig.load().startup_stall_seconds == 37

        monkeypatch.setenv("ORCHO_STARTUP_STALL_TIMEOUT", "9")
        config.AppConfig.load.cache_clear()
        assert config.AppConfig.load().startup_stall_seconds == 9

    @pytest.mark.parametrize("value", ("0", "-1", "not-a-number"))
    def test_invalid_startup_stall_timeout_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, value: str,
    ) -> None:
        monkeypatch.setenv("ORCHO_STARTUP_STALL_TIMEOUT", value)
        config.AppConfig.load.cache_clear()
        assert config.AppConfig.load().startup_stall_seconds == 120


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


class TestContentLanguage:
    """``content_language`` governs outward delivery artifacts (commit /
    PR), independent of the operator-facing task language."""

    def test_default_is_english(self) -> None:
        config.AppConfig.load.cache_clear()
        app = config.AppConfig.load()
        assert app.content_language == "English"

    def test_json_field_overrides(self) -> None:
        cfg = config.AppConfig(
            phases={}, timeouts={}, session={}, codemap={}, hypothesis={},
            language={"content_language": "ru"}, artifacts={}, pipeline={},
        )
        assert cfg.content_language == "ru"

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONTENT_LANGUAGE", "ru")
        config.AppConfig.load.cache_clear()
        try:
            assert config.AppConfig.load().content_language == "ru"
        finally:
            monkeypatch.delenv("CONTENT_LANGUAGE", raising=False)
            config.AppConfig.load.cache_clear()


class TestCommitSection:
    def test_defaults_present_on_app_config(self) -> None:
        config.AppConfig.load.cache_clear()
        app = config.AppConfig.load()
        assert app.commit["enabled"] is True
        assert app.commit["default_strategy"] == "release_summary"
        assert app.commit["interactive_default"] == "approve"
        assert app.commit["auto_in_ci"] == "approve"
        assert app.commit["add_untracked"] is True


class TestClaudeGlmSection:
    def test_binary_override_wins_and_falls_back_to_plain_claude(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path,
    ) -> None:
        override = tmp_path / "claude-compatible"
        override.touch()
        plain = MagicMock(return_value="/plain/claude")
        monkeypatch.setattr(config, "get_claude_bin", plain)
        monkeypatch.setenv("CLAUDE_GLM_BIN", str(override))
        assert config.get_claude_glm_bin() == str(override)
        plain.assert_not_called()

        monkeypatch.delenv("CLAUDE_GLM_BIN")
        assert config.get_claude_glm_bin() == "/plain/claude"
        plain.assert_called_once_with()

    def test_defaults_and_environment_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in (
            "CLAUDE_GLM_OPUS_MODEL", "CLAUDE_GLM_SONNET_MODEL",
            "CLAUDE_GLM_HAIKU_MODEL", "CLAUDE_GLM_MAX_CONTEXT_TOKENS",
            "CLAUDE_GLM_CONFIG_DIR",
        ):
            monkeypatch.delenv(key, raising=False)
        config.AppConfig.load.cache_clear()
        # An empty ``config_dir`` means "the adapter's per-user default"; the
        # adapter resolves it, so configuration never names a home path.
        assert config.AppConfig.load().claude_glm == {
            "opus_model": "glm-5.3",
            "sonnet_model": "glm-5.3",
            "haiku_model": "glm-4.7",
            "max_context_tokens": 200000,
            "config_dir": "",
        }
        monkeypatch.setenv("CLAUDE_GLM_OPUS_MODEL", "custom-opus")
        monkeypatch.setenv("CLAUDE_GLM_SONNET_MODEL", "custom-sonnet")
        monkeypatch.setenv("CLAUDE_GLM_HAIKU_MODEL", "custom-haiku")
        monkeypatch.setenv("CLAUDE_GLM_MAX_CONTEXT_TOKENS", "123456")
        monkeypatch.setenv("CLAUDE_GLM_CONFIG_DIR", "/tmp/custom-glm-config")
        config.AppConfig.load.cache_clear()
        assert config.AppConfig.load().claude_glm == {
            "opus_model": "custom-opus",
            "sonnet_model": "custom-sonnet",
            "haiku_model": "custom-haiku",
            "max_context_tokens": 123456,
            "config_dir": "/tmp/custom-glm-config",
        }

    @pytest.mark.parametrize("value", ["0", "-1", "invalid"])
    def test_context_override_requires_positive_integer(
        self, monkeypatch: pytest.MonkeyPatch, value: str,
    ) -> None:
        monkeypatch.setenv("CLAUDE_GLM_MAX_CONTEXT_TOKENS", value)
        config.AppConfig.load.cache_clear()
        with pytest.raises(ValueError, match="positive integer"):
            config.AppConfig.load()
