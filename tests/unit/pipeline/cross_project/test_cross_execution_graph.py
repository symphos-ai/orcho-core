"""Focused contract tests for immutable cross execution graph compilation."""
from __future__ import annotations

import dataclasses
import json

import pytest

from pipeline.cross_project.execution_graph import (
    CrossExecutionGraphCompileError,
    CrossExecutionGraphNodeKind,
    compile_cross_execution_graph,
)
from pipeline.cross_project.plan_parser import parse_cross_plan
from pipeline.cross_project.profile_projection import CrossProjection
from pipeline.cross_project.profile_setup import CrossProfileSetup
from pipeline.cross_project.task_plan import CrossTaskPlan, CrossTaskUnit, normalize_cross_task_plan
from pipeline.runtime import (
    CrossGatePolicy,
    CrossGateRunPolicy,
    CrossGateSkipPolicy,
    CrossScope,
    CrossStepPolicy,
    LoopStep,
    PhaseStep,
)


def _step(phase: str, handler: str = "cross_plan") -> PhaseStep:
    return PhaseStep(phase=phase, cross=CrossStepPolicy(CrossScope.GLOBAL, handler))


def _setup(*, project_steps: tuple = (PhaseStep("implement"),)) -> CrossProfileSetup:
    return CrossProfileSetup(
        requested_profile=object(),
        projection=CrossProjection(
            "test",
            (_step("plan"), LoopStep((_step("validate_plan", "cross_validate_plan"),), "validate_plan.approved")),
            project_steps,
        ),
        child_profile=object() if project_steps else None,
        projected_profile_name="test#project" if project_steps else None,
        contract_gate_policy=CrossGatePolicy(enabled=False, run=CrossGateRunPolicy.NEVER),
        cfa_gate_policy=CrossGatePolicy(
            enabled=True, run=CrossGateRunPolicy.AUTO,
            on_skip=CrossGateSkipPolicy.ALLOW,
        ),
        global_handlers=frozenset({"cross_plan", "cross_validate_plan"}),
    )


def _plan(units: tuple[CrossTaskUnit, ...]) -> CrossTaskPlan:
    return CrossTaskPlan("summary", "contract", ("narrative",), units)


def _unit(alias: str, deps: tuple[str, ...] = ()) -> CrossTaskUnit:
    return CrossTaskUnit(alias, alias, "goal", "spec", deps, (), "produces", "consumes")


def test_compiles_structural_nodes_and_reverse_dependency_stably() -> None:
    graph = compile_cross_execution_graph(
        _plan((_unit("consumer", ("producer",)), _unit("producer"), _unit("other"))),
        ("consumer", "producer", "other"), _setup(),
    )
    assert [node.kind for node in graph.nodes] == [
        CrossExecutionGraphNodeKind.GLOBAL_PHASE,
        CrossExecutionGraphNodeKind.GLOBAL_PHASE,
        CrossExecutionGraphNodeKind.PROJECT,
        CrossExecutionGraphNodeKind.PROJECT,
        CrossExecutionGraphNodeKind.PROJECT,
        CrossExecutionGraphNodeKind.CONTRACT_CHECK,
        CrossExecutionGraphNodeKind.CROSS_FINAL_ACCEPTANCE,
    ]
    projects = [node for node in graph.nodes if node.kind is CrossExecutionGraphNodeKind.PROJECT]
    # Node identities are opaque; ordering proves stable Kahn rather than alias sorting.
    assert projects[0].identity in projects[1].dependencies
    assert projects[2].identity not in projects[1].dependencies
    assert graph.nodes[-2].executor.runnable is False
    assert graph.nodes[-1].executor.runnable is True


def test_descriptive_fields_do_not_change_graph_or_inputs() -> None:
    plan = _plan((_unit("api"),))
    changed_unit = dataclasses.replace(plan.units[0], goal="other", spec="other", produces="x", consumes="y")
    changed = dataclasses.replace(plan, implementation_order=("other",), units=(changed_unit,))
    original = dataclasses.replace(plan)
    assert compile_cross_execution_graph(plan, ("api",), _setup()) == compile_cross_execution_graph(changed, ("api",), _setup())
    assert plan == original


def test_schema_admitted_duplicate_dependencies_compile_as_one_structural_edge() -> None:
    """C1 compilation must not reject a plan current schema admission accepts."""
    parsed = parse_cross_plan(json.dumps({
        "short_summary": "s",
        "interface_contract": "i",
        "implementation_order": [],
        "subtasks": [
            {"alias": "consumer", "goal": "g", "spec": "s", "depends_on": ["producer", "producer"], "files": []},
            {"alias": "producer", "goal": "g", "spec": "s", "depends_on": [], "files": []},
        ],
    }), ["consumer", "producer"])
    plan = normalize_cross_task_plan(parsed, ["consumer", "producer"])

    graph = compile_cross_execution_graph(plan, ("consumer", "producer"), _setup())
    projects = [node for node in graph.nodes if node.kind is CrossExecutionGraphNodeKind.PROJECT]

    assert len(projects[1].dependencies) == 2  # global predecessor + producer
    assert projects[0].identity in projects[1].dependencies


@pytest.mark.parametrize(
    ("plan", "owners", "message"),
    [
        (_plan((_unit("a"), _unit("a"))), ("a",), "duplicate project alias"),
        (_plan((_unit("a", ("missing",)),)), ("a",), "dangling dependency"),
        (_plan((_unit("a", ("a",)),)), ("a",), "self dependency"),
        (_plan((_unit("a", ("b",)), _unit("b", ("a",)))), ("a", "b"), "cycle"),
        (_plan((_unit("a"),)), ("b",), "unknown owner"),
    ],
)
def test_invalid_manual_inputs_fail_closed(plan, owners, message) -> None:
    with pytest.raises(CrossExecutionGraphCompileError, match=message):
        compile_cross_execution_graph(plan, owners, _setup())


def test_unassignable_profile_entry_fails_closed() -> None:
    setup = _setup()
    bad_projection = dataclasses.replace(setup.projection, global_steps=(PhaseStep("plan"),))
    with pytest.raises(CrossExecutionGraphCompileError, match="unassignable global"):
        compile_cross_execution_graph(_plan((_unit("api"),)), ("api",), dataclasses.replace(setup, projection=bad_projection))
