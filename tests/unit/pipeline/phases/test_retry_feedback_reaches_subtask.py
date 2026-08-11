"""ADR 0176 — the operator's retry_feedback reaches the replayed subtask prompt.

Writer-to-reader contract. Two halves of one journey, joined by the durable
carrier ``state.extras['implement_retry']['feedback']``:

* the WRITER is the real resume arm ``apply_phase_handoff_resume``, driven from
  a persisted ``retry_feedback`` decision on disk;
* the READER is the real ``_phase_implement`` DAG path, whose subtask prompt is
  captured off a fake runtime.

The original defect is precisely the seam between them: the retry payload
carried the operator's words, the redispatch banner printed them, and the
prompt the agent actually received never contained them — so a run stuck on a
wrong assumption could not be unblocked by feedback at all. A test that only
exercised one side would have stayed green through that bug, which is why this
module drives both.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from agents.entities import SubTask
from agents.registry import AgentRegistry, PhaseAgentConfig
from agents.runtimes._strategy import _mock_subtask_attestation
from pipeline.plan_parser import ParsedPlan
from pipeline.plugins import PluginConfig
from pipeline.runtime import PhaseStep, PipelineState, Profile

_FEEDBACK = "Use the cached client; do NOT open a second connection pool."
_HANDOFF_ID = "implement:implement_handoff:1"


class _FakeDeveloper:
    """IAgentRuntime fake that records the wire prompt of every invoke."""

    def __init__(self) -> None:
        self.model = "claude-opus-4-7"
        self.session_id = "sess-dev-1"
        self.runtime = "claude"
        self.prompts: list[str] = []

    def invoke(self, prompt: str, cwd: str = "", **kwargs: Any) -> str:
        self.prompts.append(prompt)
        return _mock_subtask_attestation(prompt)


def _plan() -> ParsedPlan:
    return ParsedPlan(
        short_summary="s", planning_context="c",
        subtasks=(
            SubTask(id="t1", goal="first", owned_files=("a.py",)),
            SubTask(id="t2", goal="second", owned_files=("b.py",)),
        ),
        source="json",
    )


def _seed_decision(run_dir, *, feedback: str | None) -> None:
    from sdk.phase_handoff import safe_handoff_id

    decisions = run_dir / "phase_handoff_decisions"
    decisions.mkdir(parents=True, exist_ok=True)
    (decisions / f"{safe_handoff_id(_HANDOFF_ID)}.json").write_text(
        json.dumps({
            "run_id": run_dir.name,
            "handoff_id": _HANDOFF_ID,
            "phase": "implement",
            "action": "retry_feedback",
            "feedback": feedback,
            "note": None,
            "decided_at": "2026-08-06T12:00:00+00:00",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _state(agent: _FakeDeveloper) -> PipelineState:
    registry = AgentRegistry()
    registry.register("claude", lambda model, _effort=None: agent)
    state = PipelineState(
        task="t", project_dir="/p", plugin=PluginConfig(),
        parsed_plan=None, registry=registry,
        phase_config=PhaseAgentConfig(
            plan_agent=agent, validate_plan_agent=agent, implement_agent=agent,
            review_changes_agent=agent, repair_changes_agent=agent,
            repair_escalation_agent=agent, final_acceptance_agent=agent,
        ),
        extras={"run_id": "run-retry", "implementation_execution": "subtask_dag"},
    )
    state.lifecycle_ctx = SimpleNamespace(
        active_step=SimpleNamespace(
            execution_policy=SimpleNamespace(
                session_split=None, session_continuity="same_zone_continue",
            ),
            prompt=None,
        ),
    )
    return state


def _run(run_dir, state: PipelineState):
    return SimpleNamespace(
        output_dir=run_dir,
        session={
            "status": "awaiting_phase_handoff",
            "phases": {},
            "phase_handoff": {
                "id": _HANDOFF_ID,
                "phase": "implement",
                "round": 1,
                "loop_max_rounds": 1,
                "artifacts": {
                    "findings": ["t2 incomplete"],
                    "incomplete_subtasks": ["t2"],
                    "attestation_incomplete": {"t2": "criteria not closed"},
                },
                "last_output": "build log",
            },
        },
        _ckpt=None,
        _metrics=None,
        state=state,
    )


def _profile() -> Profile:
    return Profile(
        name="feature",
        steps=(PhaseStep(phase="implement"),),
    )


def _resume_with(run_dir, state: PipelineState, *, feedback: str | None) -> None:
    """Drive the real writer: persisted decision → seeded retry payload."""
    from pipeline.plan_artifacts import write_parsed_plan_artifact
    from pipeline.project.handoff import apply_phase_handoff_resume

    write_parsed_plan_artifact(run_dir, _plan(), attempt=1)
    _seed_decision(run_dir, feedback=feedback)
    apply_phase_handoff_resume(_run(run_dir, state), _profile(), None)


def test_operator_feedback_reaches_the_replayed_subtask_prompt(tmp_path) -> None:
    """The whole point: the agent redoing t2 is told what the operator said."""
    run_dir = tmp_path / "20260806_120000_impl"
    run_dir.mkdir()
    agent = _FakeDeveloper()
    state = _state(agent)

    _resume_with(run_dir, state, feedback=_FEEDBACK)
    # Writer half: the durable carrier holds the operator's words verbatim.
    assert state.extras["implement_retry"]["feedback"] == _FEEDBACK
    assert state.extras["implement_retry"]["incomplete_ids"] == ["t2"]

    from pipeline.phases.builtin import _phase_implement
    _phase_implement(state)

    # Reader half: only the retried subtask is re-invoked, and its prompt
    # carries the instruction — the assertion that fails on the original bug.
    assert len(agent.prompts) == 1
    prompt = agent.prompts[0]
    assert "## Current Executable Subtask `t2`" in prompt
    assert _FEEDBACK in prompt
    assert "This subtask is being re-run" in prompt
    assert prompt.index("This subtask is being re-run") < prompt.index(
        "## Current Executable Subtask",
    )


def test_ordinary_pass_carries_no_operator_instruction(tmp_path) -> None:
    """Non-regression: a run nobody asked to redo is prompted as before.

    Guards the other direction — the new argument must not leak framing into
    an ordinary first pass, where there is no operator instruction to honour.
    """
    agent = _FakeDeveloper()
    state = _state(agent)
    state.parsed_plan = _plan()

    from pipeline.phases.builtin import _phase_implement
    _phase_implement(state)

    assert len(agent.prompts) == 2
    for prompt in agent.prompts:
        assert "This subtask is being re-run" not in prompt
        assert _FEEDBACK not in prompt
