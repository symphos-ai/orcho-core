"""Unit tests for ``pipeline.subtask_substance_repair`` (ADR 0073).

Covers the filtered repair-DAG construction and the bounded repair loop:
only incomplete ids are scheduled, done dependencies ride along as read-only
``prior_results`` context, and the ``repair_attempts`` budget is honored.
"""
from __future__ import annotations

import pytest

from agents.entities import SubTask
from pipeline.dag_runner import (
    DagRunResult,
    ImplementationReceipt,
    PriorSubtaskContext,
    SubTaskResult,
)
from pipeline.plan_parser import ParsedPlan
from pipeline.subtask_substance_repair import (
    SubstanceRepairResult,
    build_repair_plan,
    run_substance_repair,
)


def _st(sid: str, deps: tuple[str, ...] = ()) -> SubTask:
    return SubTask(id=sid, goal=f"goal-{sid}", depends_on=deps)


def _plan(*subs: SubTask) -> ParsedPlan:
    return ParsedPlan(
        subtasks=tuple(subs),
        source="json",
        short_summary="s",
        goal="ship safely",
        acceptance_criteria=("all done",),
    )


def _receipt(sid: str, state: str, deps: tuple[str, ...] = ()) -> ImplementationReceipt:
    return ImplementationReceipt(
        subtask_id=sid, state=state, runtime="claude", model="m",
        skill=None, depends_on=deps,
    )


def _done_result(sid: str) -> SubTaskResult:
    return SubTaskResult(
        subtask_id=sid, runtime="claude", model="m", skill=None,
        output="repaired output", duration=0.1,
    )


# ── build_repair_plan ──────────────────────────────────────────────────────

def test_build_repair_plan_filters_to_incomplete_only() -> None:
    plan = _plan(_st("a"), _st("b", ("a",)), _st("c", ("a",)))
    repair = build_repair_plan(plan, {"b", "c"})
    assert [s.id for s in repair.subtasks] == ["b", "c"]  # order preserved
    # Other contract fields survive so repair agents see the full contract.
    assert repair.goal == "ship safely"
    assert repair.acceptance_criteria == ("all done",)
    # Done node ``a`` is intentionally absent from the filtered plan.
    assert all(s.id != "a" for s in repair.subtasks)


def test_build_repair_plan_keeps_dangling_dep_to_done_node() -> None:
    # ``b`` still declares depends_on=("a",) even though ``a`` is filtered out;
    # the runner seam treats ``a`` as a satisfied prior id.
    plan = _plan(_st("a"), _st("b", ("a",)))
    repair = build_repair_plan(plan, {"b"})
    (only,) = repair.subtasks
    assert only.id == "b"
    assert only.depends_on == ("a",)


# ── run_substance_repair: single repair ────────────────────────────────────

def test_single_repair_promotes_incomplete_to_done() -> None:
    plan = _plan(_st("a"), _st("b", ("a",)))
    seen_plans: list[tuple[str, ...]] = []
    seen_prior: list[set[str]] = []

    def repair_pass(repair_plan, prior_results):
        seen_plans.append(tuple(s.id for s in repair_plan.subtasks))
        seen_prior.append(set(prior_results))
        return DagRunResult(
            completed=(_done_result("b"),),
            receipts=(_receipt("b", "done", ("a",)),),
        )

    result = run_substance_repair(
        parsed_plan=plan,
        incomplete_ids={"b"},
        done_context={"a": PriorSubtaskContext(subtask_id="a")},
        repair_attempts=1,
        repair_pass=repair_pass,
    )

    assert isinstance(result, SubstanceRepairResult)
    assert result.repaired_ids == ("b",)
    assert result.still_incomplete_ids == ()
    assert result.all_repaired is True
    assert result.attempts_used == 1
    # Only the incomplete node was scheduled; the done dep was context only.
    assert seen_plans == [("b",)]
    assert seen_prior == [{"a"}]


def test_still_incomplete_after_exhausting_budget() -> None:
    plan = _plan(_st("b"))
    calls = 0

    def repair_pass(repair_plan, prior_results):
        nonlocal calls
        calls += 1
        # ``b`` never closes its criteria.
        return DagRunResult(
            completed=(),
            failed=(),
            receipts=(_receipt("b", "incomplete"),),
        )

    result = run_substance_repair(
        parsed_plan=plan,
        incomplete_ids={"b"},
        done_context={},
        repair_attempts=2,
        repair_pass=repair_pass,
    )

    assert result.repaired_ids == ()
    assert result.still_incomplete_ids == ("b",)
    assert result.all_repaired is False
    assert result.attempts_used == 2  # budget capped the loop
    assert calls == 2


def test_zero_budget_runs_no_pass() -> None:
    plan = _plan(_st("b"))
    calls = 0

    def repair_pass(repair_plan, prior_results):
        nonlocal calls
        calls += 1
        return DagRunResult(completed=(), receipts=(_receipt("b", "done"),))

    result = run_substance_repair(
        parsed_plan=plan,
        incomplete_ids={"b"},
        done_context={},
        repair_attempts=0,
        repair_pass=repair_pass,
    )

    assert calls == 0
    assert result.attempts_used == 0
    assert result.still_incomplete_ids == ("b",)
    assert result.repaired_ids == ()


def test_multi_round_promotes_newly_done_into_context() -> None:
    # Two incomplete nodes; ``y`` depends on ``x``. Round 1 repairs ``x`` only;
    # round 2 should re-run just ``y`` with ``x`` now in the prior context.
    plan = _plan(_st("x"), _st("y", ("x",)))
    rounds: list[tuple[tuple[str, ...], set[str]]] = []

    def repair_pass(repair_plan, prior_results):
        ids = tuple(s.id for s in repair_plan.subtasks)
        rounds.append((ids, set(prior_results)))
        if "x" in ids:  # round 1: x done, y still incomplete
            return DagRunResult(
                completed=(_done_result("x"),),
                receipts=(
                    _receipt("x", "done"),
                    _receipt("y", "incomplete", ("x",)),
                ),
            )
        # round 2: only y scheduled, now done
        return DagRunResult(
            completed=(_done_result("y"),),
            receipts=(_receipt("y", "done", ("x",)),),
        )

    result = run_substance_repair(
        parsed_plan=plan,
        incomplete_ids={"x", "y"},
        done_context={},
        repair_attempts=3,
        repair_pass=repair_pass,
    )

    assert result.attempts_used == 2
    assert set(result.repaired_ids) == {"x", "y"}
    assert result.still_incomplete_ids == ()
    # Round 1 ran both; round 2 ran only y with x promoted into prior context.
    assert rounds[0][0] == ("x", "y")
    assert rounds[1][0] == ("y",)
    assert "x" in rounds[1][1]


def test_negative_budget_rejected() -> None:
    plan = _plan(_st("b"))
    with pytest.raises(ValueError, match="repair_attempts must be ≥0"):
        run_substance_repair(
            parsed_plan=plan,
            incomplete_ids={"b"},
            done_context={},
            repair_attempts=-1,
            repair_pass=lambda p, pr: DagRunResult(completed=()),
        )
