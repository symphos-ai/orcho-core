"""Falsifiers for the pure canonical cross-parent reducer."""

from __future__ import annotations

import pytest

from pipeline.run_state.cross_parent import (
    ActiveOperation,
    CheckpointHandoff,
    ChildExecution,
    ChildFacts,
    CrossParentFacts,
    Observation,
    ParentClass,
    PendingDecision,
    PhaseIdentity,
    ReleaseDisposition,
    ScheduledGateIdentity,
    TerminalDisposition,
    reduce_cross_parent_state,
)


def _facts(*children: ChildFacts, **kwargs: object) -> CrossParentFacts:
    return CrossParentFacts(declared_aliases=("api", "web"), children=children, **kwargs)


def _success(alias: str) -> ChildFacts:
    return ChildFacts(alias, Observation.PRESENT, "done")


@pytest.mark.parametrize(
    ("child", "execution", "evaluable", "release", "blocker"),
    [
        (_success("api"), ChildExecution.TERMINAL, True, ReleaseDisposition.NOT_APPLICABLE, None),
        (
            ChildFacts("api", Observation.PRESENT, "failed"),
            ChildExecution.TERMINAL,
            False,
            ReleaseDisposition.UNAVAILABLE,
            "status:failed",
        ),
        (
            ChildFacts("api", Observation.PRESENT, "interrupted"),
            ChildExecution.TERMINAL,
            False,
            ReleaseDisposition.UNAVAILABLE,
            "status:interrupted",
        ),
        (
            ChildFacts("api"),
            ChildExecution.PENDING,
            False,
            ReleaseDisposition.UNAVAILABLE,
            "child_missing",
        ),
        (
            ChildFacts("api", Observation.MALFORMED),
            ChildExecution.INCONSISTENT,
            False,
            ReleaseDisposition.UNAVAILABLE,
            "physical_malformed",
        ),
        (
            ChildFacts("api", Observation.PRESENT, "unknown"),
            ChildExecution.TERMINAL,
            False,
            ReleaseDisposition.UNAVAILABLE,
            "status_unknown:unknown",
        ),
    ],
)
def test_child_outcomes_fail_closed(
    child: ChildFacts,
    execution: ChildExecution,
    evaluable: bool,
    release: ReleaseDisposition,
    blocker: str | None,
) -> None:
    state = reduce_cross_parent_state(_facts(child, _success("web")))
    projected = state.children[0]
    assert (projected.execution, projected.contract_evaluable, projected.release_disposition) == (
        execution,
        evaluable,
        release,
    )
    if blocker:
        assert blocker in {item.code for item in projected.blockers}


def test_running_phase_and_gate_are_ordered_active_operations() -> None:
    phase = ActiveOperation(phase=PhaseIdentity("implement", "api"))
    gate = ActiveOperation(
        gate=ScheduledGateIdentity("implement", "after_phase", ("python", "-m", "pytest"), "web")
    )
    state = reduce_cross_parent_state(
        _facts(_success("api"), _success("web"), active_operations=(phase, gate))
    )
    assert state.parent_class is ParentClass.RUNNING
    assert state.active_operations == (phase, gate)


def test_proxied_handoff_has_exact_payload_actions_and_checkpoint_routing() -> None:
    decision = PendingDecision(
        "project:api:parent",
        "project",
        ("continue", "halt"),
        "api",
        "project:api:parent",
        "child-1",
    )
    state = reduce_cross_parent_state(
        _facts(
            ChildFacts(
                "api", Observation.PRESENT, "awaiting_phase_handoff", pending_decision=decision
            ),
            _success("web"),
            checkpoint_handoff=CheckpointHandoff(
                True, "project", "api", "project:api:parent", "child-1"
            ),
        )
    )
    assert state.parent_class is ParentClass.AWAITING_OPERATOR
    assert state.pending_decision == decision
    assert state.pending_decision.available_actions == ("continue", "halt")


def test_parent_project_proxy_and_child_handoff_are_one_pending_decision() -> None:
    parent = PendingDecision(
        "project:api:parent",
        "project",
        ("continue", "halt"),
        "api",
        "project:api:parent",
        "child-1",
    )
    child = PendingDecision("child-1", "", ("continue", "halt"))
    state = reduce_cross_parent_state(
        _facts(
            ChildFacts(
                "api", Observation.PRESENT, PAUSE, pending_decision=child
            ),
            _success("web"),
            pending_decision=parent,
            checkpoint_handoff=CheckpointHandoff(
                True, "project", "api", "project:api:parent", "child-1"
            ),
        )
    )

    assert state.parent_class is ParentClass.AWAITING_OPERATOR
    assert state.pending_decision == parent
    assert state.violations == ()


def test_mismatched_project_proxy_and_child_handoff_remain_inconsistent() -> None:
    parent = PendingDecision(
        "project:api:parent", "project", ("continue",), "api", "project:api:parent", "child-1"
    )
    state = reduce_cross_parent_state(
        _facts(
            ChildFacts(
                "api",
                Observation.PRESENT,
                PAUSE,
                pending_decision=PendingDecision("other-child", "", ("continue",)),
            ),
            _success("web"),
            pending_decision=parent,
            checkpoint_handoff=CheckpointHandoff(
                True, "project", "api", "project:api:parent", "child-1"
            ),
        )
    )

    assert state.parent_class is ParentClass.INCONSISTENT
    assert "multiple_pending_decisions" in {item.code for item in state.violations}


