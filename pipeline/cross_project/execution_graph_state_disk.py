"""Read-only durable adapter for runner-owned cross gate facts."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.cross_project.execution_graph import CrossExecutionGraph
from pipeline.cross_project.execution_graph_state import (
    CrossExecutionGraphState,
    RunnerGateFacts,
    reduce_cross_execution_graph_state,
)
from pipeline.cross_project.execution_graph_state_runtime import (
    build_runtime_runner_gate_facts,
)
from pipeline.run_state.cross_parent_disk import load_cross_parent_state


def load_runner_gate_facts(graph: CrossExecutionGraph, run_dir: Path | str) -> RunnerGateFacts:
    try:
        value = json.loads((Path(run_dir) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = {}
    try:
        checkpoint = json.loads((Path(run_dir) / "cross_checkpoint.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        checkpoint = {}
    return build_runtime_runner_gate_facts(
        graph,
        value if isinstance(value, dict) else {},
        checkpoint if isinstance(checkpoint, dict) else {},
    )


def load_cross_execution_graph_state(
    graph: CrossExecutionGraph, run_dir: Path | str
) -> CrossExecutionGraphState:
    """Read graph state without mutating the run directory or checkpoint."""
    return reduce_cross_execution_graph_state(
        graph, load_cross_parent_state(run_dir), load_runner_gate_facts(graph, run_dir)
    )


__all__ = ["load_cross_execution_graph_state", "load_runner_gate_facts"]
