from __future__ import annotations

from types import SimpleNamespace

from agents.entities import SubTask
from pipeline.plan_parser import ParsedPlan
from pipeline.project.handoff_advice_contract import (
    build_advice_contract_snapshot,
    render_accepted_plan_contract,
)
from pipeline.runtime.handoff import PhaseHandoffRequested
from pipeline.runtime.roles import PhaseHandoffType


def _signal(*, artifacts: dict | None = None) -> PhaseHandoffRequested:
    return PhaseHandoffRequested(
        handoff_id="review_changes:review:2",
        phase="review_changes",
        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
        trigger="rejected",
        verdict="REJECTED",
        approved=False,
        round_extras_key="review",
        round=2,
        loop_max_rounds=3,
        available_actions=("continue", "retry_feedback", "halt"),
        artifacts=artifacts or {},
    )


def test_snapshot_is_lossless_and_has_stable_invariant_ids() -> None:
    task = "Fix café\n\nKeep the newline.\n"
    plan = ParsedPlan(
        subtasks=(SubTask(id="extract", goal="Extract", done_criteria=("Tests pass", "Facade shrinks"), owned_files=("a.py",), allowed_modifications=("a.lock",)),),
        source="json",
        goal="Keep advice contract-bound",
        acceptance_criteria=("Digest exact task", "Render plan"),
        owned_files=("pipeline/project/a.py",),
        allowed_modifications=("docs/schema.json",),
    )
    snapshot = build_advice_contract_snapshot(
        SimpleNamespace(state=SimpleNamespace(task=task, parsed_plan=plan)),
        _signal(artifacts={"gate_set": "unit", "gate_command": "pytest -q", "failure_kind": "test_failure", "correction_context": "full context\nwithout trimming"}),
    )

    assert snapshot.task_sha256 == "2cbacec85d0bbc581aa6ff5313644633c65d68059c0c39f274c323308dbf727c"
    assert [item.id for item in snapshot.acceptance_criteria] == ["acceptance:1", "acceptance:2"]
    assert [item.id for item in snapshot.subtasks[0].done_criteria] == ["task:extract:done:1", "task:extract:done:2"]
    assert snapshot.owned_files == ("pipeline/project/a.py",)
    assert snapshot.subtasks[0].owned_files == ("a.py",)
    assert snapshot.aggregate_owned_files == ("pipeline/project/a.py", "a.py")
    assert snapshot.aggregate_allowed_modifications == ("docs/schema.json", "a.lock")
    assert snapshot.gate_command == "pytest -q"
    assert snapshot.correction_context == "full context\nwithout trimming"
    rendered = render_accepted_plan_contract(snapshot)
    assert "[acceptance:1] Digest exact task" in rendered
    assert "[task:extract:done:2] Facade shrinks" in rendered
    assert "gate_command: pytest -q" in rendered
    assert "full context\nwithout trimming" in rendered


def test_snapshot_marks_absent_parsed_plan_without_inference() -> None:
    snapshot = build_advice_contract_snapshot(
        SimpleNamespace(state=SimpleNamespace(task="raw task", parsed_plan=None)), _signal()
    )
    assert snapshot.parsed_plan_available is False
    assert snapshot.acceptance_criteria == ()
    assert "parsed_plan_available: false" in render_accepted_plan_contract(snapshot)
