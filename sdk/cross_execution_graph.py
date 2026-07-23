"""Read-only SDK access to an immutable cross execution graph snapshot."""
from __future__ import annotations

from pathlib import Path

from pipeline.cross_project.execution_graph import (
    CrossExecutionGraph,
    CrossExecutionGraphCompileIdentity,
    CrossExecutionGraphExecutor,
    CrossExecutionGraphExecutorPolicy,
    CrossExecutionGraphNode,
    CrossExecutionGraphNodeKind,
    CrossExecutionGraphNodeOwner,
)
from pipeline.cross_project.execution_graph_store import (
    CrossExecutionGraphStoreError,
    load_cross_execution_graph as _load,
)
from sdk.errors import CrossExecutionGraphInvalid
from sdk.runs import _CWD_DEFAULT, find_run

__all__ = [
    "CrossExecutionGraph",
    "CrossExecutionGraphCompileIdentity",
    "CrossExecutionGraphExecutor",
    "CrossExecutionGraphExecutorPolicy",
    "CrossExecutionGraphNode",
    "CrossExecutionGraphNodeKind",
    "CrossExecutionGraphNodeOwner",
    "load_cross_execution_graph",
]


def load_cross_execution_graph(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> CrossExecutionGraph:
    """Load one persisted C1 structural graph without modifying the run.

    ``NoWorkspace`` and ``RunNotFound`` from context resolution propagate.
    A missing, malformed, unsupported, or internally inconsistent snapshot is
    reported as :class:`sdk.errors.CrossExecutionGraphInvalid`; this reader
    never reconstructs a graph from plan or runtime state.
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    try:
        return _load(ref.run_dir)
    except CrossExecutionGraphStoreError as exc:
        raise CrossExecutionGraphInvalid(str(exc)) from exc
