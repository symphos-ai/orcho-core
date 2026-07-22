"""Public read-only contract tests for cross execution graph snapshots."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from pipeline.cross_project.execution_graph import (
    CrossExecutionGraph,
    CrossExecutionGraphCompileIdentity,
    CrossExecutionGraphExecutor,
    CrossExecutionGraphExecutorPolicy,
    CrossExecutionGraphNode,
    CrossExecutionGraphNodeKind,
    CrossExecutionGraphNodeOwner,
    _fingerprint,
)
from pipeline.cross_project.execution_graph_store import write_cross_execution_graph
from sdk import (
    CrossExecutionGraphInvalid,
    RunNotFound,
    load_cross_execution_graph,
    to_jsonable,
)


def _graph() -> CrossExecutionGraph:
    nodes = (
        CrossExecutionGraphNode("g", CrossExecutionGraphNodeKind.GLOBAL_PHASE, (), CrossExecutionGraphNodeOwner.GLOBAL, CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.GLOBAL_HANDLER, handler="cross_plan")),
        CrossExecutionGraphNode("p", CrossExecutionGraphNodeKind.PROJECT, ("g",), CrossExecutionGraphNodeOwner.PROJECT, CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.PROJECT_PIPELINE)),
        CrossExecutionGraphNode("c", CrossExecutionGraphNodeKind.CONTRACT_CHECK, ("p",), CrossExecutionGraphNodeOwner.RUNNER, CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.RUNNER_GATE, run="auto", on_skip="block")),
        CrossExecutionGraphNode("f", CrossExecutionGraphNodeKind.CROSS_FINAL_ACCEPTANCE, ("c",), CrossExecutionGraphNodeOwner.RUNNER, CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.RUNNER_GATE, run="never", on_skip="block")),
    )
    return CrossExecutionGraph(CrossExecutionGraphCompileIdentity(1, _fingerprint(nodes)), nodes)


def test_loader_is_typed_jsonable_and_read_only(tmp_path: Path) -> None:
    run = tmp_path / "cross-1"
    run.mkdir()
    write_cross_execution_graph(run, _graph())
    before = {path.relative_to(run): path.read_bytes() for path in run.rglob("*") if path.is_file()}
    graph = load_cross_execution_graph("cross-1", runs_dir=tmp_path, cwd=None)
    assert isinstance(graph, CrossExecutionGraph)
    assert json.dumps(to_jsonable(graph))
    after = {path.relative_to(run): path.read_bytes() for path in run.rglob("*") if path.is_file()}
    assert after == before
    for cls in (CrossExecutionGraph, CrossExecutionGraphCompileIdentity, CrossExecutionGraphNode, CrossExecutionGraphExecutorPolicy):
        assert dataclasses.is_dataclass(cls)
        assert cls.__dataclass_params__.frozen is True
        assert "__slots__" in cls.__dict__


def test_missing_or_invalid_snapshot_is_typed_sdk_error(tmp_path: Path) -> None:
    run = tmp_path / "cross-1"
    run.mkdir()
    with pytest.raises(CrossExecutionGraphInvalid, match="missing"):
        load_cross_execution_graph("cross-1", runs_dir=tmp_path, cwd=None)
    (run / "cross_execution_graph.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(CrossExecutionGraphInvalid, match="malformed"):
        load_cross_execution_graph("cross-1", runs_dir=tmp_path, cwd=None)


def test_missing_run_remains_run_not_found(tmp_path: Path) -> None:
    with pytest.raises(RunNotFound):
        load_cross_execution_graph("absent", runs_dir=tmp_path, cwd=None)
