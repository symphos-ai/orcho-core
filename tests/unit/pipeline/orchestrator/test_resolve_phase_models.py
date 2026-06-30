"""Pin: ``_resolve_phase_models`` routes every slot through
:func:`config.phase_model` so the run-header banner reflects
``_config/config.local.json`` overrides.

Previously only ``plan`` and ``repair_escalation`` read AppConfig;
``implement`` / ``repair_changes`` fell back to the raw CLI
``--model`` default and ``review`` used the import-time
``CODEX_MODEL`` constant — both bypass paths silently ignored
local config. This test pins the fix so a future refactor cannot
re-introduce the divergence.
"""
from __future__ import annotations

from unittest.mock import patch

from pipeline.project.profile_dispatch import (
    resolve_phase_models as _resolve_phase_models,
)


def test_every_slot_reads_appconfig_phase_models_when_no_phase_config() -> None:
    # ``_resolve_phase_models`` calls ``config.phase_model(name, default)``
    # for each slot. Patch the resolver to confirm EVERY slot is
    # consulted via the AppConfig-backed accessor, and that the
    # caller's fallback only kicks in when AppConfig has no entry.
    seen: list[tuple[str, str]] = []

    def _stub(name: str, default: str = "") -> str:
        seen.append((name, default))
        # Return distinctive values per phase so the call site
        # actually consumes the AppConfig output.
        return f"model:{name}"

    with patch("pipeline.project.profile_dispatch.config.phase_model", _stub):
        result = _resolve_phase_models(
            phase_config=None,
            fallback_code_model="cli-fallback-model",
        )

    plan, implement, repair_changes, repair_escalation, review = result
    assert plan == "model:plan"
    assert implement == "model:implement"
    assert repair_changes == "model:repair_changes"
    assert repair_escalation == "model:repair_escalation"
    assert review == "model:review_changes"

    # Each phase is consulted exactly once via config.phase_model,
    # in the canonical slot order.
    queried_phases = [name for name, _ in seen]
    assert queried_phases == [
        "plan",
        "implement",
        "repair_changes",
        "repair_escalation",
        "review_changes",
    ]


def test_implement_and_repair_use_cli_fallback_when_appconfig_silent() -> None:
    # When ``config.phase_model`` falls back to its ``default``
    # argument (AppConfig has no entry for the phase), the banner
    # must surface the CLI ``fallback_code_model`` rather than a
    # hardcoded literal. This pins the second-order contract: the
    # caller's CLI override flows through.
    def _passthrough(name: str, default: str = "") -> str:
        return default

    with patch("pipeline.project.profile_dispatch.config.phase_model", _passthrough):
        result = _resolve_phase_models(
            phase_config=None,
            fallback_code_model="cli-fallback-model",
        )

    plan, implement, repair_changes, _esc, review = result
    # plan / repair_escalation keep their own literal defaults.
    assert plan == "claude-opus-4-8[1m]"
    # implement / repair_changes use the CLI fallback the user
    # passed in, NOT a hardcoded Claude default.
    assert implement == "cli-fallback-model"
    assert repair_changes == "cli-fallback-model"
    # review uses CODEX_MODEL as the documented fallback.
    from core.infra import config as _cfg
    assert review == _cfg.CODEX_MODEL


def test_phase_config_override_short_circuits_appconfig() -> None:
    # When the caller supplied an explicit ``PhaseAgentConfig``,
    # ``_resolve_phase_models`` returns its agent models verbatim
    # and never queries ``config.phase_model``. Pin the
    # short-circuit so a future refactor cannot accidentally
    # double-source the values.
    from types import SimpleNamespace

    class _Agent:
        def __init__(self, model: str) -> None:
            self.model = model

    fake_phase_config = SimpleNamespace(
        plan_agent=_Agent("explicit-plan"),
        implement_agent=_Agent("explicit-implement"),
        repair_changes_agent=_Agent("explicit-repair"),
        repair_escalation_agent=_Agent("explicit-esc"),
        review_changes_agent=_Agent("explicit-review"),
    )

    calls: list[str] = []

    def _watcher(name: str, default: str = "") -> str:
        calls.append(name)
        return "should-not-be-used"

    with patch("pipeline.project.profile_dispatch.config.phase_model", _watcher):
        result = _resolve_phase_models(
            phase_config=fake_phase_config,
            fallback_code_model="cli-fallback",
        )

    assert result == (
        "explicit-plan",
        "explicit-implement",
        "explicit-repair",
        "explicit-esc",
        "explicit-review",
    )
    # No AppConfig lookups happen on the override path.
    assert calls == []