def test_rejected_release_is_evaluable_but_not_release_ready() -> None:
    rejected = ChildFacts(
        "api",
        Observation.PRESENT,
        "halted",
        halt_reason="final_acceptance_rejected",
        release_verdict="REJECTED",
        release_ship_ready=False,
    )
    state = reduce_cross_parent_state(_facts(rejected, _success("web")))
    child = state.children[0]
    assert child.contract_evaluable is True
    assert child.release_disposition is ReleaseDisposition.REJECTED
    assert child.release_ready is False
    assert state.parent_class is ParentClass.BLOCKED


def test_checkpoint_done_cannot_override_failed_child() -> None:
    state = reduce_cross_parent_state(
        _facts(
            ChildFacts("api", Observation.PRESENT, "failed", checkpoint_sub_status="done"),
            _success("web"),
        )
    )
    assert state.parent_class is ParentClass.INCONSISTENT
    assert state.children[0].contract_evaluable is False
    assert {violation.code for violation in state.violations} == {
        "checkpoint_sub_status_contradiction"
    }


def test_payload_routing_conflict_is_inconsistent() -> None:
    decision = PendingDecision(
        "project:api:parent", "project", ("continue",), "api", "project:api:parent", "child-1"
    )
    state = reduce_cross_parent_state(
        _facts(
            ChildFacts("api", Observation.PRESENT, PAUSE, pending_decision=decision),
            _success("web"),
            checkpoint_handoff=CheckpointHandoff(
                True, "project", "web", "project:api:parent", "child-1"
            ),
        )
    )
    assert state.parent_class is ParentClass.INCONSISTENT
    assert "checkpoint_alias_conflict" in {item.code for item in state.violations}


def test_terminal_parent_cannot_mask_running_child_or_gate() -> None:
    state = reduce_cross_parent_state(
        _facts(
            _success("api"),
            _success("web"),
            parent_status="done",
            active_operations=(ActiveOperation(phase=PhaseIdentity("review")),),
        )
    )
    assert state.parent_class is ParentClass.INCONSISTENT
    assert "terminal_parent_contradiction" in {item.code for item in state.violations}


def test_terminal_success_requires_all_children_release_ready() -> None:
    state = reduce_cross_parent_state(
        _facts(_success("api"), _success("web"), parent_status="done")
    )
    assert state.parent_class is ParentClass.TERMINAL_SUCCESS
    assert state.terminal_disposition is TerminalDisposition.SUCCESS


def test_terminal_failure_allows_failed_child() -> None:
    state = reduce_cross_parent_state(
        _facts(ChildFacts("api", Observation.PRESENT, "failed"), _success("web"), parent_status="failed")
    )
    assert state.parent_class is ParentClass.TERMINAL_FAILURE
    assert state.terminal_disposition is TerminalDisposition.FAILURE


def test_terminal_halted_allows_rejected_child_release() -> None:
    rejected = ChildFacts(
        "api",
        Observation.PRESENT,
        "halted",
        halt_reason="final_acceptance_rejected",
        release_verdict="REJECTED",
        release_ship_ready=False,
    )
    state = reduce_cross_parent_state(_facts(rejected, _success("web"), parent_status="halted"))
    assert state.parent_class is ParentClass.TERMINAL_HALTED
    assert state.terminal_disposition is TerminalDisposition.HALTED


def test_residual_decision_on_failed_child_is_inconsistent() -> None:
    state = reduce_cross_parent_state(
        _facts(
            ChildFacts(
                "api",
                Observation.PRESENT,
                "failed",
                pending_decision=PendingDecision("child-1", "project", ("continue",), "api"),
            ),
            _success("web"),
        )
    )
    assert state.parent_class is ParentClass.INCONSISTENT
    assert "pending_decision_without_pause" in {item.code for item in state.violations}


def test_declared_order_extra_and_duplicate_child_facts_are_deterministic() -> None:
    state = reduce_cross_parent_state(
        _facts(_success("web"), _success("api"), _success("api"), _success("extra"))
    )
    assert [child.alias for child in state.children] == ["api", "web"]
    assert {item.code for item in state.violations} == {"duplicate_child_facts", "undeclared_child"}


def test_duplicate_declared_alias_is_reduced_once() -> None:
    state = reduce_cross_parent_state(
        CrossParentFacts(("api", "api", "web"), (_success("api"), _success("web")))
    )
    assert [child.alias for child in state.children] == ["api", "web"]
    assert "declared_alias_duplicate" in {item.code for item in state.violations}


def test_replay_of_equal_facts_is_equal_and_collections_are_tuples() -> None:
    facts = _facts(_success("api"), _success("web"))
    first = reduce_cross_parent_state(facts)
    assert first == reduce_cross_parent_state(facts)
    assert isinstance(first.children, tuple)
    assert isinstance(first.active_operations, tuple)
    assert isinstance(first.blockers, tuple)
    assert isinstance(first.violations, tuple)


def test_public_fact_collections_are_normalized_to_tuples() -> None:
    decision = PendingDecision("plan:1", "plan", ["continue"])
    facts = CrossParentFacts(["api"], [ChildFacts("api", active_operations=[])])
    assert isinstance(decision.available_actions, tuple)
    assert isinstance(facts.declared_aliases, tuple)
    assert isinstance(facts.children, tuple)
    assert isinstance(facts.children[0].active_operations, tuple)


PAUSE = "awaiting_phase_handoff"
