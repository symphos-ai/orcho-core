"""``_resolve_tests_config(plugin)`` reads from
``plugin.quality_gates["tests"]`` dict exclusively (the legacy
``plugin.testing`` field was deleted). Empty / missing config →
``TestingConfig()`` default (skipped semantics).

These tests pin the dict→TestingConfig coercion so customer
plugin.py files using
``PLUGIN["quality_gates"] = {"tests": {...}}`` get identical
behaviour to the now-deleted ``PLUGIN["testing"] = {...}`` shape.
"""
from __future__ import annotations

from pipeline.plugins import PluginConfig, TestSuiteConfig
from pipeline.project_testing import resolve_tests_config as _resolve_tests_config

# ── plugin.quality_gates["tests"] dict is the only source ────────────────────

class TestQualityGatesDictResolution:
    def test_dict_with_run_command(self) -> None:
        plugin = PluginConfig(
            quality_gates={"tests": {"run_command": "pytest",
                                     "fail_keyword": "FAILED"}},
        )
        cfg = _resolve_tests_config(plugin)
        assert cfg.run_command == "pytest"
        assert cfg.fail_keyword == "FAILED"

    def test_dict_with_suites_coerces_correctly(self) -> None:
        plugin = PluginConfig(
            quality_gates={"tests": {
                "suites": [
                    {"name": "edit_mode", "run_command": "unity-edit",
                     "fail_keyword": "FAILED", "timeout": 60},
                    {"name": "play_mode", "run_command": "unity-play",
                     "fail_keyword": "FAILED", "timeout": 120},
                ],
            }},
        )
        cfg = _resolve_tests_config(plugin)
        assert len(cfg.suites) == 2
        assert all(isinstance(s, TestSuiteConfig) for s in cfg.suites)
        assert cfg.suites[0].name == "edit_mode"
        assert cfg.suites[0].timeout == 60
        assert cfg.suites[1].run_command == "unity-play"

    def test_unknown_keys_dropped_silently(self) -> None:
        """Forward-compat: unknown keys (e.g. customer adds a future
 field) shouldn't crash. Mirror ``load_plugin``'s filter."""
        plugin = PluginConfig(
            quality_gates={"tests": {
                "run_command": "x",
                "future_field": "ignored",
            }},
        )
        cfg = _resolve_tests_config(plugin)
        assert cfg.run_command == "x"


# ── No config → empty default (skipped semantics) ────────────────────────────

class TestEmptyConfig:
    def test_empty_quality_gates_dict(self) -> None:
        """Empty quality_gates dict → empty TestingConfig (skipped)."""
        plugin = PluginConfig(quality_gates={})
        cfg = _resolve_tests_config(plugin)
        assert cfg.run_command is None
        assert cfg.suites == []

    def test_dict_with_only_other_gates(self) -> None:
        """quality_gates has lint config but no tests → empty TestingConfig."""
        plugin = PluginConfig(
            quality_gates={"lint": {"run_command": "ruff"}},
        )
        cfg = _resolve_tests_config(plugin)
        assert cfg.run_command is None


# ── Priority 3: empty default ────────────────────────────────────────────────

class TestEmptyDefault:
    def test_no_config_returns_skipped_default(self) -> None:
        plugin = PluginConfig()  # both unset
        cfg = _resolve_tests_config(plugin)
        # Default TestingConfig: run_command=None, suites=[] → skipped
        assert cfg.run_command is None
        assert cfg.suites == []
