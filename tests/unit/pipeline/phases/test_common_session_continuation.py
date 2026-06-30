"""P0 — session_split continuation across phases (Session-Aware Subtask DAG v1).

These tests pin the contract that ``session_split`` — not a phase default
or a subtask index — owns physical provider-session continuation:

* ``_should_continue_prompt_session`` returns ``True`` only when the
  resolved split yields a reusable key *and* a committed
  ``PromptSessionState`` already carries a provider ``session_id``.
* ``_session_aware_invoke`` seeds the (distinct) phase agent instance
  from that stored session id so ``continue_session=True`` physically
  resumes the same provider session — the failure the
  ``builtin.py`` "fresh-reset" guard exists to prevent.

The historical bug: ``_phase_implement`` invoked without
``continue_session``, so ``session_split=common`` reset a stored state to
fresh and never resumed plan→implement. Each phase also binds a distinct
agent instance (``PhaseAgentConfig`` slots), so passing the flag alone is
insufficient without seeding.

Scoped to the helpers: a plain ``PipelineState`` plus recording agents
pin the contract without spinning up the FSM.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.io.retry import AgentCallError
from core.observability import prompt_trace
from pipeline.phases.builtin import (
    _compute_session_key,
    _ensure_lifecycle_ctx,
    _phase_implement,
    _session_aware_invoke,
    _should_continue_prompt_session,
)
from pipeline.plugins import PluginConfig
from pipeline.prompts.session import PromptSessionSplit, PromptSessionState
from pipeline.prompts.turn import PromptTurn, PromptTurnEditor
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptLayer,
    PromptPart,
    PromptStability,
)
from pipeline.runtime import PipelineState
from pipeline.runtime.roles import SessionInvocationRole


class _RecordingAgent:
    """Phase-agent stub. Same class for every slot so two instances
    share a runtime id — modelling ``plan_agent`` and ``implement_agent``
    both resolving to the same runtime class with the same model."""

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        session_id: str | None = "sess-plan-1",
        fail_missing_session_once: bool = False,
        success_session_id: str | None = None,
    ) -> None:
        self.model = model
        self.session_id = session_id
        self.calls: list[dict[str, Any]] = []
        self.fail_missing_session_once = fail_missing_session_once
        self.success_session_id = success_session_id

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        continue_session: bool = False,
        attachments: tuple = (),
        mutates_artifacts: bool = False,
    ) -> str:
        self.calls.append({
            "prompt": prompt,
            "continue_session": continue_session,
        })
        if self.fail_missing_session_once and len(self.calls) == 1:
            raise AgentCallError(
                "Agent call failed: exit=1",
                exit_code=1,
                stderr=(
                    "No conversation found with session ID: "
                    "sess-plan-1"
                ),
            )
        if self.success_session_id:
            self.session_id = self.success_session_id
        return "ok"


def _role() -> PromptPart:
    return PromptPart(
        kind="role", name="implementation_engineer", source="core",
        body="role:implementation_engineer", layer=PromptLayer.ROLE,
    )


def _turn_input(body: str, name: str = "implement_task") -> PromptPart:
    return PromptPart(
        kind="turn_input", name=name, source="code-owned", body=body,
        layer=PromptLayer.TURN, stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE, volatile_reason="turn input",
        id=name,
    )


def _turn(body: str = "do the work") -> PromptTurn:
    editor = PromptTurnEditor()
    editor.append(_role())
    editor.append(_turn_input(body))
    return editor.build()


def _state(*, role: str | None = None) -> PipelineState:
    state = PipelineState(
        task="implement the plan",
        project_dir="/proj",
        plugin=PluginConfig(),
        extras={"run_id": "run-p0-1"},
    )
    # ADR 0113: the implement-shaped seam resolves continuity from the active
    # step's declaration; these tests vary only the orthogonal session_split,
    # so the step always carries implement's real continuity default
    # (same_zone_continue) and the resolver never has to guess.
    state.lifecycle_ctx = SimpleNamespace(
        active_step=SimpleNamespace(
            prompt=SimpleNamespace(role=role) if role is not None else None,
            execution_policy=SimpleNamespace(
                session_split=None, session_continuity="same_zone_continue"
            ),
        ),
    )
    return state


def _seed(
    state: PipelineState,
    agent: _RecordingAgent,
    *,
    phase: str,
    split: PromptSessionSplit,
) -> None:
    """Run one full-render invoke so a committed PromptSessionState with a
    provider session id lands in ``state.prompt_sessions``."""
    _session_aware_invoke(
        agent, state, phase=phase, turn=_turn("seed turn"),
        cwd="/proj", split=split, continue_session=False,
    )


@pytest.fixture(autouse=True)
def _drain_render_envelope():
    prompt_trace.take_last_upper()
    yield
    prompt_trace.take_last_upper()


# ── _should_continue_prompt_session policy matrix ──────────────────────


class TestShouldContinuePolicy:
    def test_common_plan_then_implement_resumes(self) -> None:
        state = _state()
        plan_agent = _RecordingAgent(session_id="sess-plan-1")
        _seed(state, plan_agent, phase="plan", split=PromptSessionSplit.COMMON)

        # A different instance (same class + model) — the implement slot.
        implement_agent = _RecordingAgent(session_id=None)
        assert _should_continue_prompt_session(
            state, implement_agent, phase="implement",
            role=SessionInvocationRole.IMPLEMENT,
            split=PromptSessionSplit.COMMON,
        ) is True

    def test_per_phase_plan_then_implement_does_not_resume(self) -> None:
        state = _state()
        plan_agent = _RecordingAgent(session_id="sess-plan-1")
        _seed(
            state, plan_agent, phase="plan",
            split=PromptSessionSplit.PER_PHASE,
        )

        implement_agent = _RecordingAgent(session_id=None)
        # per_phase:plan and per_phase:implement are distinct keys.
        assert _should_continue_prompt_session(
            state, implement_agent, phase="implement",
            role=SessionInvocationRole.IMPLEMENT,
            split=PromptSessionSplit.PER_PHASE,
        ) is False

    def test_per_role_different_roles_does_not_resume(self) -> None:
        state = _state(role="plan_reviewer")
        agent = _RecordingAgent(session_id="sess-1")
        _seed(state, agent, phase="validate_plan", split=PromptSessionSplit.PER_ROLE)

        # Switch the active role; the per_role key changes with it.
        state.lifecycle_ctx.active_step.prompt.role = "implementation_engineer"
        assert _should_continue_prompt_session(
            state, agent, phase="implement",
            role=SessionInvocationRole.IMPLEMENT,
            split=PromptSessionSplit.PER_ROLE,
        ) is False

    def test_per_role_same_role_implement_then_repair_resumes(self) -> None:
        state = _state(role="implementation_engineer")
        agent = _RecordingAgent(session_id="sess-1")
        _seed(state, agent, phase="implement", split=PromptSessionSplit.PER_ROLE)

        # Same role on a later phase → same per_role key → resume.
        assert _should_continue_prompt_session(
            state, agent, phase="repair_changes",
            role=SessionInvocationRole.REPAIR,
            split=PromptSessionSplit.PER_ROLE,
        ) is True

    def test_stateless_never_resumes(self) -> None:
        state = _state()
        agent = _RecordingAgent(session_id="sess-1")
        # Even with prior state seeded under another split, stateless is False.
        _seed(state, agent, phase="implement", split=PromptSessionSplit.COMMON)
        assert _should_continue_prompt_session(
            state, agent, phase="implement",
            role=SessionInvocationRole.IMPLEMENT,
            split=PromptSessionSplit.STATELESS,
        ) is False

    def test_model_change_does_not_resume(self) -> None:
        state = _state()
        plan_agent = _RecordingAgent(session_id="sess-plan-1", model="m1")
        _seed(state, plan_agent, phase="plan", split=PromptSessionSplit.COMMON)

        # A model swap invalidates the COMMON key (model_key participates).
        implement_agent = _RecordingAgent(session_id=None, model="m2")
        assert _should_continue_prompt_session(
            state, implement_agent, phase="implement",
            role=SessionInvocationRole.IMPLEMENT,
            split=PromptSessionSplit.COMMON,
        ) is False

    def test_no_provider_session_id_does_not_resume(self) -> None:
        state = _state()
        # Seed agent never captured a provider session id.
        plan_agent = _RecordingAgent(session_id=None)
        _seed(state, plan_agent, phase="plan", split=PromptSessionSplit.COMMON)

        implement_agent = _RecordingAgent(session_id=None)
        assert _should_continue_prompt_session(
            state, implement_agent, phase="implement",
            role=SessionInvocationRole.IMPLEMENT,
            split=PromptSessionSplit.COMMON,
        ) is False


# ── _session_aware_invoke seeds the agent for physical resume ──────────


class TestCrossPhaseSeeding:
    def test_common_implement_seeds_agent_and_renders_delta(self) -> None:
        state = _state()
        plan_agent = _RecordingAgent(session_id="sess-plan-1")
        _seed(state, plan_agent, phase="plan", split=PromptSessionSplit.COMMON)

        implement_agent = _RecordingAgent(session_id=None)
        cont = _should_continue_prompt_session(
            state, implement_agent, phase="implement",
            role=SessionInvocationRole.IMPLEMENT,
            split=PromptSessionSplit.COMMON,
        )
        assert cont is True

        _session_aware_invoke(
            implement_agent, state, phase="implement", turn=_turn(),
            cwd="/proj", split=PromptSessionSplit.COMMON, continue_session=cont,
        )

        # The implement agent was seeded with the plan agent's provider
        # session id and armed to resume on its CLI call.
        assert implement_agent.session_id == "sess-plan-1"
        assert getattr(implement_agent, "_followup_resume_pending", False) is True
        # The wire call actually resumed.
        assert implement_agent.calls[0]["continue_session"] is True
        # And the render path took the delta branch (provider session
        # assumed present).
        meta = state.phase_log["implement"]["prompt_render"]
        assert meta["render_mode"] == "delta"
        assert meta["session_key"]["scope"] == "common"
        assert meta["continue_session"] is True

    def test_stale_session_id_is_overwritten_to_stored_id(self) -> None:
        # The implement agent already points at a *different* session
        # (e.g. a prior run / leaked id). continue_session=True must
        # reconcile it to the stored id, otherwise the delta wire omits
        # parts from one session while the runtime resumes another.
        state = _state()
        plan_agent = _RecordingAgent(session_id="sess-plan-1")
        _seed(state, plan_agent, phase="plan", split=PromptSessionSplit.COMMON)

        implement_agent = _RecordingAgent(session_id="stale-other-session")
        cont = _should_continue_prompt_session(
            state, implement_agent, phase="implement",
            role=SessionInvocationRole.IMPLEMENT,
            split=PromptSessionSplit.COMMON,
        )
        assert cont is True

        _session_aware_invoke(
            implement_agent, state, phase="implement", turn=_turn(),
            cwd="/proj", split=PromptSessionSplit.COMMON, continue_session=cont,
        )
        # Reconciled to the stored provider session, not left stale.
        assert implement_agent.session_id == "sess-plan-1"
        assert getattr(implement_agent, "_followup_resume_pending", False) is True
        assert implement_agent.calls[0]["continue_session"] is True

    def test_matching_session_id_is_not_reseeded(self) -> None:
        # Same-instance / already-aligned resume (e.g. within-phase loop
        # or CHAIN): the id already matches, so no reseed / no resume-arm
        # churn.
        state = _state()
        agent = _RecordingAgent(session_id="sess-1")
        _seed(state, agent, phase="implement", split=PromptSessionSplit.COMMON)

        # Stored id now equals the agent's id.
        cont = _should_continue_prompt_session(
            state, agent, phase="implement",
            role=SessionInvocationRole.IMPLEMENT,
            split=PromptSessionSplit.COMMON,
        )
        assert cont is True
        _session_aware_invoke(
            agent, state, phase="implement", turn=_turn(),
            cwd="/proj", split=PromptSessionSplit.COMMON, continue_session=cont,
        )
        assert agent.session_id == "sess-1"
        assert getattr(agent, "_followup_resume_pending", False) is False

    def test_continue_false_keeps_full_render_and_no_seed(self) -> None:
        state = _state()
        plan_agent = _RecordingAgent(session_id="sess-plan-1")
        _seed(state, plan_agent, phase="plan", split=PromptSessionSplit.COMMON)

        # Caller declines to continue → the stored state is reset to fresh,
        # render stays full, and the agent is not seeded.
        implement_agent = _RecordingAgent(session_id=None)
        _session_aware_invoke(
            implement_agent, state, phase="implement", turn=_turn(),
            cwd="/proj", split=PromptSessionSplit.COMMON, continue_session=False,
        )
        assert implement_agent.session_id is None
        meta = state.phase_log["implement"]["prompt_render"]
        assert meta["render_mode"] == "full"
        assert meta["continue_session"] is False

    def test_missing_provider_session_falls_back_to_full_fresh_prompt(self) -> None:
        state = _state()
        plan_agent = _RecordingAgent(session_id="sess-plan-1")
        _seed(state, plan_agent, phase="plan", split=PromptSessionSplit.COMMON)

        implement_agent = _RecordingAgent(
            session_id=None,
            fail_missing_session_once=True,
            success_session_id="sess-fresh-2",
        )
        cont = _should_continue_prompt_session(
            state, implement_agent, phase="implement",
            role=SessionInvocationRole.IMPLEMENT,
            split=PromptSessionSplit.COMMON,
        )
        assert cont is True

        raw = _session_aware_invoke(
            implement_agent, state, phase="implement", turn=_turn(),
            cwd="/proj", split=PromptSessionSplit.COMMON, continue_session=cont,
        )

        assert raw == "ok"
        assert [c["continue_session"] for c in implement_agent.calls] == [
            True, False,
        ]
        assert "role:implementation_engineer" in implement_agent.calls[1]["prompt"]
        assert "do the work" in implement_agent.calls[1]["prompt"]
        meta = state.phase_log["implement"]["prompt_render"]
        assert meta["render_mode"] == "full"
        assert meta["continue_session"] is False
        assert implement_agent.session_id == "sess-fresh-2"


# ── real caller: _phase_implement resolves split from the active step ──


class TestPhaseImplementCaller:
    """Prove ``_phase_implement`` resolves ``common`` from the active
    step's execution policy and physically resumes — not just the helper
    in isolation. This closes the gap between the unit probe and the
    lifecycle dispatch the FSM actually drives."""

    @staticmethod
    def _install_implement_agent(
        state: PipelineState, agent: _RecordingAgent,
    ) -> None:
        from agents.registry import PhaseAgentConfig

        state.phase_config = PhaseAgentConfig(
            plan_agent=_RecordingAgent(),
            validate_plan_agent=_RecordingAgent(),
            implement_agent=agent,
            review_changes_agent=_RecordingAgent(),
            repair_changes_agent=_RecordingAgent(),
            repair_escalation_agent=_RecordingAgent(),
            final_acceptance_agent=_RecordingAgent(),
        )

    def test_phase_implement_resumes_common_via_active_step(self) -> None:
        state = _state()
        # Active step carries session_split=common, as a profile would.
        ctx = _ensure_lifecycle_ctx(state)
        ctx.active_step = SimpleNamespace(
            execution_policy=SimpleNamespace(
                session_split="common", session_continuity="same_zone_continue"
            ),
            prompt=None,
        )
        # An earlier phase (same runtime class + model) committed a
        # common session.
        plan_agent = _RecordingAgent(session_id="sess-plan-1")
        key = _compute_session_key(
            state, plan_agent, phase="plan", split=PromptSessionSplit.COMMON,
        )
        state.prompt_sessions[key] = PromptSessionState(
            key=key, session_id="sess-plan-1",
        )

        impl = _RecordingAgent(session_id=None)
        self._install_implement_agent(state, impl)
        _phase_implement(state)

        # _phase_implement resolved common from the step, computed
        # continue_session=True, seeded the implement agent, and resumed.
        assert impl.session_id == "sess-plan-1"
        assert impl.calls[0]["continue_session"] is True
        meta = state.phase_log["implement"]["prompt_render"]
        assert meta["session_key"]["scope"] == "common"
        assert meta["render_mode"] == "delta"
        assert meta["continue_session"] is True

    def test_phase_implement_default_per_phase_does_not_resume(self) -> None:
        # No execution policy → per_phase default → plan's key differs
        # from implement's, so no cross-phase resume. Regression guard
        # that P0 left the default path untouched.
        state = _state()
        plan_agent = _RecordingAgent(session_id="sess-plan-1")
        key = _compute_session_key(
            state, plan_agent, phase="plan",
            split=PromptSessionSplit.PER_PHASE,
        )
        state.prompt_sessions[key] = PromptSessionState(
            key=key, session_id="sess-plan-1",
        )
        impl = _RecordingAgent(session_id=None)
        self._install_implement_agent(state, impl)
        _phase_implement(state)

        assert impl.session_id is None
        assert impl.calls[0]["continue_session"] is False
        meta = state.phase_log["implement"]["prompt_render"]
        assert meta["session_key"]["scope"] == "per_phase:implement"
        assert meta["render_mode"] == "full"
