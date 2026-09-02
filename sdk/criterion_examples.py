"""Versioned criterion-matrix conformance examples (ADR 0188).

These live in the **installed package**, not in ``orcho-core/tests/``, so a
downstream consumer (notably the MCP server) can assert byte-equivalence
against the same canonical JSON core produces without reaching into a source
checkout.

Every example is built by the real reducer and the real durable value objects,
so an example cannot drift away from the implementation it documents.

Bump :data:`CRITERION_EXAMPLES_VERSION` whenever an example's canonical JSON
changes; consumers pin it.
"""
from __future__ import annotations

from typing import Any

from core.contracts.criteria import AcceptanceCriterion, GateRef
from pipeline.criterion_decisions import HumanDecision
from pipeline.criterion_matrix import (
    CriterionClaim,
    HumanDecisionFact,
    build_criterion_matrix,
    empty_criterion_matrix,
)

__all__ = [
    "CRITERION_EXAMPLES_VERSION",
    "EXAMPLE_NAMES",
    "criterion_matrix_example",
    "human_decision_chain_example",
]

CRITERION_EXAMPLES_VERSION = "1"

#: Every conformance case, in a stable order.
EXAMPLE_NAMES: tuple[str, ...] = (
    "three_class",
    "multi_gate",
    "explicit_empty",
    "absent_matrix",
    "mixed_state",
)

_UNIT = GateRef("unit", "after_phase", "implement")
_LINT = GateRef("lint", "after_phase", "implement")
_SMOKE = GateRef("smoke", "before_delivery", "")


def _three_class() -> dict[str, Any]:
    criteria = (
        AcceptanceCriterion(
            id="C1",
            intent="The changed behavior is regression-tested",
            verify="executable",
            gate_refs=(_UNIT,),
        ),
        AcceptanceCriterion(
            id="C2",
            intent="The public explanation is understandable without internal context",
            verify="agent_assertion",
        ),
        AcceptanceCriterion(
            id="C3",
            intent="The end-to-end interaction is acceptable to an operator",
            verify="human",
            human_instructions="Exercise the journey and record accept or reject.",
        ),
    )
    return build_criterion_matrix(
        criteria,
        executors_by_criterion={"C1": ("task-2",)},
        gate_states={_UNIT.identity: "proven"},
        gate_proof_refs={_UNIT.identity: "receipt-17"},
        claims=(CriterionClaim(criterion_id="C2", id="finding-3", kind="finding"),),
    ).to_dict()


def _multi_gate() -> dict[str, Any]:
    criteria = (
        AcceptanceCriterion(
            id="C1",
            intent="Every declared gate proves the change",
            verify="executable",
            gate_refs=(_UNIT, _LINT, _SMOKE),
        ),
    )
    return build_criterion_matrix(
        criteria,
        executors_by_criterion={"C1": ("task-1", "task-2")},
        gate_states={
            _UNIT.identity: "proven",
            _LINT.identity: "failed",
            _SMOKE.identity: "stale",
        },
        gate_proof_refs={
            _UNIT.identity: "receipt-unit",
            _LINT.identity: "receipt-lint",
        },
    ).to_dict()


def _mixed_state() -> dict[str, Any]:
    """All three verification classes with states from all three groups.

    Deliberately covers a failing executable row, an advisory agent row, an
    accepted human row, and a pending human row so a consumer can assert the
    canonical ``counts_by_state`` ordering rather than an alphabetical or
    insertion order that happens to agree on a smaller sample.
    """
    criteria = (
        AcceptanceCriterion(
            id="C1",
            intent="The regression suite proves the change",
            verify="executable",
            gate_refs=(_UNIT,),
        ),
        AcceptanceCriterion(
            id="C2",
            intent="The lint gate proves repository hygiene",
            verify="executable",
            gate_refs=(_LINT,),
        ),
        AcceptanceCriterion(
            id="C3",
            intent="The documentation reads coherently",
            verify="agent_assertion",
        ),
        AcceptanceCriterion(
            id="C4",
            intent="The operator accepts the migration journey",
            verify="human",
            human_instructions="Run the migration once and record the outcome.",
        ),
        AcceptanceCriterion(
            id="C5",
            intent="The operator accepts the rollback journey",
            verify="human",
            human_instructions="Roll the migration back and record the outcome.",
        ),
    )
    return build_criterion_matrix(
        criteria,
        executors_by_criterion={"C1": ("task-1",), "C2": ("task-2",)},
        gate_states={
            _UNIT.identity: "proven",
            _LINT.identity: "failed",
        },
        gate_proof_refs={
            _UNIT.identity: "receipt-unit",
            _LINT.identity: "receipt-lint",
        },
        claims=(CriterionClaim(criterion_id="C3", id="claim-1", kind="claim"),),
        human_decisions={
            "C4": HumanDecisionFact(
                criterion_id="C4", decision_id="hd-C4-1", decision="accept",
            ),
        },
    ).to_dict()


_BUILDERS = {
    "three_class": _three_class,
    "multi_gate": _multi_gate,
    "explicit_empty": lambda: empty_criterion_matrix().to_dict(),
    "absent_matrix": lambda: None,
    "mixed_state": _mixed_state,
}


def criterion_matrix_example(name: str) -> dict[str, Any] | None:
    """Return one conformance example.

    ``"absent_matrix"`` returns ``None`` — the legacy-bundle case, which is
    meaningfully different from ``"explicit_empty"``.
    """
    try:
        builder = _BUILDERS[name]
    except KeyError:
        raise ValueError(
            f"unknown criterion example {name!r}; known: {list(EXAMPLE_NAMES)}"
        ) from None
    return builder()


def human_decision_chain_example() -> list[dict[str, Any]]:
    """A valid append-only supersession chain in durable write order.

    The first record omits ``supersedes``; the replacement names the previous
    head; unused optional keys are absent, never ``null``. Only the last record
    is the head, and it is the ``decision_id`` a matrix ``proof_ref`` cites.
    """
    return [
        HumanDecision(
            decision_id="hd-C3-1",
            run_id="20260101_000000",
            criterion_id="C3",
            decision="reject",
            recorded_at="2026-01-01T00:00:00Z",
            note="The empty state is unreadable.",
            actor="operator",
        ).to_dict(),
        HumanDecision(
            decision_id="hd-C3-2",
            run_id="20260101_000000",
            criterion_id="C3",
            decision="accept",
            recorded_at="2026-01-01T00:05:00.500000Z",
            supersedes="hd-C3-1",
        ).to_dict(),
    ]
