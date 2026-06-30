"""
Plugin discovery via importlib.metadata.

orcho-core declares its built-in agent runtimes and phase handlers via
entry_points groups so any third-party package can extend without core
edits. These tests pin that contract:

 * single ``orcho.agent_runtimes`` group (replaces the legacy
 3-way ``orcho.providers.{architect,developer,reviewer}`` groups —
 same agent class served all three roles in practice).
 * AgentRegistry.default() and register_builtin_phases() actually go
 through entry_points (not a hidden hardcoded mapping).
 * A third-party entry can override a built-in by registering under the
 same name (the supported override mechanism).
"""

from __future__ import annotations

from importlib.metadata import entry_points

import pytest

from agents.registry import AgentRegistry
from pipeline.phases.builtin import (
    default_registry,
    register_builtin_phases,
)
from pipeline.runtime import PhaseRegistry

# ── pyproject.toml entry_points are actually registered ──────────────────────

class TestEntryPointGroups:
    def test_agent_runtimes_group_has_builtin_runtimes(self) -> None:
        """single ``orcho.agent_runtimes`` group ships
 claude/codex/gemini. Replaces the legacy 3-way
 ``orcho.providers.{architect,developer,reviewer}`` groups."""
        names = {ep.name for ep in entry_points(group="orcho.agent_runtimes")}
        assert {"claude", "codex", "gemini"} <= names

    def test_legacy_provider_groups_deleted(self) -> None:
        """clean break: legacy 3-way provider groups must be
 empty (per ``feedback_no_backcompat_ceremony`` — no aliasing).
 """
        for legacy_group in (
            "orcho.providers.architect",
            "orcho.providers.developer",
            "orcho.providers.reviewer",
        ):
            names = {ep.name for ep in entry_points(group=legacy_group)}
            assert names == set(), (
                f"legacy group {legacy_group!r} should be empty, "
                f"got {names}"
            )

    def test_phases_group_has_all_seven_builtins(self) -> None:
        """redesign: the four DAG handlers (decompose /
 decompose_qa / execute_dag / integrate_qa) moved into
 ``DagExecutionMode`` and are no longer in the public
 ``orcho.phases`` surface — see ADR 0001 / plan."""
        names = {ep.name for ep in entry_points(group="orcho.phases")}
        expected = {
            "plan", "validate_plan", "implement", "review_changes", "repair_changes", "final_acceptance",
            "compliance_check",
        }
        assert expected <= names
        # DAG-internal handlers explicitly absent now.
        assert {"decompose", "decompose_qa", "execute_dag", "integrate_qa"} \
            .isdisjoint(names)

    def test_runtime_entry_targets_are_loadable(self) -> None:
        """Every advertised runtime class actually imports — guards
 against typos in pyproject.toml that would only surface at
 runtime. single group ``orcho.agent_runtimes``."""
        for ep in entry_points(group="orcho.agent_runtimes"):
            cls = ep.load()  # raises if module/class missing
            assert isinstance(cls, type), (
                f"orcho.agent_runtimes/{ep.name} is not a class"
            )

    def test_phase_entry_targets_are_callable(self) -> None:
        for ep in entry_points(group="orcho.phases"):
            handler = ep.load()
            assert callable(handler), f"orcho.phases/{ep.name} is not callable"


# ── AgentRegistry.default() actually goes through entry_points ───────────────

class TestAgentRegistryDiscovery:
    def test_default_picks_up_all_three_built_in_providers(self) -> None:
        r = AgentRegistry.default()
        # Internal dicts are an implementation detail but the simplest
        # way to verify the full registration set without spinning real
        # CLI binaries.
        assert "claude" in r._runtimes
        assert "codex" in r._runtimes
        # Gemini gating: in CI / dev machines without the binary, gemini
        # is skipped; when present, it lands in all three slots.
        # We tolerate either by checking only the always-present pair above.

    def test_factories_accept_model_and_optional_effort(self) -> None:
        r = AgentRegistry.default()
        # Factories must accept (model) and (model, effort) — this is
        # how PhaseAgentConfig.default builds agents downstream.
        from unittest.mock import patch
        with patch("agents.runtimes.claude.config.get_claude_bin", return_value="/bin/echo"):
            agent = r.architect("test-model", runtime="claude")
            assert agent.model == "test-model"
            agent = r.architect("test-model", runtime="claude", effort="low")
            assert agent.model == "test-model"

    def test_unknown_runtime_raises_keyerror(self) -> None:
        r = AgentRegistry.default()
        with pytest.raises(KeyError, match="No agent runtime registered"):
            r.architect("anything", runtime="nonexistent")


# ── register_builtin_phases local builtins + entry_points overlay ────────────

class TestPhaseRegistryDiscovery:
    def test_default_registry_has_all_builtin_handlers(self) -> None:
        """redesign: DAG-internal handlers are no longer
 registered as phases — they live as private methods on
 ``DagExecutionMode`` and are discovered via
 ``ExecutionModeRegistry`` instead."""
        r = default_registry()
        for name in (
            "plan", "validate_plan", "implement", "review_changes", "repair_changes", "final_acceptance",
            "compliance_check",
        ):
            assert r.has(name), f"missing phase handler {name!r}"
        for absent in ("decompose", "decompose_qa", "execute_dag", "integrate_qa"):
            assert not r.has(absent), \
                f"DAG-internal {absent!r} must not be in PhaseRegistry"

    def test_local_builtins_survive_stale_entry_point_metadata(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import importlib.metadata

        monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: ())

        r = default_registry()

        assert r.has("correction_triage")

    def test_entry_point_overlay_can_override_local_builtin(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import importlib.metadata
        from types import SimpleNamespace

        def custom_plan(state):
            return state

        monkeypatch.setattr(
            importlib.metadata,
            "entry_points",
            lambda group: (
                SimpleNamespace(name="plan", load=lambda: custom_plan),
            ),
        )

        r = default_registry()

        assert r.get("plan") is custom_plan

    def test_register_builtin_phases_returns_same_registry(self) -> None:
        r = PhaseRegistry()
        out = register_builtin_phases(r)
        assert out is r

    def test_default_registry_exposes_builtin_phase_keys(self) -> None:
        """``default_registry()`` exposes exactly the built-in
        ``orcho.phases`` keys — catches drift in the entry-point set."""
        r = default_registry()
        assert set(r.names()) == {
            "plan", "validate_plan", "implement", "review_changes",
            "repair_changes", "final_acceptance", "compliance_check",
            "correction_triage",
        }


# ── Override mechanism (third-party plugin shadows a built-in) ───────────────

class TestThirdPartyOverride:
    """Re-registration is the documented mechanism for plugin authors
 to swap a built-in handler — e.g. replacing the ``compliance_check``
 no-op stub with one that signs the run."""

    def test_re_registering_a_phase_overwrites_the_handler(self) -> None:
        r = default_registry()

        custom_called: list[str] = []
        def custom_compliance(state):
            custom_called.append("ran")
            return state

        r.register("compliance_check", custom_compliance)
        # Now retrieving by name returns the overlay, not the stub.
        assert r.get("compliance_check") is custom_compliance

    def test_re_registering_a_runtime_overwrites_the_factory(self) -> None:
        r = AgentRegistry.default()

        sentinel = object()
        r.register("claude", lambda model, effort=None, _s=sentinel: _s)
        # Subsequent.developer("claude") returns the overlay.
        agent = r.developer("any-model", runtime="claude")
        assert agent is sentinel
