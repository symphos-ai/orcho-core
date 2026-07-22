"""Contracts for the pure C2 cross execution-graph reducer."""
from __future__ import annotations

import dataclasses

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
from pipeline.cross_project.execution_graph_state import (
    CrossExecutionGraphOperationExecutor,
    CrossExecutionGraphReason,
    CrossExecutionGraphStatus,
    RunnerGateFact,
    RunnerGateFacts,
    reduce_cross_execution_graph_state,
    select_first_ready_node,
)
from pipeline.run_state.cross_parent import (
    ActiveOperation,
    ChildFacts,
    CrossParentFacts,
    Observation,
    PhaseIdentity,
    ScheduledGateIdentity,
    reduce_cross_parent_state,
)


def _graph() -> CrossExecutionGraph:
    producer, consumer, independent = (project_node_identity(alias) for alias in ("producer", "consumer", "independent"))
    contract, cfa = "contract", "cfa"
    project = CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.PROJECT_PIPELINE)
    gate = CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.RUNNER_GATE, run="auto")
    return CrossExecutionGraph(CrossExecutionGraphCompileIdentity(1, "test"), (
        CrossExecutionGraphNode("global", CrossExecutionGraphNodeKind.GLOBAL_PHASE, (), CrossExecutionGraphNodeOwner.GLOBAL, CrossExecutionGraphExecutorPolicy(CrossExecutionGraphExecutor.GLOBAL_HANDLER)),
        CrossExecutionGraphNode(producer, CrossExecutionGraphNodeKind.PROJECT, ("global",), CrossExecutionGraphNodeOwner.PROJECT, project),
        CrossExecutionGraphNode(consumer, CrossExecutionGraphNodeKind.PROJECT, (producer,), CrossExecutionGraphNodeOwner.PROJECT, project),
        CrossExecutionGraphNode(independent, CrossExecutionGraphNodeKind.PROJECT, ("global",), CrossExecutionGraphNodeOwner.PROJECT, project),
        CrossExecutionGraphNode(contract, CrossExecutionGraphNodeKind.CONTRACT_CHECK, (producer, consumer, independent), CrossExecutionGraphNodeOwner.RUNNER, gate),
        CrossExecutionGraphNode(cfa, CrossExecutionGraphNodeKind.CROSS_FINAL_ACCEPTANCE, (contract,), CrossExecutionGraphNodeOwner.RUNNER, gate),
    ))


def _parent(*children: ChildFacts):
    return reduce_cross_parent_state(CrossParentFacts(("producer", "consumer", "independent"), children))


def test_failed_producer_blocks_direct_and_transitive_consumers_but_not_independent() -> None:
    state = reduce_cross_execution_graph_state(_graph(), _parent(
        ChildFacts("producer", Observation.PRESENT, "failed"),
    ))
    by_alias = {node.alias: node for node in state.nodes if node.alias}
    assert by_alias["producer"].status is CrossExecutionGraphStatus.BLOCKED
    assert by_alias["consumer"].reason is CrossExecutionGraphReason.DEPENDENCY_BLOCKED
    assert by_alias["independent"].status is CrossExecutionGraphStatus.READY
    assert select_first_ready_node(state).alias == "independent"


def test_all_statuses_and_runner_gate_facts_are_immutable() -> None:
    parent = _parent(
        ChildFacts("producer", Observation.PRESENT, "done"),
        ChildFacts("consumer", Observation.PRESENT, "running", active_operations=(ActiveOperation(phase=PhaseIdentity("implement", "consumer")),)),
    )
    graph = _graph()
    state = reduce_cross_execution_graph_state(graph, parent, RunnerGateFacts((RunnerGateFact("contract", skipped=True),)))
    assert {node.status for node in state.nodes} == {
        CrossExecutionGraphStatus.COMPLETED, CrossExecutionGraphStatus.RUNNING,
        CrossExecutionGraphStatus.READY, CrossExecutionGraphStatus.SKIPPED,
    }
    assert reduce_cross_execution_graph_state(graph, parent, RunnerGateFacts((RunnerGateFact("contract", skipped=True),))) == state
    unrecorded = reduce_cross_execution_graph_state(graph, parent)
    assert next(node for node in unrecorded.nodes if node.identity == "contract").status is CrossExecutionGraphStatus.PENDING


def test_reason_vocabulary_is_closed_and_stable() -> None:
    assert {reason.value for reason in CrossExecutionGraphReason} == {
        "global_already_completed", "child_completed", "child_running", "child_paused",
        "child_failed", "child_inconsistent", "optional_project_not_run", "dependency_pending", "dependency_blocked",
        "runner_gate_completed", "runner_gate_skipped", "runner_gate_running",
        "policy_disabled", "policy_never", "fact_mismatch",
    }


def test_nested_child_phase_and_gate_keep_alias_and_distinct_executor_identity() -> None:
    state = reduce_cross_execution_graph_state(_graph(), _parent(ChildFacts(
        "producer", Observation.PRESENT, "running", active_operations=(
            ActiveOperation(phase=PhaseIdentity("implement", "producer")),
            ActiveOperation(gate=ScheduledGateIdentity("implement", "after_phase", ("python", "-m", "pytest"), "producer")),
        ),
    )))
    operations = next(node for node in state.nodes if node.alias == "producer").operations
    assert [op.alias for op in operations] == ["producer", "producer"]
    assert [op.executor for op in operations] == [CrossExecutionGraphOperationExecutor.CHILD_PHASE, CrossExecutionGraphOperationExecutor.CHILD_SCHEDULED_GATE]


def test_checkpoint_hints_do_not_choose_a_graph_outcome_without_child_fact() -> None:
    graph = _graph()
    no_checkpoint = reduce_cross_execution_graph_state(graph, _parent())
    done_checkpoint = reduce_cross_execution_graph_state(
        graph, _parent(ChildFacts("producer", checkpoint_sub_status="done"))
    )
    failed_checkpoint = reduce_cross_execution_graph_state(
        graph, _parent(ChildFacts("producer", checkpoint_sub_status="failed"))
    )
    assert done_checkpoint == no_checkpoint
    assert failed_checkpoint == no_checkpoint


def test_unmatched_runner_fact_fails_closed() -> None:
    graph = _graph()
    bad_fact_state = reduce_cross_execution_graph_state(graph, _parent(), RunnerGateFacts((RunnerGateFact("unknown", completed=True),)))
    assert all(node.status is CrossExecutionGraphStatus.BLOCKED for node in bad_fact_state.nodes[1:])


def test_unmatched_project_identity_fails_closed() -> None:
    graph = _graph()
    mismatched = dataclasses.replace(
        graph,
        nodes=(graph.nodes[0], dataclasses.replace(graph.nodes[1], identity="wrong"), *graph.nodes[2:]),
    )
    state = reduce_cross_execution_graph_state(mismatched, _parent())
    assert all(node.status is CrossExecutionGraphStatus.BLOCKED for node in state.nodes[1:])


def test_dangling_dependency_fails_closed_instead_of_raising_stop_iteration() -> None:
    graph = dataclasses.replace(
        _graph(),
        nodes=(
            _graph().nodes[0],
            dataclasses.replace(_graph().nodes[1], dependencies=("dangling",)),
            *_graph().nodes[2:],
        ),
    )
    state = reduce_cross_execution_graph_state(graph, _parent())
    producer = next(node for node in state.nodes if node.alias == "producer")
    assert producer.status is CrossExecutionGraphStatus.BLOCKED
    assert producer.reason is CrossExecutionGraphReason.DEPENDENCY_BLOCKED
