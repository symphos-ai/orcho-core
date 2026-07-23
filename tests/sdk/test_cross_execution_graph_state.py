"""Public contract smoke tests for the derived cross graph-state SDK reader."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from pipeline.cross_project.execution_graph import (
    CrossExecutionGraph,
    CrossExecutionGraphCompileIdentity,
    CrossExecutionGraphExecutor,
    CrossExecutionGraphExecutorPolicy,
    CrossExecutionGraphNode,
    CrossExecutionGraphNodeKind,
    CrossExecutionGraphNodeOwner,
    _fingerprint,
    project_node_identity,
)
from pipeline.cross_project.execution_graph_state_disk import load_runner_gate_facts
from pipeline.cross_project.execution_graph_state_runtime import (
    build_runtime_runner_gate_facts,
    reduce_runtime_cross_execution_graph_state,
)
from pipeline.cross_project.execution_graph_store import write_cross_execution_graph
from sdk import CrossExecutionGraphState, load_cross_execution_graph_state, to_jsonable


def test_graph_state_sdk_exports_typed_jsonable_reader() -> None:
    assert callable(load_cross_execution_graph_state)
    assert dataclasses.is_dataclass(CrossExecutionGraphState)
    assert CrossExecutionGraphState.__dataclass_params__.frozen is True
    assert "__slots__" in CrossExecutionGraphState.__dict__
    assert json.dumps(to_jsonable(CrossExecutionGraphState(()))) == '{"nodes": []}'


def _graph() -> CrossExecutionGraph:
    project = CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.PROJECT_PIPELINE)
    gate = CrossExecutionGraphExecutorPolicy(
        CrossExecutionGraphExecutor.RUNNER_GATE, run="always", on_skip="allow",
    )
    global_step = CrossExecutionGraphNode(
        "global", CrossExecutionGraphNodeKind.GLOBAL_PHASE, (),
        CrossExecutionGraphNodeOwner.GLOBAL,
        CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.GLOBAL_HANDLER, handler="plan"),
    )
    core = CrossExecutionGraphNode(
        project_node_identity("core"), CrossExecutionGraphNodeKind.PROJECT, ("global",),
        CrossExecutionGraphNodeOwner.PROJECT, project,
    )
    contract = CrossExecutionGraphNode(
        "contract", CrossExecutionGraphNodeKind.CONTRACT_CHECK, (core.identity,),
        CrossExecutionGraphNodeOwner.RUNNER, gate,
    )
    cfa = CrossExecutionGraphNode(
        "cfa", CrossExecutionGraphNodeKind.CROSS_FINAL_ACCEPTANCE, ("contract",),
        CrossExecutionGraphNodeOwner.RUNNER, gate,
    )
    nodes = (global_step, core, contract, cfa)
    return CrossExecutionGraph(CrossExecutionGraphCompileIdentity(1, _fingerprint(nodes)), nodes)


def test_sdk_disk_projection_equals_runtime_projection_and_is_read_only(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    child_dir = run_dir / "core"
    child_dir.mkdir(parents=True)
    graph = _graph()
    write_cross_execution_graph(run_dir, graph)
    meta = {
        "projects": {"core": str(tmp_path / "core")},
        "phases": {"projects": {"core": {"status": "done"}}},
    }
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (child_dir / "meta.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")

    runtime = reduce_runtime_cross_execution_graph_state(graph, meta, {}, str(run_dir))
    before = sorted(path.relative_to(run_dir) for path in run_dir.rglob("*"))
    disk = load_cross_execution_graph_state("run-1", runs_dir=runs_dir, cwd=None)
    after = sorted(path.relative_to(run_dir) for path in run_dir.rglob("*"))

    assert disk == runtime
    assert after == before


def test_disk_and_runtime_gate_facts_keep_cfa_handoff_active(tmp_path: Path) -> None:
    graph = _graph()
    write_cross_execution_graph(tmp_path, graph)
    meta = {"phases": {"cross_final_acceptance": {"verdict": "REJECTED"}}}
    checkpoint = {"phase_handoff_pending": True, "phase_handoff_kind": "cfa"}
    (tmp_path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (tmp_path / "cross_checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")

    runtime = build_runtime_runner_gate_facts(graph, meta, checkpoint)
    disk = load_runner_gate_facts(graph, tmp_path)

    assert disk == runtime
    cfa = next(fact for fact in disk.entries if fact.identity == "cfa")
    assert cfa.active is True
    assert cfa.completed is False


def test_disk_and_runtime_gate_facts_distinguish_completed_cfa_from_reusable_cache(tmp_path: Path) -> None:
    graph = _graph()
    write_cross_execution_graph(tmp_path, graph)
    valid = {
        "output": "approved", "raw_output": "{}", "approved": True,
        "verdict": "APPROVED", "short_summary": "ok", "ship_ready": True,
        "release_blockers": [], "verification_gaps": [],
        "contract_status": {
            "task_contract": "satisfied", "interfaces": "compatible",
            "persistence": "safe", "tests": "sufficient",
        },
        "source": "agent", "duration_s": 1.0,
    }
    rejected = {
        **valid,
        "approved": False,
        "verdict": "REJECTED",
        "ship_ready": False,
        "short_summary": "Release is blocked.",
        "release_blockers": [{
            "id": "CFA1", "severity": "P1", "title": "Blocked release",
            "body": "A cross-project invariant is broken.",
            "required_fix": "Repair the invariant.",
            "why_blocks_release": "Consumers would observe inconsistent data.",
        }],
        "contract_status": {
            "task_contract": "incomplete", "interfaces": "broken",
            "persistence": "safe", "tests": "weak",
        },
    }
    cases = {
        "approved": valid,
        "rejected_override": {**rejected, "override": {"action": "continue"}},
        "rejected_halted": rejected,
        "malformed": {"verdict": "APPROVED"},
    }

    for expected, entry in cases.items():
        meta = {"phases": {"cross_final_acceptance": entry}}
        (tmp_path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        runtime = build_runtime_runner_gate_facts(graph, meta, {})
        disk = load_runner_gate_facts(graph, tmp_path)
        assert disk == runtime
        cfa = next(fact for fact in runtime.entries if fact.identity == "cfa")
        assert cfa.completed is (expected != "malformed")
