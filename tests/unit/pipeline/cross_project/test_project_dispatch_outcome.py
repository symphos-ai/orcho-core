"""Durable child-outcome classification at the cross dispatch boundary."""

from __future__ import annotations

import json
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
from pipeline.cross_project.project_dispatch import (
    DispatchPorts,
    ProjectDispatchContext,
    run_project_dispatch,
)


def _context(tmp_path, aliases=("core", "mcp")) -> ProjectDispatchContext:
    projects = {}
    for alias in aliases:
        project = tmp_path / alias
        project.mkdir()
        projects[alias] = project
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return ProjectDispatchContext(
        task="test dispatch outcomes",
        projects=projects,
        task_plan=None,
        resume_from="resume-id",
        dry_run=False,
        max_rounds=1,
        code_model="test",
        phase_config=None,
        child_profile=object(),
        requested_profile_name="test",
        has_global_plan=False,
        provider=MagicMock(),
        hypothesis_enabled=False,
        followup_session_seeds_per_alias=None,
        run_dir=run_dir,
        output_dir=False,
        plan_output="",
        plan_review_dict=None,
        cross_ckpt={"sub_status": {}},
        session={"phases": {"projects": {}}},
        cross_phase_usage={},
        ports=DispatchPorts(MagicMock(), MagicMock(), MagicMock()),
        terminal=False,
    )


def _graph(*, aliases: tuple[str, ...], dependencies: dict[str, tuple[str, ...]]) -> CrossExecutionGraph:
    project = CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.PROJECT_PIPELINE)
    gate = CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.RUNNER_GATE, run="auto")
    identities = {alias: project_node_identity(alias) for alias in aliases}
    nodes = [
        CrossExecutionGraphNode(
            "global", CrossExecutionGraphNodeKind.GLOBAL_PHASE, (),
            CrossExecutionGraphNodeOwner.GLOBAL,
            CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.GLOBAL_HANDLER),
        )
    ]
    nodes.extend(
        CrossExecutionGraphNode(
            identities[alias], CrossExecutionGraphNodeKind.PROJECT,
            tuple(identities[dependency] for dependency in dependencies[alias]),
            CrossExecutionGraphNodeOwner.PROJECT, project,
        )
        for alias in aliases
    )
    nodes.extend((
        CrossExecutionGraphNode("contract", CrossExecutionGraphNodeKind.CONTRACT_CHECK, tuple(identities.values()), CrossExecutionGraphNodeOwner.RUNNER, gate),
        CrossExecutionGraphNode("cfa", CrossExecutionGraphNodeKind.CROSS_FINAL_ACCEPTANCE, ("contract",), CrossExecutionGraphNodeOwner.RUNNER, gate),
    ))
    return CrossExecutionGraph(CrossExecutionGraphCompileIdentity(1, "test"), tuple(nodes))


def test_normal_return_halted_preserves_session_and_blocks_dispatch(tmp_path, monkeypatch) -> None:
    ctx = _context(tmp_path, ("core",))
    halted = {
        "status": "halted",
        "halt_reason": "operator requested stop",
        "phases": {"implement": {"status": "halted"}},
    }
    monkeypatch.setattr(
        project_dispatch,
        "run_project_pipeline",
        lambda request: SimpleNamespace(session=halted),
    )

    result = run_project_dispatch(ctx)

    assert result == project_dispatch.ProjectDispatchResult(
        paused=False, blocking_aliases=("core",),
    )
    assert ctx.cross_ckpt["sub_status"] == {"core": "failed"}
    assert ctx.session["phases"]["projects"]["core"] is halted


def test_exception_records_failure_and_continues_to_later_alias(tmp_path, monkeypatch) -> None:
    ctx = _context(tmp_path)
    calls: list[str] = []

    def _child(request):
        calls.append(request.project_alias)
        if request.project_alias == "core":
            raise RuntimeError("broken child")
        session = {"status": "done", "phases": {}}
        (ctx.run_dir / request.project_alias / "meta.json").write_text(json.dumps(session))
        return SimpleNamespace(session=session)

    monkeypatch.setattr(project_dispatch, "run_project_pipeline", _child)

    result = run_project_dispatch(ctx)

    assert calls == ["core", "mcp"]
    assert result.blocking_aliases == ("core",)
    assert ctx.cross_ckpt["sub_status"] == {"core": "failed", "mcp": "done"}
    assert ctx.session["phases"]["projects"]["core"] == {
        "status": "failed",
        "error": "RuntimeError: broken child",
        "phases": {},
    }


def test_resume_retries_embedded_done_without_physical_child(tmp_path, monkeypatch) -> None:
    ctx = _context(tmp_path, ("done", "failed", "unknown"))
    ctx.cross_ckpt["sub_status"] = {
        "done": "done", "failed": "failed", "unknown": "wat",
    }
    ctx.session["phases"]["projects"] = {
        "done": {"status": "done"},
        "failed": {"status": "failed"},
        "unknown": {"status": "wat"},
    }
    calls: list[str] = []
    def _child(request):
        calls.append(request.project_alias)
        session = {"status": "done", "phases": {}}
        (ctx.run_dir / request.project_alias / "meta.json").write_text(json.dumps(session))
        return SimpleNamespace(session=session)

    monkeypatch.setattr(project_dispatch, "run_project_pipeline", _child)

    result = run_project_dispatch(ctx)

    assert calls == ["done", "failed", "unknown"]
    assert result.blocking_aliases == ()
    assert ctx.session["phases"]["projects"]["done"] == {"status": "done", "phases": {}}


