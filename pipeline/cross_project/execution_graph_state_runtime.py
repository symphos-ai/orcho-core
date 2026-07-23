"""Live adapter for runner-owned cross gate facts."""
from __future__ import annotations

from typing import Any

from pipeline.cross_project.cfa_gate import has_completed_cfa_phase_entry
from pipeline.cross_project.contract_check import _has_completed_contract_cache
from pipeline.cross_project.execution_graph import (
    CrossExecutionGraph,
    CrossExecutionGraphNodeKind,
)
from pipeline.cross_project.execution_graph_state import (
    CrossExecutionGraphState,
    RunnerGateFact,
    RunnerGateFacts,
    reduce_cross_execution_graph_state,
)
from pipeline.cross_project.parent_state_runtime import reduce_runtime_cross_parent_state


def build_runtime_runner_gate_facts(
    graph: CrossExecutionGraph,
    session: dict[str, Any],
    checkpoint: dict[str, Any] | None = None,
) -> RunnerGateFacts:
    """Normalize runner facts without broadening gate cache semantics."""
    phases = session.get("phases", {}) if isinstance(session.get("phases"), dict) else {}
    checkpoint = checkpoint or {}
    entries = []
    for node in graph.nodes:
        if node.kind not in {
            CrossExecutionGraphNodeKind.CONTRACT_CHECK,
            CrossExecutionGraphNodeKind.CROSS_FINAL_ACCEPTANCE,
        }:
            continue
        value = phases.get(node.kind.value)
        if value is None:
            continue
        row = value if isinstance(value, dict) else {}
        cfa_handoff_pending = (
            checkpoint.get("phase_handoff_pending")
            and checkpoint.get("phase_handoff_kind") == "cfa"
        ) or (
            isinstance(session.get("phase_handoff"), dict)
            and session["phase_handoff"].get("kind") == "cfa"
        )
        if node.kind is CrossExecutionGraphNodeKind.CONTRACT_CHECK:
            completed = _has_completed_contract_cache(row)
            skipped = False
            active = False
        else:
            completed = has_completed_cfa_phase_entry(row) and not cfa_handoff_pending
            skipped = bool(row.get("skipped"))
            active = bool(cfa_handoff_pending)
        entries.append(
            RunnerGateFact(
                node.identity,
                completed=completed,
                skipped=skipped,
                active=active,
            )
        )
    return RunnerGateFacts(tuple(entries))


def reduce_runtime_cross_execution_graph_state(
    graph: CrossExecutionGraph,
    session: dict[str, Any],
    checkpoint: dict[str, Any],
    run_dir: str,
) -> CrossExecutionGraphState:
    """Compose existing canonical child reduction with runner gate facts.

    ``checkpoint`` is passed only to the parent reducer, which validates it as
    a routing hint; it cannot manufacture a graph node disposition.
    """
    return reduce_cross_execution_graph_state(
        graph,
        reduce_runtime_cross_parent_state(session, checkpoint, run_dir),
        build_runtime_runner_gate_facts(graph, session, checkpoint),
    )


__all__ = ["build_runtime_runner_gate_facts", "reduce_runtime_cross_execution_graph_state"]
