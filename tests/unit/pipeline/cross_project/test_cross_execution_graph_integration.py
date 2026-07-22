"""C1 lifecycle boundary tests: persist structure without changing dispatch."""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.cross_project import session_run
from pipeline.cross_project.execution_graph_store import (
    CrossExecutionGraphStoreError,
    load_cross_execution_graph,
)
from pipeline.cross_project.planning_loop import CrossPlanningResult
from pipeline.cross_project.profile_projection import CrossProjection
from pipeline.cross_project.profile_setup import CrossProfileSetup
from pipeline.runtime import (
    CrossGatePolicy,
    CrossScope,
    CrossStepPolicy,
    PhaseStep,
)

_PLAN = '''{
  "short_summary": "s",
  "interface_contract": "i",
  "implementation_order": ["descriptive only"],
  "subtasks": [
    {"alias": "consumer", "goal": "g", "spec": "c", "depends_on": ["producer"], "files": [], "produces": "", "consumes": ""},
    {"alias": "producer", "goal": "g", "spec": "p", "depends_on": [], "files": [], "produces": "", "consumes": ""}
  ]
}'''


def _profile_setup(project_steps: tuple = (PhaseStep("implement"),)) -> CrossProfileSetup:
    global_step = PhaseStep(
        "plan", cross=CrossStepPolicy(CrossScope.GLOBAL, "cross_plan"),
    )
    return CrossProfileSetup(
        requested_profile=SimpleNamespace(name="test"),
        projection=CrossProjection("test", (global_step,), project_steps),
        child_profile=object() if project_steps else None,
        projected_profile_name="test#project" if project_steps else None,
        contract_gate_policy=CrossGatePolicy(enabled=False),
        cfa_gate_policy=CrossGatePolicy(enabled=False),
        global_handlers=frozenset({"cross_plan"}),
    )


def _context(tmp_path: Path, *, profile_setup: CrossProfileSetup | None = None):
    return SimpleNamespace(
        r=SimpleNamespace(success=lambda *_: None, print=lambda *_: None, warn=lambda *_: None, C=SimpleNamespace(MAGENTA="", BOLD="", CYAN="", GREY="", GREEN=""), banner=lambda *_a, **_k: None),
        aliases=["consumer", "producer"], run_dir=tmp_path,
        profile_setup=profile_setup or _profile_setup(), has_global_plan=True,
        session={"phases": {}}, cross_ckpt={}, cross_phase_usage={}, terminal=False,
        requested_profile=SimpleNamespace(name="test"), cross_mode="plan",
        task_plan=None, global_validate_step=None, global_plan_step=None,
        has_global_validate=False,
        effective_plan_rounds=1, common_cwd=str(tmp_path), plan_agent=None,
        review_agent=None, cross_hypothesis=None, cross_hypothesis_attempts=[],
    )


def _request(tmp_path: Path, *, dry_run: bool = False):
    return SimpleNamespace(
        task="task", projects={"consumer": tmp_path, "producer": tmp_path},
        output_dir=None, dry_run=dry_run, plan_file=None, resume_from=None,
        resumed_meta=None,
    )


def test_admitted_plan_writes_graph_before_plan_mode_return(tmp_path, monkeypatch) -> None:
    ctx, request = _context(tmp_path), _request(tmp_path)
    monkeypatch.setattr(session_run, "_run_cross_planning", lambda _ctx: CrossPlanningResult(status="approved", plan_output=_PLAN, plan_approved=True, skipped_phase0=True))
    monkeypatch.setattr(session_run, "_write_cross_checkpoint", lambda *_: None)
    assert session_run._run_planning(request, ctx) is True
    graph = load_cross_execution_graph(tmp_path)
    projects = [node for node in graph.nodes if node.kind.value == "project"]
    assert projects[0].identity in projects[1].dependencies


@pytest.mark.parametrize("dry_run,has_global_plan", [(True, True), (False, False)])
def test_non_admitted_paths_do_not_write_snapshot(tmp_path, monkeypatch, dry_run, has_global_plan) -> None:
    ctx, request = _context(tmp_path), _request(tmp_path, dry_run=dry_run)
    ctx.has_global_plan = has_global_plan
    monkeypatch.setattr(session_run, "_run_cross_planning", lambda _ctx: CrossPlanningResult(status="bypass", plan_approved=True, skipped_phase0=True))
    monkeypatch.setattr(session_run, "_write_cross_checkpoint", lambda *_: None)
    monkeypatch.setattr(session_run, "write_cross_execution_graph", lambda *_: pytest.fail("must not write"))
    assert session_run._run_planning(request, ctx) is True


def test_resume_without_graph_snapshot_stops_before_dispatch(tmp_path, monkeypatch) -> None:
    ctx, request = _context(tmp_path), _request(tmp_path)
    request.resume_from = "prior"
    monkeypatch.setattr(session_run, "_run_cross_planning", lambda _ctx: CrossPlanningResult(status="approved", plan_output=_PLAN, plan_approved=True, skipped_phase0=True))
    monkeypatch.setattr(session_run, "_write_cross_checkpoint", lambda *_: None)
    with pytest.raises(CrossExecutionGraphStoreError, match="graph is missing"):
        session_run._run_planning(request, ctx)


def test_resume_with_equal_inputs_accepts_existing_snapshot(tmp_path, monkeypatch) -> None:
    for resume_from in (None, "prior"):
        ctx, request = _context(tmp_path), _request(tmp_path)
        request.resume_from = resume_from
        monkeypatch.setattr(session_run, "_run_cross_planning", lambda _ctx: CrossPlanningResult(status="approved", plan_output=_PLAN, plan_approved=True, skipped_phase0=True))
        monkeypatch.setattr(session_run, "_write_cross_checkpoint", lambda *_: None)
        assert session_run._run_planning(request, ctx) is True


def test_graph_is_not_consumed_by_dispatch_gates_checkpoint_or_parent_reducer() -> None:
    from pipeline.cross_project import (
        cfa_gate,
        checkpoint,
        contract_check,
        parent_state_runtime,
    )

    for module in (checkpoint, contract_check, cfa_gate, parent_state_runtime):
        assert "execution_graph" not in inspect.getsource(module)


def test_dispatch_receives_graph_for_serial_selection(tmp_path, monkeypatch) -> None:
    ctx, request = _context(tmp_path), _request(tmp_path)
    ctx.task_plan = session_run.normalize_cross_task_plan(
        session_run.plan_parser.parse_cross_plan(_PLAN, ctx.aliases), ctx.aliases,
    )
    ctx.code_model = "model"
    ctx.child_profile = object()
    ctx.provider = object()
    ctx.plan_output = _PLAN
    ctx.plan_review_dict = None
    ctx.participant_set = None
    request.resume_from = None
    request.max_rounds = 1
    request.phase_config = None
    request.hypothesis_enabled = None
    request.followup_session_seeds_per_alias = None
    ctx.execution_graph = object()
    captured: list = []
    monkeypatch.setattr(session_run, "_run_project_dispatch", lambda dispatch: captured.append(dispatch.execution_graph) or SimpleNamespace(paused=True))
    assert session_run._run_dispatch_and_contract(request, ctx) is True
    assert captured == [ctx.execution_graph]
