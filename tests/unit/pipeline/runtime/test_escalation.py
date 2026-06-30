"""helper.

Pin handler-side FIX agent escalation + session_mode resolution. Legacy
``_PipelineRun.run_review_fix_loop`` continues to drive escalation
imperatively (not exercised here); this file verifies that when v2
dispatch is active, the handler self-resolves the same per-round
agent + mode that the legacy path would have produced.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents.protocols import SessionMode
from pipeline.phases.builtin import _resolve_fix_runtime_config
from pipeline.plugins import PluginConfig
from pipeline.runtime import PipelineState
from pipeline.runtime.handoff import HUMAN_DIRECTED_FLAG_KEY


@dataclass
class _FakePhaseConfig:
    repair_changes_agent: Any = None
    repair_escalation_agent: Any = None
    implement_agent: Any = None


def _state_with_extras(**extras) -> PipelineState:
    return PipelineState(
        task="t",
        project_dir="/p",
        plugin=PluginConfig(),
        phase_config=_FakePhaseConfig(),
        extras=dict(extras),
    )


# ── _resolve_fix_runtime_config ──────────────────────────────────────────────

class TestResolveFixRuntimeConfig:
    def test_round1_no_escalation(self) -> None:
        s = _state_with_extras(
            repair_round=1,
            session_mode_initial="auto",
            implement_model="claude-sonnet",
            repair_model="claude-sonnet",
            repair_escalation_model="claude-opus",
            chain_same_model_only=False,
        )
        cfg = _resolve_fix_runtime_config(s)
        assert cfg["repair_round"] == 1
        assert cfg["repair_model_for_round"] == "claude-sonnet"
        # AUTO + same models → CHAIN
        assert cfg["effective_mode"] is SessionMode.CHAIN

    def test_round2_escalates_repair_model_label(self) -> None:
        """Round > 1 swaps in the escalation *model* for the round."""
        s = _state_with_extras(
            repair_round=2,
            session_mode_initial="auto",
            implement_model="claude-sonnet",
            repair_model="claude-sonnet",
            repair_escalation_model="claude-opus",
            chain_same_model_only=False,
        )
        cfg = _resolve_fix_runtime_config(s)
        assert cfg["repair_round"] == 2
        assert cfg["repair_model_for_round"] == "claude-opus"

    def test_round2_auto_escalation_resolves_hybrid(self) -> None:
        """Automatic round > 1 escalates the model (sonnet → opus). Under the
        shipped ``chain_same_model_only=true`` guard a cross-model edge must
        resolve to HYBRID (re-prime), NOT a cold STATELESS — this is the real
        default config shape (escalation model differs from the base)."""
        s = _state_with_extras(
            repair_round=2,
            session_mode_initial="auto",
            implement_model="claude-sonnet-4-6",
            repair_model="claude-sonnet-4-6",
            repair_escalation_model="claude-opus-4-7",
            chain_same_model_only=True,
        )
        cfg = _resolve_fix_runtime_config(s)
        assert cfg["repair_model_for_round"] == "claude-opus-4-7"
        assert cfg["effective_mode"] is SessionMode.HYBRID

    def test_human_directed_round_chains_on_base_model(self) -> None:
        """A human-directed ``retry_feedback`` round is a continuation, not an
        escalation: it stays on the base repair model and resolves to CHAIN so
        it resumes the implementer session — even at repair_round > 1 with an
        escalation model configured."""
        s = _state_with_extras(
            repair_round=2,
            session_mode_initial="auto",
            implement_model="claude-sonnet-4-6",
            repair_model="claude-sonnet-4-6",
            repair_escalation_model="claude-opus-4-7",
            chain_same_model_only=True,
        )
        s.extras[HUMAN_DIRECTED_FLAG_KEY] = True
        cfg = _resolve_fix_runtime_config(s)
        assert cfg["human_directed"] is True
        # Base model, not the escalation model.
        assert cfg["repair_model_for_round"] == "claude-sonnet-4-6"
        assert cfg["effective_mode"] is SessionMode.CHAIN

    def test_round2_explicit_stateless_passes_through(self) -> None:
        s = _state_with_extras(
            repair_round=2,
            session_mode_initial="stateless",
            implement_model="claude-sonnet",
            repair_model="claude-sonnet",
            chain_same_model_only=False,
        )
        cfg = _resolve_fix_runtime_config(s)
        assert cfg["effective_mode"] is SessionMode.STATELESS

    def test_round2_chain_persists_when_explicit(self) -> None:
        s = _state_with_extras(
            repair_round=2,
            session_mode_initial="chain",
            implement_model="x",
            repair_model="y",
            repair_escalation_model="z",
            chain_same_model_only=False,
        )
        cfg = _resolve_fix_runtime_config(s)
        assert cfg["effective_mode"] is SessionMode.CHAIN

    def test_chain_same_model_only_forces_hybrid_on_mismatch(self) -> None:
        s = _state_with_extras(
            repair_round=1,
            session_mode_initial="auto",
            implement_model="claude-sonnet",
            repair_model="codex-gpt",
            repair_escalation_model="codex-gpt",
            chain_same_model_only=True,
        )
        cfg = _resolve_fix_runtime_config(s)
        assert cfg["effective_mode"] is SessionMode.HYBRID

    def test_auto_suppresses_chain_when_implement_tokens_exceed_budget(self) -> None:
        s = _state_with_extras(
            repair_round=1,
            session_mode_initial="auto",
            implement_model="claude-sonnet",
            repair_model="claude-sonnet",
            repair_escalation_model="claude-sonnet",
            chain_same_model_only=True,
        )
        s.phase_log["implement"] = {
            "_metrics_usage": {"tokens_in": 1_000_001, "tool_calls": 1}
        }
        cfg = _resolve_fix_runtime_config(s)
        assert cfg["effective_mode"] is SessionMode.STATELESS
        assert (
            cfg["session_mode_reason"]
            == "auto_chain_suppressed_context_pressure"
        )
        pressure = cfg["session_mode_context_pressure"]
        assert pressure["tokens_in"] == 1_000_001
        assert pressure["max_tokens_in"] == 1_000_000
        assert pressure["fallback_mode"] == "stateless"

    def test_auto_suppresses_chain_when_implement_tool_calls_exceed_budget(self) -> None:
        s = _state_with_extras(
            repair_round=1,
            session_mode_initial="auto",
            implement_model="claude-sonnet",
            repair_model="claude-sonnet",
            repair_escalation_model="claude-sonnet",
            chain_same_model_only=True,
        )
        s.phase_log["implement"] = {
            "_metrics_usage": {"tokens_in": 42, "tool_calls": 31}
        }
        cfg = _resolve_fix_runtime_config(s)
        assert cfg["effective_mode"] is SessionMode.STATELESS
        pressure = cfg["session_mode_context_pressure"]
        assert pressure["tool_calls"] == 31
        assert pressure["max_tool_calls"] == 30

    def test_auto_context_pressure_uses_hybrid_when_codemap_available(self) -> None:
        s = _state_with_extras(
            repair_round=1,
            session_mode_initial="auto",
            implement_model="claude-sonnet",
            repair_model="claude-sonnet",
            repair_escalation_model="claude-sonnet",
            chain_same_model_only=True,
            codemap="repo outline",
        )
        s.phase_log["implement"] = {
            "_metrics_usage": {"tokens_in": 1_000_001, "tool_calls": 1}
        }
        cfg = _resolve_fix_runtime_config(s)
        assert cfg["effective_mode"] is SessionMode.HYBRID
        assert (
            cfg["session_mode_context_pressure"]["fallback_mode"] == "hybrid"
        )

    def test_explicit_chain_ignores_auto_context_pressure_budget(self) -> None:
        s = _state_with_extras(
            repair_round=1,
            session_mode_initial="chain",
            implement_model="claude-sonnet",
            repair_model="claude-sonnet",
            repair_escalation_model="claude-sonnet",
            chain_same_model_only=True,
        )
        s.phase_log["implement"] = {
            "_metrics_usage": {"tokens_in": 35_285_443, "tool_calls": 159}
        }
        cfg = _resolve_fix_runtime_config(s)
        assert cfg["effective_mode"] is SessionMode.CHAIN
        assert cfg["session_mode_reason"] is None

    def test_default_when_extras_empty(self) -> None:
        """No repair_round / models in extras — defaults shouldn't crash."""
        s = _state_with_extras()
        cfg = _resolve_fix_runtime_config(s)
        assert cfg["repair_round"] == 1
        # Empty models, AUTO → STATELESS or CHAIN; verify no exception.
        assert cfg["effective_mode"] in {
            SessionMode.STATELESS, SessionMode.CHAIN, SessionMode.HYBRID,
        }

    def test_loop_round_fallback_when_no_repair_round(self) -> None:
        """v2 LoopStep with default round_extras_key sets ``loop_round``;
 helper falls back to that when ``repair_round`` not set."""
        s = _state_with_extras(loop_round=2, session_mode_initial="auto")
        cfg = _resolve_fix_runtime_config(s)
        assert cfg["repair_round"] == 2


