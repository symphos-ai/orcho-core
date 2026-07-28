"""Project-handoff resume returns to normal cross graph scheduling."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from pipeline.cross_project import project_dispatch
from pipeline.cross_project.execution_graph import (
    CrossExecutionGraph,
    CrossExecutionGraphCompileIdentity,
    CrossExecutionGraphExecutor,
    CrossExecutionGraphExecutorPolicy,
    CrossExecutionGraphNode,
    CrossExecutionGraphNodeKind,
    CrossExecutionGraphNodeOwner,
    project_node_identity,
)
from pipeline.cross_project.execution_graph_state import (
    CrossExecutionGraphStatus,
    RunnerGateFact,
    RunnerGateFacts,
    reduce_cross_execution_graph_state,
    select_first_ready_node,
)
from pipeline.cross_project.execution_graph_state_runtime import (
    reduce_runtime_cross_execution_graph_state,
)
from pipeline.cross_project.handoff import resume_project_phase_handoff
from pipeline.cross_project.parent_state_runtime import reduce_runtime_cross_parent_state
from pipeline.cross_project.project_dispatch import (
    DispatchPorts,
    ProjectDispatchContext,
    run_project_dispatch,
)
from sdk.phase_handoff import phase_handoff_decide


def _graph() -> CrossExecutionGraph:
    project = CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.PROJECT_PIPELINE)
    gate = CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.RUNNER_GATE, run="auto")
    alpha = project_node_identity("alpha")
    beta = project_node_identity("beta")
    return CrossExecutionGraph(
        CrossExecutionGraphCompileIdentity(1, "resume-continuation"),
        (
            CrossExecutionGraphNode(
                "global", CrossExecutionGraphNodeKind.GLOBAL_PHASE, (),
                CrossExecutionGraphNodeOwner.GLOBAL,
                CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.GLOBAL_HANDLER),
            ),
            CrossExecutionGraphNode(alpha, CrossExecutionGraphNodeKind.PROJECT, (), CrossExecutionGraphNodeOwner.PROJECT, project),
            CrossExecutionGraphNode(beta, CrossExecutionGraphNodeKind.PROJECT, (), CrossExecutionGraphNodeOwner.PROJECT, project),
            CrossExecutionGraphNode("contract", CrossExecutionGraphNodeKind.CONTRACT_CHECK, (alpha, beta), CrossExecutionGraphNodeOwner.RUNNER, gate),
            CrossExecutionGraphNode("cfa", CrossExecutionGraphNodeKind.CROSS_FINAL_ACCEPTANCE, ("contract",), CrossExecutionGraphNodeOwner.RUNNER, gate),
        ),
    )


def test_project_handoff_resume_dispatches_remaining_ready_children_before_gates(
    tmp_path: Path, monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    projects = {alias: tmp_path / alias for alias in ("alpha", "beta")}
    for path in projects.values():
        path.mkdir()

    child_handoff_id = "review_changes:repair_round:1"
    parent_handoff_id = f"project:alpha:{child_handoff_id}"
    child_handoff = {
        "id": child_handoff_id,
        "phase": "review_changes",
        "type": "human_feedback_on_reject",
        "trigger": "rejected",
        "round": 1,
        "loop_max_rounds": 1,
        "available_actions": ["continue", "halt"],
        "artifacts": {},
    }
    parent_handoff = {**child_handoff, "id": parent_handoff_id, "artifacts": {
        "project_alias": "alpha", "child_handoff_id": child_handoff_id,
    }}
    paused_alpha = {"status": "awaiting_phase_handoff", "phase_handoff": child_handoff, "phases": {}}
    (run_dir / "alpha").mkdir()
    (run_dir / "alpha" / "meta.json").write_text(json.dumps(paused_alpha), encoding="utf-8")
    session = {
        "projects": {alias: str(path) for alias, path in projects.items()},
        "status": "awaiting_phase_handoff",
        "phase_handoff": parent_handoff,
        "phases": {"projects": {"alpha": paused_alpha}},
    }
    checkpoint = {
        "phase0_done": True,
        "sub_status": {"alpha": "awaiting_phase_handoff"},
        "phase_handoff_pending": True,
        "phase_handoff_id": parent_handoff_id,
        "phase_handoff_kind": "project",
        "phase_handoff_project_alias": "alpha",
        "phase_handoff_child_id": child_handoff_id,
    }
    (run_dir / "meta.json").write_text(json.dumps(session), encoding="utf-8")
    (run_dir / "cross_checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    phase_handoff_decide(run_dir.name, parent_handoff_id, "continue", runs_dir=run_dir.parent, cwd=None)

    assert not resume_project_phase_handoff(
        cross_ckpt=checkpoint, run_dir=run_dir, output_dir=run_dir, session=session, success=lambda _message: None,
    )
    persisted_parent = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    persisted_checkpoint = json.loads((run_dir / "cross_checkpoint.json").read_text(encoding="utf-8"))
    assert persisted_parent["status"] == "running"
    assert "phase_handoff" not in persisted_parent
    assert persisted_checkpoint["sub_status"]["alpha"] == "awaiting_phase_handoff"
    assert persisted_checkpoint["phase_handoff_pending"] is False

    calls: list[str] = []
    def child(request):
        calls.append(request.project_alias)
        result = {"status": "done", "phases": {}}
        (run_dir / request.project_alias).mkdir(exist_ok=True)
        (run_dir / request.project_alias / "meta.json").write_text(json.dumps(result), encoding="utf-8")
        return SimpleNamespace(session=result)

    monkeypatch.setattr(project_dispatch, "run_project_pipeline", child)
    ctx = ProjectDispatchContext(
        task="resume", projects=projects, task_plan=None, resume_from=run_dir.name,
        dry_run=False, max_rounds=1, code_model="test", phase_config=None,
        child_profile=object(), requested_profile_name="test", has_global_plan=False,
        provider=MagicMock(), hypothesis_enabled=False, followup_session_seeds_per_alias=None,
        run_dir=run_dir, output_dir=run_dir, plan_output="", plan_review_dict=None,
        cross_ckpt=checkpoint, session=session, cross_phase_usage={},
        ports=DispatchPorts(MagicMock(), MagicMock(), MagicMock()), terminal=False,
        execution_graph=_graph(), resolved_handoff_alias="alpha",
    )

    result = run_project_dispatch(ctx)

    assert result.paused is False
    assert calls == ["alpha", "beta"]
    assert ctx.session["phases"]["projects"]["alpha"]["status"] == "done"
    assert ctx.session["phases"]["projects"]["beta"]["status"] == "done"


def test_interrupted_child_resume_reaches_gates_after_both_children_done(
    tmp_path: Path, monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    projects = {alias: tmp_path / alias for alias in ("alpha", "beta")}
    for path in projects.values():
        path.mkdir()
    alpha = {"status": "done", "phases": {}}
    beta = {"status": "running", "phases": {"implement": {"status": "running"}}}
    for alias, child in (("alpha", alpha), ("beta", beta)):
        (run_dir / alias).mkdir()
        (run_dir / alias / "meta.json").write_text(json.dumps(child), encoding="utf-8")
    alpha_meta_before = (run_dir / "alpha" / "meta.json").read_bytes()
    session = {
        "projects": {alias: str(path) for alias, path in projects.items()},
        "phases": {"projects": {"alpha": alpha, "beta": beta}},
    }
    checkpoint = {"sub_status": {"alpha": "done", "beta": "running"}}
    calls: list[tuple[str, str | None]] = []

    def child(request):
        calls.append((request.project_alias, request.resume_from))
        result = {"status": "done", "phases": {}}
        (run_dir / "beta" / "meta.json").write_text(json.dumps(result), encoding="utf-8")
        return SimpleNamespace(session=result)

    monkeypatch.setattr(project_dispatch, "run_project_pipeline", child)
    ctx = ProjectDispatchContext(
        task="resume", projects=projects, task_plan=None, resume_from=run_dir.name,
        dry_run=False, max_rounds=1, code_model="test", phase_config=None,
        child_profile=object(), requested_profile_name="test", has_global_plan=False,
        provider=MagicMock(), hypothesis_enabled=False, followup_session_seeds_per_alias=None,
        run_dir=run_dir, output_dir=run_dir, plan_output="", plan_review_dict=None,
        cross_ckpt=checkpoint, session=session, cross_phase_usage={},
        ports=DispatchPorts(MagicMock(), MagicMock(), MagicMock()), terminal=False,
        execution_graph=_graph(),
    )

    assert run_project_dispatch(ctx).paused is False
    assert calls == [("beta", "beta")]
    assert (run_dir / "alpha" / "meta.json").read_bytes() == alpha_meta_before
    assert set(session["phases"]["projects"]) == {"alpha", "beta"}

    graph_state = reduce_runtime_cross_execution_graph_state(
        _graph(), session, checkpoint, str(run_dir),
    )
    assert select_first_ready_node(graph_state).identity == "contract"
    assert next(node for node in graph_state.nodes if node.identity == "cfa").status is CrossExecutionGraphStatus.PENDING

    gate_state = reduce_cross_execution_graph_state(
        _graph(),
        reduce_runtime_cross_parent_state(session, checkpoint, run_dir),
        RunnerGateFacts((RunnerGateFact("contract", completed=True),)),
    )
    assert select_first_ready_node(gate_state).identity == "cfa"
