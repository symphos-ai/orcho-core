"""Implement handler forwards a bypassed plan-review critique as advisory.

When a finding-gate (``validate_plan``) returned REJECTED but was bypassed
without a replan loop, ``state.last_critique`` is non-empty on entry to
``implement``. The handler forwards it to ``build_prompt`` as advisory reviewer
critique so the developer addresses the findings while implementing — it must
NOT replan and the loop must NOT pause (no phase-handoff opened by implement).

These tests exercise the real ``_phase_implement`` handler with a recording
mock agent (same harness style as the architect prompt-session tests) and
assert on the captured wire prompt. The trigger is generic (non-empty
``last_critique``), so the negative case (APPROVED → ``last_critique == ""``)
adds no advisory part.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pipeline.lifecycle import default_lifecycle_context
from pipeline.phases.builtin.handlers.implement import _phase_implement
from pipeline.plugins import PluginConfig
from pipeline.runtime import PhaseRegistry, PipelineState

# Rendered REJECTED critique text, as ``validate_plan`` would leave on
# ``state.last_critique`` when its verdict is REJECTED-unresolved.
_REJECTED_CRITIQUE = (
    "REJECTED. Findings:\n"
    "- acceptance criteria are too vague to verify\n"
    "- no rollback path described for the migration"
)


class _RecordingAgent:
    """Mock implement agent — records every wire prompt it is invoked with."""

    def __init__(self, *, model: str = "claude-opus-4-7") -> None:
        self.model = model
        self.session_id = "sess-impl-1"
        self.calls: list[dict[str, Any]] = []

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        continue_session: bool = False,
        attachments: tuple = (),
        mutates_artifacts: bool = False,
    ) -> str:
        self.calls.append({"prompt": prompt, "cwd": cwd})
        return "Implemented the change."


def _make_state(*, last_critique: str = "") -> PipelineState:
    state = PipelineState(
        task="Add structured logging",
        project_dir="/proj",
        plugin=PluginConfig(),
        extras={"run_id": "run-impl-1", "loop_round": 1},
        last_critique=last_critique,
    )
    # ADR 0113 (declarative continuity): the implement handler resolves
    # continuity off the active step. These handler-level tests bypass the FSM
    # (which seeds active_step in production), so seed a real lifecycle context
    # with implement's real declared continuity — ``same_zone_continue``. With
    # no prior session / same-write-zone signal it resolves fresh, leaving
    # these advisory-critique assertions (orthogonal to continuity) unchanged.
    ctx = default_lifecycle_context(phase_registry=PhaseRegistry())
    ctx.active_step = SimpleNamespace(
        prompt=None,
        execution_policy=SimpleNamespace(
            session_split=None, session_continuity="same_zone_continue",
        ),
    )
    state.lifecycle_ctx = ctx
    return state


def _install_agent(state: PipelineState, agent: _RecordingAgent) -> None:
    state.phase_config = SimpleNamespace(
        plan_agent=agent,
        validate_plan_agent=agent,
        implement_agent=agent,
        review_agent=agent,
        repair_agent=agent,
        final_acceptance_agent=agent,
    )


def _run_implement(monkeypatch, *, last_critique: str) -> tuple[
    PipelineState, _RecordingAgent,
]:
    """Drive the real implement handler, stubbing the post-work verification
    receipt writer so the test does not spawn an environment-probe subprocess.
    """
    import pipeline.phases.builtin.handlers.implement as impl_mod

    monkeypatch.setattr(
        impl_mod, "_write_implement_verification_receipt", lambda state: None,
    )
    agent = _RecordingAgent()
    state = _make_state(last_critique=last_critique)
    _install_agent(state, agent)
    _phase_implement(state)
    return state, agent


class TestImplementForwardsBypassedCritique:
    def test_rejected_critique_forwarded_as_advisory(self, monkeypatch) -> None:
        state, agent = _run_implement(
            monkeypatch, last_critique=_REJECTED_CRITIQUE,
        )
        assert len(agent.calls) == 1
        prompt = agent.calls[0]["prompt"]
        # Original findings text reaches the implement wire prompt.
        assert "acceptance criteria are too vague to verify" in prompt
        assert "no rollback path described for the migration" in prompt
        # Wrapped in the code-owned advisory framing — advisory, not a
        # replan command or a blocking gate.
        assert "Do not replan" in prompt
        assert "reviewer advisory feedback" in prompt
        assert "Address applicable findings while implementing" in prompt
        # The implement handler never opens a phase-handoff (bypass pause
        # semantics live in the loop runner, not here).
        assert state.phase_handoff_request is None

    def test_approved_adds_no_advisory_part(self, monkeypatch) -> None:
        # APPROVED validate_plan leaves last_critique == "" → no forwarding.
        state, agent = _run_implement(monkeypatch, last_critique="")
        assert len(agent.calls) == 1
        prompt = agent.calls[0]["prompt"]
        assert "Do not replan" not in prompt
        assert "reviewer advisory feedback" not in prompt
        assert state.phase_handoff_request is None

    def test_whitespace_only_critique_adds_no_advisory_part(
        self, monkeypatch,
    ) -> None:
        # The trigger is a non-empty *stripped* critique; whitespace-only
        # is treated as empty and adds no part.
        state, agent = _run_implement(monkeypatch, last_critique="   \n  ")
        prompt = agent.calls[0]["prompt"]
        assert "Do not replan" not in prompt
        assert state.phase_handoff_request is None
