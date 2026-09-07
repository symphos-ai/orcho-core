"""Cross revision inputs survive fresh provider sessions and artifact changes."""
from pathlib import Path

import pytest

from pipeline.cross_project.prompts import cross_plan_review_focus, cross_replan_prompt
from pipeline.cross_project.session_invoke import session_aware_invoke
from tests.unit.pipeline.cross_project.test_cross_orchestrator import (
    _approved_review_json,
    _cp_json,
    _cross_test_appconfig_mock,
    _rejected_review_json,
    _ScriptedProvider,
)
from tests.unit.pipeline.cross_project.test_cross_plan_handoff import _seed_decision
from tests.unit.pipeline.cross_project.test_planning_session_delta import _RecordingAgent


@pytest.mark.parametrize("mode", ["full", "minimal", "minimal_with_format"])
def test_replan_artifact_reaches_fresh_and_warm_invocations(tmp_path: Path, mode: str):
    artifact = tmp_path / "cross_plan.md"
    agent = _RecordingAgent()
    sessions = {}
    for index, continued in enumerate((False, True, False)):
        baseline = f"REQUIREMENT_ONLY_IN_PRIOR_PLAN_{index}"
        artifact.write_text(baseline)
        turn = cross_replan_prompt(
            "Task", "Fix a different issue", {"api": tmp_path}, tmp_path,
            professional_prompt_mode=mode,
        )
        session_aware_invoke(
            agent, turn=turn, prompt_sessions=sessions if continued else {},
            run_id="run", phase="cross_plan", cwd=str(tmp_path),
            continue_session=continued,
        )
        wire = agent.calls[-1]["prompt"]
        assert baseline in wire
        if index:
            assert f"REQUIREMENT_ONLY_IN_PRIOR_PLAN_{index - 1}" not in wire


@pytest.mark.parametrize("mode", ["full", "minimal", "minimal_with_format"])
def test_review_receives_acceptance_beyond_task_excerpt(tmp_path: Path, mode: str):
    task = "Task context " * 60 + "LATE_ACCEPTANCE_MUST_BE_VERIFIED"
    turn = cross_plan_review_focus(
        task, ["api"], plan_artifact="CANDIDATE_PLAN",
        professional_prompt_mode=mode,
    )
    agent = _RecordingAgent()
    session_aware_invoke(
        agent, turn=turn, prompt_sessions={}, run_id="run", phase="cross_validate_plan",
        cwd=str(tmp_path), continue_session=False,
    )
    assert task in agent.calls[0]["prompt"]
    assert "CANDIDATE_PLAN" in agent.calls[0]["prompt"]


def test_real_loop_and_cold_handoff_receive_latest_valid_baseline(tmp_path, monkeypatch):
    from pipeline.cross_project import orchestrator as cross

    _cross_test_appconfig_mock(monkeypatch, cross)
    projects = {"api": tmp_path / "api", "web": tmp_path / "web"}
    for project in projects.values():
        project.mkdir()
    run_dir = tmp_path / "run"
    task = "Original task " * 40 + "REVIEW_ACCEPTANCE_AT_END"
    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web", interface_contract="BASELINE_ONE_ONLY"),
            _cp_json("api", "web", interface_contract="BASELINE_TWO_ONLY"),
        ],
        review_outputs=[_rejected_review_json(), _rejected_review_json()],
    )
    session = cross.run_cross_pipeline(
        task=task, projects=projects, output_dir=run_dir,
        provider=provider, cross_mode="plan",
    )
    assert "BASELINE_ONE_ONLY" in provider.plan.calls[1][0]
    assert all(task in call[0] for call in provider.review.calls)
    _seed_decision(
        run_dir, handoff_id=session["phase_handoff"]["id"],
        action="retry_feedback", feedback="Repair a separate issue",
    )
    # Fresh provider and invocation store: no conversational memory of the plan.
    retry = _ScriptedProvider(plan_outputs=["{invalid"], review_outputs=[])
    paused = cross.run_cross_pipeline(
        task=task, projects=projects, output_dir=run_dir, provider=retry,
        cross_mode="plan", resume_from=run_dir.name, resumed_meta=session,
    )
    assert "BASELINE_TWO_ONLY" in retry.plan.calls[0][0]
    assert "BASELINE_ONE_ONLY" not in retry.plan.calls[0][0]
    assert not retry.review.calls  # malformed output never reaches model review
    _seed_decision(
        run_dir, handoff_id=paused["phase_handoff"]["id"],
        action="retry_feedback", feedback="Fix JSON syntax",
    )
    final = _ScriptedProvider(
        plan_outputs=[_cp_json("api", "web")], review_outputs=[_approved_review_json()],
    )
    cross.run_cross_pipeline(
        task=task, projects=projects, output_dir=run_dir, provider=final,
        cross_mode="plan", resume_from=run_dir.name, resumed_meta=paused,
    )
    assert "BASELINE_TWO_ONLY" in final.plan.calls[0][0]
    assert task in final.review.calls[0][0]


def test_first_invalid_plan_has_no_invented_baseline(tmp_path):
    turn = cross_replan_prompt("Task", "Invalid JSON", {"api": tmp_path}, tmp_path)
    assert "Previous cross-plan revision baseline" not in turn.text
    assert "Invalid JSON" in turn.text
