"""Thin serial selection helpers for the immutable cross execution graph."""
from __future__ import annotations

from pipeline.cross_project.execution_graph import CrossExecutionGraphNodeKind
from pipeline.cross_project.execution_graph_state import (
    CrossExecutionGraphNodeState,
    CrossExecutionGraphState,
    CrossExecutionGraphStatus,
    select_first_ready_node,
)

__all__ = ["select_ready_runner_gate", "select_first_ready_project", "selected_blocking_aliases"]


def select_first_ready_project(
    state: CrossExecutionGraphState,
) -> CrossExecutionGraphNodeState | None:
    """Return the first graph-ordered ready project, never a nested operation."""
    node = select_first_ready_node(state)
    return node if node is not None and node.kind is CrossExecutionGraphNodeKind.PROJECT else None


def selected_blocking_aliases(state: CrossExecutionGraphState) -> tuple[str, ...]:
    """Expose blocked project aliases for the existing dispatch outcome."""
    return tuple(
        node.alias
        for node in state.nodes
        if node.kind is CrossExecutionGraphNodeKind.PROJECT
        and node.status is CrossExecutionGraphStatus.BLOCKED
        and node.alias is not None
    )


def select_ready_runner_gate(
    state: CrossExecutionGraphState, kind: CrossExecutionGraphNodeKind
) -> CrossExecutionGraphNodeState | None:
    """Admit a runner gate only when it is the graph's next ready node."""
    node = select_first_ready_node(state)
    return node if node is not None and node.kind is kind else None
