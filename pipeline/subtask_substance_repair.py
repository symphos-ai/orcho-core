"""
pipeline/subtask_substance_repair.py — bounded auto-repair of INCOMPLETE
subtask_dag deliverables (ADR 0073).

When a ``subtask_dag`` implement run finishes with one or more subtasks in the
``incomplete`` state (the invocation succeeded but the typed done-criteria
attestation was missing / malformed / mismatched / not-all-met), the implement
phase-handoff policy may grant a bounded budget of automatic *substance repair*
rounds before it pauses for an operator or records an auto-waiver.

This module owns that repair logic so it does NOT bloat the already-large
``pipeline/phases/builtin/subtask_dag.py``. It is deliberately thin and pure:
it builds a *filtered* repair DAG (only the incomplete ids; every already-done
dependency stays as read-only context) on top of the T4 seams
(:func:`pipeline.plan_parser.topological_waves` with ``satisfied_ids``,
:func:`pipeline.dag_runner.run_dag_sequential` with ``prior_results``, and
:class:`pipeline.dag_runner.PriorSubtaskContext`), then drives it for at most
``repair_attempts`` rounds. The actual DAG execution is dependency-injected as a
``repair_pass`` callable, so the repair policy is unit-testable without real
agents, sessions, or a working tree.

Done subtasks are never re-invoked or re-mutated: they are handed to each pass
as ``prior_results`` context only, and a subtask that becomes done mid-repair is
promoted to that context for subsequent rounds (with its live output when the
same-process result is available, degrading to a receipt-derived
:class:`PriorSubtaskContext` otherwise).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace

from agents.entities import SubTask
from pipeline.dag_runner import (
    DagRunResult,
    ImplementationReceipt,
    PriorSubtaskContext,
    SubTaskResult,
)
from pipeline.plan_parser import ParsedPlan

#: A bound repair executor: given a filtered plan (incomplete subtasks only) and
#: the prior-result context for its satisfied dependencies, run one DAG pass and
#: return its outcome. Production binds this to
#: :func:`pipeline.dag_runner.run_dag_sequential`; tests inject a fake.
RepairPass = Callable[
    [ParsedPlan, "dict[str, SubTaskResult | PriorSubtaskContext]"],
    DagRunResult,
]

#: A dependency context value the repair DAG treats as already-satisfied.
PriorContext = SubTaskResult | PriorSubtaskContext


@dataclass(frozen=True)
class SubstanceRepairResult:
    """Outcome of a bounded substance-repair run.

    ``repaired_ids`` are incomplete subtasks that reached ``done`` during
    repair; ``still_incomplete_ids`` are those that did not (whether they
    stayed incomplete or hit a hard failure). ``attempts_used`` is the number
    of repair passes actually executed (``0`` when the budget was ``0`` or
    there was nothing to repair). ``receipts`` are the receipts from the final
    pass executed (empty when no pass ran).
    """
    repaired_ids: tuple[str, ...] = ()
    still_incomplete_ids: tuple[str, ...] = ()
    attempts_used: int = 0
    receipts: tuple[ImplementationReceipt, ...] = field(default_factory=tuple)

    @property
    def all_repaired(self) -> bool:
        """True iff no incomplete subtask remains after repair."""
        return not self.still_incomplete_ids


def build_repair_plan(
    parsed_plan: ParsedPlan,
    incomplete_ids: Iterable[str],
) -> ParsedPlan:
    """Project ``parsed_plan`` down to just its INCOMPLETE subtasks.

    The returned plan keeps every other field of ``parsed_plan`` (goal,
    acceptance_criteria, …) so the repair agents still see the full contract;
    only ``subtasks`` is filtered to the incomplete ids, preserving their
    original order. An incomplete subtask's ``depends_on`` may reference a
    done id that is no longer in the plan — that is intentional: the done
    dependency is supplied to :func:`pipeline.dag_runner.run_dag_sequential`
    as ``prior_results`` (a satisfied id), so the seam schedules the node and
    renders the dependency's context without re-running it. The filtered plan
    is therefore NOT re-validated (``validate_dag`` ran at parse time).
    """
    wanted = set(incomplete_ids)
    subs = tuple(s for s in parsed_plan.subtasks if s.id in wanted)
    return replace(parsed_plan, subtasks=subs)


def run_substance_repair(
    *,
    parsed_plan: ParsedPlan,
    incomplete_ids: Iterable[str],
    done_context: Mapping[str, PriorContext],
    repair_attempts: int,
    repair_pass: RepairPass,
) -> SubstanceRepairResult:
    """Drive bounded substance repair of ``incomplete_ids``.

    Each round re-runs only the still-incomplete subtasks (a filtered repair
    DAG built via :func:`build_repair_plan`), handing every done dependency in
    as read-only ``prior_results`` context. A subtask that reaches ``done`` is
    promoted to that context — using its live :class:`SubTaskResult` when this
    same-process pass produced one, else a degraded
    :class:`PriorSubtaskContext` — so a later round's still-incomplete
    dependents still see it without it being re-invoked. The loop stops as soon
    as nothing incomplete remains or the ``repair_attempts`` budget is spent.

    ``repair_attempts`` is the hard ceiling on passes; ``0`` runs no repair at
    all (every incomplete id is returned as still-incomplete). The injected
    ``repair_pass`` performs the real execution, keeping this function free of
    agent/session/filesystem side effects.
    """
    if repair_attempts < 0:
        raise ValueError(f"repair_attempts must be ≥0, got {repair_attempts}")

    wanted = set(incomplete_ids)
    # Preserve plan order; ignore ids that aren't real subtasks.
    remaining: list[SubTask] = [
        s for s in parsed_plan.subtasks if s.id in wanted
    ]

    prior: dict[str, PriorContext] = dict(done_context)
    repaired: list[str] = []
    attempts_used = 0
    last_receipts: tuple[ImplementationReceipt, ...] = ()

    while remaining and attempts_used < repair_attempts:
        attempts_used += 1
        repair_plan = replace(parsed_plan, subtasks=tuple(remaining))
        result = repair_pass(repair_plan, dict(prior))
        last_receipts = result.receipts

        receipt_state = {r.subtask_id: r.state for r in result.receipts}
        receipt_by_id = {r.subtask_id: r for r in result.receipts}
        completed_by_id = {r.subtask_id: r for r in result.completed}

        next_remaining: list[SubTask] = []
        for sub in remaining:
            if receipt_state.get(sub.id) == "done":
                repaired.append(sub.id)
                # Promote to context for the next round. Prefer the live
                # result (carries output) over the degraded receipt view.
                live = completed_by_id.get(sub.id)
                if live is not None:
                    prior[sub.id] = live
                elif sub.id in receipt_by_id:
                    prior[sub.id] = PriorSubtaskContext.from_receipt(
                        receipt_by_id[sub.id]
                    )
            else:
                next_remaining.append(sub)
        remaining = next_remaining

    return SubstanceRepairResult(
        repaired_ids=tuple(repaired),
        still_incomplete_ids=tuple(s.id for s in remaining),
        attempts_used=attempts_used,
        receipts=last_receipts,
    )
