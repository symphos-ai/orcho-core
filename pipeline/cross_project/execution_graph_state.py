"""Pure derived state for the immutable cross execution graph."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pipeline.cross_project.execution_graph import (
    CrossExecutionGraph,
    CrossExecutionGraphExecutor,
    CrossExecutionGraphNodeKind,
    resolve_project_node_alias,
)
from pipeline.run_state.cross_parent import (
    ActiveOperation,
    ChildExecution,
    CrossParentState,
)

__all__ = [
    "CrossExecutionGraphNodeState", "CrossExecutionGraphOperation", "CrossExecutionGraphOperationExecutor",
    "CrossExecutionGraphReason", "CrossExecutionGraphState", "CrossExecutionGraphStatus",
    "RunnerGateFact", "RunnerGateFacts", "reduce_cross_execution_graph_state", "select_first_ready_node",
]


class CrossExecutionGraphStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class CrossExecutionGraphReason(StrEnum):
    GLOBAL_ALREADY_COMPLETED = "global_already_completed"
    CHILD_COMPLETED = "child_completed"
    CHILD_RUNNING = "child_running"
    CHILD_PAUSED = "child_paused"
    CHILD_FAILED = "child_failed"
    CHILD_INCONSISTENT = "child_inconsistent"
    OPTIONAL_PROJECT_NOT_RUN = "optional_project_not_run"
    DEPENDENCY_PENDING = "dependency_pending"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    RUNNER_GATE_COMPLETED = "runner_gate_completed"
    RUNNER_GATE_SKIPPED = "runner_gate_skipped"
    RUNNER_GATE_RUNNING = "runner_gate_running"
    POLICY_DISABLED = "policy_disabled"
    POLICY_NEVER = "policy_never"
    FACT_MISMATCH = "fact_mismatch"


class CrossExecutionGraphOperationExecutor(StrEnum):
    CHILD_PHASE = "child_phase"
    CHILD_SCHEDULED_GATE = "child_scheduled_gate"


@dataclass(frozen=True, slots=True)
class CrossExecutionGraphOperation:
    alias: str
    executor: CrossExecutionGraphOperationExecutor
    phase: str
    hook: str | None = None
    command: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunnerGateFact:
    identity: str
    completed: bool = False
    skipped: bool = False
    active: bool = False


@dataclass(frozen=True, slots=True)
class RunnerGateFacts:
    entries: tuple[RunnerGateFact, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))


@dataclass(frozen=True, slots=True)
class CrossExecutionGraphNodeState:
    identity: str
    kind: CrossExecutionGraphNodeKind
    status: CrossExecutionGraphStatus
    reason: CrossExecutionGraphReason
    alias: str | None = None
    operations: tuple[CrossExecutionGraphOperation, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "operations", tuple(self.operations))


@dataclass(frozen=True, slots=True)
class CrossExecutionGraphState:
    nodes: tuple[CrossExecutionGraphNodeState, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))


def _operations(alias: str, values: tuple[ActiveOperation, ...]) -> tuple[CrossExecutionGraphOperation, ...]:
    result: list[CrossExecutionGraphOperation] = []
    for value in values:
        if value.phase is not None:
            result.append(CrossExecutionGraphOperation(alias, CrossExecutionGraphOperationExecutor.CHILD_PHASE, value.phase.phase))
        elif value.gate is not None:
            result.append(CrossExecutionGraphOperation(alias, CrossExecutionGraphOperationExecutor.CHILD_SCHEDULED_GATE, value.gate.phase, value.gate.hook, value.gate.command))
    return tuple(result)


def _invalid(graph: CrossExecutionGraph, parent: CrossParentState, gates: RunnerGateFacts) -> bool:
    aliases = {child.alias for child in parent.children}
    projects = [node for node in graph.nodes if node.kind is CrossExecutionGraphNodeKind.PROJECT]
    # ``sub_status`` is a checkpoint cursor, not durable child evidence.  The
    # parent reducer records its contradiction for repair visibility, but the
    # graph must not turn that cursor alone into a node disposition.
    substantive_violations = tuple(
        violation
        for violation in parent.violations
        if violation.code != "checkpoint_sub_status_contradiction"
        # A runner-level CFA handoff has no child identity.  ADR-0148's
        # child-oriented checkpoint adapter records its ``kind=cfa`` cursor
        # as a mismatch against that intentionally empty child kind; it must
        # not invalidate the already-materialized contract gate on a CFA
        # continue resume.
        and not (
            violation.code == "checkpoint_kind_conflict"
            and parent.pending_decision is not None
            and parent.pending_decision.kind == ""
        )
    )
    if substantive_violations or len(projects) != len(aliases):
        return True
    if any(resolve_project_node_alias(graph, alias) is None for alias in aliases):
        return True
    known = {node.identity for node in graph.nodes if node.executor.executor is CrossExecutionGraphExecutor.RUNNER_GATE}
    seen: set[str] = set()
    for fact in gates.entries:
        if fact.identity not in known or fact.identity in seen or sum((fact.completed, fact.skipped, fact.active)) > 1:
            return True
        seen.add(fact.identity)
    return False


def reduce_cross_execution_graph_state(
    graph: CrossExecutionGraph,
    parent: CrossParentState,
    gate_facts: RunnerGateFacts | None = None,
) -> CrossExecutionGraphState:
    """Project graph status solely from immutable graph and canonical facts."""
    gate_facts = gate_facts or RunnerGateFacts()
    invalid = _invalid(graph, parent, gate_facts)
    children = {child.alias: child for child in parent.children}
    gate_index = {fact.identity: fact for fact in gate_facts.entries}
    states: list[CrossExecutionGraphNodeState] = []
    for node in graph.nodes:
        alias = next((a for a in children if resolve_project_node_alias(graph, a) is node), None)
        if node.kind is CrossExecutionGraphNodeKind.GLOBAL_PHASE:
            state = CrossExecutionGraphNodeState(node.identity, node.kind, CrossExecutionGraphStatus.COMPLETED, CrossExecutionGraphReason.GLOBAL_ALREADY_COMPLETED)
        elif invalid:
            state = CrossExecutionGraphNodeState(node.identity, node.kind, CrossExecutionGraphStatus.BLOCKED, CrossExecutionGraphReason.FACT_MISMATCH, alias)
        elif node.kind is CrossExecutionGraphNodeKind.PROJECT:
            child = children[alias]  # alias is validated above
            # A global-only profile still has structural project nodes, but no
            # child pipeline is admitted for them.  They are optional graph
            # predecessors and must be satisfied without inventing a child
            # completion fact; required missing children remain pending.
            if not node.required and child.execution is ChildExecution.PENDING:
                state = CrossExecutionGraphNodeState(node.identity, node.kind, CrossExecutionGraphStatus.SKIPPED, CrossExecutionGraphReason.OPTIONAL_PROJECT_NOT_RUN, alias)
            elif child.execution is ChildExecution.RUNNING:
                state = CrossExecutionGraphNodeState(node.identity, node.kind, CrossExecutionGraphStatus.RUNNING, CrossExecutionGraphReason.CHILD_RUNNING, alias, _operations(alias, child.active_operations))
            elif child.execution is ChildExecution.PAUSED:
                state = CrossExecutionGraphNodeState(node.identity, node.kind, CrossExecutionGraphStatus.BLOCKED, CrossExecutionGraphReason.CHILD_PAUSED, alias)
            elif child.execution is ChildExecution.INCONSISTENT:
                state = CrossExecutionGraphNodeState(node.identity, node.kind, CrossExecutionGraphStatus.BLOCKED, CrossExecutionGraphReason.CHILD_INCONSISTENT, alias)
            elif child.execution is ChildExecution.TERMINAL and child.contract_evaluable:
                state = CrossExecutionGraphNodeState(node.identity, node.kind, CrossExecutionGraphStatus.COMPLETED, CrossExecutionGraphReason.CHILD_COMPLETED, alias)
            elif child.execution is ChildExecution.TERMINAL:
                state = CrossExecutionGraphNodeState(node.identity, node.kind, CrossExecutionGraphStatus.BLOCKED, CrossExecutionGraphReason.CHILD_FAILED, alias)
            else:
                state = CrossExecutionGraphNodeState(node.identity, node.kind, CrossExecutionGraphStatus.PENDING, CrossExecutionGraphReason.DEPENDENCY_PENDING, alias)
        else:
            fact = gate_index.get(node.identity)
            if not node.executor.enabled:
                state = CrossExecutionGraphNodeState(node.identity, node.kind, CrossExecutionGraphStatus.SKIPPED, CrossExecutionGraphReason.POLICY_DISABLED)
            elif node.executor.run == "never":
                state = CrossExecutionGraphNodeState(node.identity, node.kind, CrossExecutionGraphStatus.SKIPPED, CrossExecutionGraphReason.POLICY_NEVER)
            elif fact and fact.completed:
                state = CrossExecutionGraphNodeState(node.identity, node.kind, CrossExecutionGraphStatus.COMPLETED, CrossExecutionGraphReason.RUNNER_GATE_COMPLETED)
            elif fact and fact.skipped:
                state = CrossExecutionGraphNodeState(node.identity, node.kind, CrossExecutionGraphStatus.SKIPPED, CrossExecutionGraphReason.RUNNER_GATE_SKIPPED)
            elif fact and fact.active:
                state = CrossExecutionGraphNodeState(node.identity, node.kind, CrossExecutionGraphStatus.RUNNING, CrossExecutionGraphReason.RUNNER_GATE_RUNNING)
            else:
                state = CrossExecutionGraphNodeState(node.identity, node.kind, CrossExecutionGraphStatus.PENDING, CrossExecutionGraphReason.DEPENDENCY_PENDING)
        if state.status is CrossExecutionGraphStatus.PENDING:
            previous = {
                prior.identity: states[index]
                for index, prior in enumerate(graph.nodes[:len(states)])
            }
            dependencies = [previous.get(dependency) for dependency in node.dependencies]
            if any(item is None for item in dependencies) or any(item.status is CrossExecutionGraphStatus.BLOCKED for item in dependencies):
                state = CrossExecutionGraphNodeState(
                    node.identity, node.kind, CrossExecutionGraphStatus.BLOCKED,
                    CrossExecutionGraphReason.DEPENDENCY_BLOCKED, state.alias, state.operations,
                )
            elif any(
                item.status not in {CrossExecutionGraphStatus.COMPLETED, CrossExecutionGraphStatus.SKIPPED}
                for item in dependencies
            ):
                state = CrossExecutionGraphNodeState(node.identity, node.kind, CrossExecutionGraphStatus.PENDING, CrossExecutionGraphReason.DEPENDENCY_PENDING, state.alias, state.operations)
            else:
                state = CrossExecutionGraphNodeState(node.identity, node.kind, CrossExecutionGraphStatus.READY, CrossExecutionGraphReason.DEPENDENCY_PENDING, state.alias, state.operations)
        elif (
            node.executor.executor is CrossExecutionGraphExecutor.RUNNER_GATE
            and state.status is not CrossExecutionGraphStatus.SKIPPED
            and (
                any(
                    dependency not in {prior.identity for prior in graph.nodes[:len(states)]}
                    for dependency in node.dependencies
                )
                or any(
                    states[next(i for i, prior in enumerate(graph.nodes[:len(states)]) if prior.identity == dependency)].status
                    is CrossExecutionGraphStatus.BLOCKED
                    for dependency in node.dependencies
                )
            )
        ):
            state = CrossExecutionGraphNodeState(
                node.identity, node.kind, CrossExecutionGraphStatus.BLOCKED,
                CrossExecutionGraphReason.DEPENDENCY_BLOCKED,
            )
        states.append(state)
    return CrossExecutionGraphState(tuple(states))


def select_first_ready_node(state: CrossExecutionGraphState) -> CrossExecutionGraphNodeState | None:
    return next((node for node in state.nodes if node.status is CrossExecutionGraphStatus.READY), None)
