"""Read-only SDK access to canonical derived cross graph state."""
from __future__ import annotations

from pathlib import Path

from pipeline.cross_project.execution_graph_state import (
    CrossExecutionGraphNodeState,
    CrossExecutionGraphOperation,
    CrossExecutionGraphOperationExecutor,
    CrossExecutionGraphReason,
    CrossExecutionGraphState,
    CrossExecutionGraphStatus,
    RunnerGateFact,
    RunnerGateFacts,
)
from pipeline.cross_project.execution_graph_state_disk import (
    load_cross_execution_graph_state as _load,
)
from sdk.cross_execution_graph import load_cross_execution_graph
from sdk.runs import _CWD_DEFAULT, find_run

__all__ = [
    "CrossExecutionGraphNodeState",
    "CrossExecutionGraphOperation",
    "CrossExecutionGraphOperationExecutor",
    "CrossExecutionGraphReason",
    "CrossExecutionGraphState",
    "CrossExecutionGraphStatus",
    "RunnerGateFact",
    "RunnerGateFacts",
    "load_cross_execution_graph_state",
]


def load_cross_execution_graph_state(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> CrossExecutionGraphState:
    """Load derived graph state without writing a status ledger or run artifact."""
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    return _load(load_cross_execution_graph(ref.run_id, runs_dir=ref.run_dir.parent, cwd=None), ref.run_dir)