def test_residual_handoff_on_failed_child_is_not_paused(tmp_path, monkeypatch) -> None:
    ctx = _context(tmp_path, ("core",))
    monkeypatch.setattr(
        project_dispatch,
        "run_project_pipeline",
        lambda request: SimpleNamespace(
            session={
                "status": "failed",
                "phase_handoff": {"id": "child-1", "available_actions": ["continue"]},
                "phases": {},
            }
        ),
    )

    result = run_project_dispatch(ctx)

    assert result == project_dispatch.ProjectDispatchResult(paused=False, blocking_aliases=("core",))


def test_graph_dispatch_runs_reverse_dependency_producer_first(tmp_path, monkeypatch) -> None:
    ctx = _context(tmp_path, ("consumer", "producer"))
    ctx.execution_graph = _graph(
        aliases=("producer", "consumer"),
        dependencies={"producer": (), "consumer": ("producer",)},
    )
    calls: list[str] = []

    def child(request):
        calls.append(request.project_alias)
        session = {"status": "done", "phases": {}}
        (ctx.run_dir / request.project_alias).mkdir(exist_ok=True)
        (ctx.run_dir / request.project_alias / "meta.json").write_text(json.dumps(session))
        return SimpleNamespace(session=session)

    monkeypatch.setattr(project_dispatch, "run_project_pipeline", child)
    assert run_project_dispatch(ctx).paused is False
    assert calls == ["producer", "consumer"]


def test_graph_dispatch_runs_independent_aliases_serially_in_graph_order(tmp_path, monkeypatch) -> None:
    ctx = _context(tmp_path, ("first", "second"))
    ctx.execution_graph = _graph(
        aliases=("first", "second"), dependencies={"first": (), "second": ()}
    )
    calls: list[str] = []

    def child(request):
        calls.append(request.project_alias)
        session = {"status": "done", "phases": {}}
        (ctx.run_dir / request.project_alias).mkdir(exist_ok=True)
        (ctx.run_dir / request.project_alias / "meta.json").write_text(json.dumps(session))
        return SimpleNamespace(session=session)

    monkeypatch.setattr(project_dispatch, "run_project_pipeline", child)
    assert run_project_dispatch(ctx).paused is False
    assert calls == ["first", "second"]


def test_graph_dispatch_blocks_consumers_but_runs_independent_alias(tmp_path, monkeypatch) -> None:
    ctx = _context(tmp_path, ("consumer", "producer", "independent"))
    ctx.execution_graph = _graph(
        aliases=("producer", "consumer", "independent"),
        dependencies={"producer": (), "consumer": ("producer",), "independent": ()},
    )
    calls: list[str] = []

    def child(request):
        calls.append(request.project_alias)
        status = "failed" if request.project_alias == "producer" else "done"
        session = {"status": status, "phases": {}}
        (ctx.run_dir / request.project_alias).mkdir(exist_ok=True)
        (ctx.run_dir / request.project_alias / "meta.json").write_text(json.dumps(session))
        return SimpleNamespace(session=session)

    monkeypatch.setattr(project_dispatch, "run_project_pipeline", child)
    result = run_project_dispatch(ctx)
    assert calls == ["producer", "independent"]
    assert set(result.blocking_aliases) == {"producer", "consumer"}


def test_graph_dispatch_checkpoint_done_does_not_skip_missing_child(tmp_path, monkeypatch) -> None:
    ctx = _context(tmp_path, ("core",))
    ctx.execution_graph = _graph(aliases=("core",), dependencies={"core": ()})
    ctx.cross_ckpt["sub_status"] = {"core": "done"}
    ctx.session["phases"]["projects"] = {"core": {"status": "done"}}
    calls: list[str] = []

    def child(request):
        calls.append(request.project_alias)
        session = {"status": "done", "phases": {}}
        (ctx.run_dir / "core").mkdir(exist_ok=True)
        (ctx.run_dir / "core" / "meta.json").write_text(json.dumps(session))
        return SimpleNamespace(session=session)

    monkeypatch.setattr(project_dispatch, "run_project_pipeline", child)
    assert run_project_dispatch(ctx).paused is False
    assert calls == ["core"]


def test_graph_dispatch_preserves_physical_completion_without_checkpoint(tmp_path, monkeypatch) -> None:
    ctx = _context(tmp_path, ("core",))
    ctx.execution_graph = _graph(aliases=("core",), dependencies={"core": ()})
    completed = {"status": "done", "phases": {}}
    ctx.session["phases"]["projects"] = {"core": completed}
    (ctx.run_dir / "core").mkdir()
    (ctx.run_dir / "core" / "meta.json").write_text(json.dumps(completed))
    monkeypatch.setattr(
        project_dispatch,
        "run_project_pipeline",
        lambda _request: (_ for _ in ()).throw(AssertionError("completed child was redispatched")),
    )
    assert run_project_dispatch(ctx).paused is False
    assert ctx.session["phases"]["projects"]["core"] is completed