# ── _phase_repair_changes integration: v2 dispatch flag triggers escalation ────────────

class TestPhaseFixV2Escalation:
    def _build_state(self, *, repair_round: int) -> PipelineState:
        # Stub agents so phase_config swap is observable. M9 routes
        # repair through _session_aware_invoke which calls
        # agent.invoke(...), so the stub now ships a no-op invoke
        # alongside the legacy ``label``/``model`` fields the swap
        # assertions read.
        class _A:
            def __init__(self, label: str) -> None:
                self.label = label
                self.session_id = None
            model = "test"

            def invoke(
                self,
                prompt: str,
                cwd: str,
                *,
                continue_session: bool = False,
                attachments: tuple = (),
                mutates_artifacts: bool = False,
            ) -> str:
                return "fixed"

        pc = _FakePhaseConfig(
            repair_changes_agent=_A("default-fix"),
            repair_escalation_agent=_A("escalate"),
            implement_agent=_A("implement"),
        )
        s = PipelineState(
            task="t", project_dir="/p", plugin=PluginConfig(),
            phase_config=pc,
            extras={
                "repair_round":             repair_round,
                "session_mode_initial":  "chain",
                "implement_model":           "claude-sonnet",
                "repair_model":             "claude-sonnet",
                "repair_escalation_model":    "claude-opus",
                "chain_same_model_only": False,
                "_v2_dispatch_active":   True,
            },
        )
        s.last_critique = "needs fix"
        # ADR 0113: repair_changes declares same_zone_continue continuity. Build
        # the full lifecycle context the FSM would (so session_mode_resolver et
        # al. exist) and seed its active step with the repair continuity
        # declaration, so the repair seam's resolver finds it instead of failing
        # loudly.
        from pipeline.lifecycle import default_lifecycle_context
        from pipeline.runtime import PhaseRegistry
        from pipeline.runtime.profile import ExecutionPolicy
        from pipeline.runtime.steps import PhaseStep

        s.lifecycle_ctx = default_lifecycle_context(
            phase_registry=PhaseRegistry()
        )
        s.lifecycle_ctx.active_step = PhaseStep(
            phase="repair_changes",
            execution="linear",
            execution_policy=ExecutionPolicy(
                mode="linear", session_continuity="same_zone_continue"
            ),
        )
        return s

    def test_round1_chain_mode_swaps_to_build_agent(self) -> None:
        """Round 1 + CHAIN mode → repair_changes_agent set to implement_agent."""
        s = self._build_state(repair_round=1)
        # Mock phases.run_fix to short-circuit (don't need real agent run).
        from pipeline import phases as _phases
        from pipeline.phases.builtin import _phase_repair_changes
        original_run_fix = _phases.run_fix

        class _PhaseResult:
            def __init__(self) -> None:
                self.output = "fixed"
                self.meta = {}

        def _fake_run_fix(*args, **kwargs):
            return _PhaseResult()
        _phases.run_fix = _fake_run_fix
        try:
            _phase_repair_changes(s)
        finally:
            _phases.run_fix = original_run_fix

        # In CHAIN round 1, repair_changes_agent should now point at implement_agent.
        assert s.phase_config.repair_changes_agent.label == "implement"
        # ADR 0113: CHAIN repair records a same-write-zone posture (the policy
        # input); the continue/fresh decision itself is policy-derived, no
        # longer the extras['continue_session'] second source.
        assert s.extras["_repair_same_write_zone"] is True

    def test_round2_escalates_fix_agent(self) -> None:
        """Round > 1 → repair_changes_agent set to repair_escalation_agent."""
        s = self._build_state(repair_round=2)
        from pipeline import phases as _phases
        from pipeline.phases.builtin import _phase_repair_changes
        original_run_fix = _phases.run_fix

        class _PhaseResult:
            def __init__(self) -> None:
                self.output = "fixed"
                self.meta = {}

        def _fake_run_fix(*args, **kwargs):
            return _PhaseResult()
        _phases.run_fix = _fake_run_fix
        try:
            _phase_repair_changes(s)
        finally:
            _phases.run_fix = original_run_fix

        # Round 2 → repair_escalation_agent.
        assert s.phase_config.repair_changes_agent.label == "escalate"

    def test_human_directed_round_swaps_to_implement_agent(self) -> None:
        """A human-directed ``retry_feedback`` round at repair_round > 1 is a
        continuation: it must NOT escalate to the fresh escalation agent, but
        reuse ``implement_agent`` (which carries the session_id) and resume."""
        s = self._build_state(repair_round=2)
        s.extras[HUMAN_DIRECTED_FLAG_KEY] = True
        from pipeline import phases as _phases
        from pipeline.phases.builtin import _phase_repair_changes
        original_run_fix = _phases.run_fix

        class _PhaseResult:
            def __init__(self) -> None:
                self.output = "fixed"
                self.meta = {}

        def _fake_run_fix(*args, **kwargs):
            return _PhaseResult()
        _phases.run_fix = _fake_run_fix
        try:
            _phase_repair_changes(s)
        finally:
            _phases.run_fix = original_run_fix

        # Human-directed continuation → implement_agent, not escalate.
        assert s.phase_config.repair_changes_agent.label == "implement"
        # ADR 0113: same-write-zone posture recorded; continuity is policy-driven.
        assert s.extras["_repair_same_write_zone"] is True

    # b: ``test_legacy_path_skips_handler_escalation``
    # was deleted along with the ``_v2_dispatch_active`` guard in
    # ``_phase_repair_changes``. The legacy ``run_review_fix_loop`` path is gone;
    # the handler now owns escalation unconditionally, so there is no
    # "legacy path skips handler" behavior left to assert.
