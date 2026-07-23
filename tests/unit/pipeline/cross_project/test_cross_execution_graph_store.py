"""Strict persistence tests for the immutable cross execution graph."""
from __future__ import annotations

import json
from dataclasses import replace

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
from pipeline.cross_project.execution_graph_store import (
    CROSS_EXECUTION_GRAPH_FILENAME,
    CrossExecutionGraphStoreError,
    load_cross_execution_graph,
    write_cross_execution_graph,
)


def _graph() -> CrossExecutionGraph:
    nodes = (
        CrossExecutionGraphNode("g", CrossExecutionGraphNodeKind.GLOBAL_PHASE, (), CrossExecutionGraphNodeOwner.GLOBAL, CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.GLOBAL_HANDLER, handler="cross_plan")),
        CrossExecutionGraphNode("p", CrossExecutionGraphNodeKind.PROJECT, ("g",), CrossExecutionGraphNodeOwner.PROJECT, CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.PROJECT_PIPELINE)),
        CrossExecutionGraphNode("c", CrossExecutionGraphNodeKind.CONTRACT_CHECK, ("p",), CrossExecutionGraphNodeOwner.RUNNER, CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.RUNNER_GATE, enabled=False, run="never", on_skip="block")),
        CrossExecutionGraphNode("f", CrossExecutionGraphNodeKind.CROSS_FINAL_ACCEPTANCE, ("c",), CrossExecutionGraphNodeOwner.RUNNER, CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.RUNNER_GATE, run="auto", on_skip="allow")),
    )
    return CrossExecutionGraph(CrossExecutionGraphCompileIdentity(1, _fingerprint(nodes)), nodes)


def test_round_trip_is_exact_and_wire_has_no_lifecycle_ledger(tmp_path) -> None:
    graph = _graph()
    write_cross_execution_graph(tmp_path, graph)
    assert load_cross_execution_graph(tmp_path) == graph
    wire = json.loads((tmp_path / CROSS_EXECUTION_GRAPH_FILENAME).read_text())
    assert set(wire) == {"schema_version", "compile_identity", "nodes"}
    assert "status" not in json.dumps(wire)


def test_equal_write_is_noop_and_bad_existing_snapshot_is_preserved(tmp_path) -> None:
    graph = _graph()
    path = write_cross_execution_graph(tmp_path, graph)
    before = path.read_bytes()
    write_cross_execution_graph(tmp_path, graph)
    assert path.read_bytes() == before
    path.write_text("{bad", encoding="utf-8")
    corrupt = path.read_bytes()
    with pytest.raises(CrossExecutionGraphStoreError, match="malformed"):
        write_cross_execution_graph(tmp_path, graph)
    assert path.read_bytes() == corrupt


def test_unequal_existing_snapshot_is_not_overwritten(tmp_path) -> None:
    graph = _graph()
    path = write_cross_execution_graph(tmp_path, graph)
    before = path.read_bytes()
    changed_contract = replace(
        graph.nodes[2], executor=replace(graph.nodes[2].executor, enabled=True),
    )
    changed_nodes = (graph.nodes[0], graph.nodes[1], changed_contract, graph.nodes[3])
    changed = CrossExecutionGraph(
        CrossExecutionGraphCompileIdentity(1, _fingerprint(changed_nodes)), changed_nodes,
    )
    with pytest.raises(CrossExecutionGraphStoreError, match="differs"):
        write_cross_execution_graph(tmp_path, changed)
    assert path.read_bytes() == before


def test_replace_failure_cleans_temp_and_preserves_target(tmp_path, monkeypatch) -> None:
    import pipeline.cross_project.execution_graph_store as store

    target = tmp_path / CROSS_EXECUTION_GRAPH_FILENAME
    target.write_bytes(b"previous target")
    # Make the writer take its atomic path while retaining a sentinel that a
    # failed replace must not alter.
    monkeypatch.setattr(store.Path, "exists", lambda _path: False)
    monkeypatch.setattr(store.os, "replace", lambda _src, _dst: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError, match="replace failed"):
        write_cross_execution_graph(tmp_path, _graph())
    assert target.read_bytes() == b"previous target"
    assert not list(tmp_path.glob(f".{CROSS_EXECUTION_GRAPH_FILENAME}.*.tmp"))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda wire: wire.__setitem__("schema_version", 99), "unknown graph schema"),
        (lambda wire: wire.__setitem__("status", "done"), "unexpected fields"),
        (lambda wire: wire["nodes"][0].__setitem__("kind", "unknown"), "invalid nodes"),
        (lambda wire: wire["nodes"][0].__setitem__("owner", "unknown"), "invalid nodes"),
        (lambda wire: wire["nodes"][1].__setitem__("dependencies", ["missing"]), "dangling"),
        (lambda wire: wire["compile_identity"].__setitem__("fingerprint", "wrong"), "fingerprint mismatch"),
    ],
)
def test_strict_loader_rejects_schema_extras_enums_edges_and_fingerprint(tmp_path, mutate, message) -> None:
    path = write_cross_execution_graph(tmp_path, _graph())
    wire = json.loads(path.read_text())
    mutate(wire)
    path.write_text(json.dumps(wire), encoding="utf-8")
    with pytest.raises(CrossExecutionGraphStoreError, match=message):
        load_cross_execution_graph(tmp_path)
