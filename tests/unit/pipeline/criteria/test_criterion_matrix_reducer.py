# SPDX-License-Identifier: Apache-2.0
"""T4 — the pure criterion reducer: state algebra, precedence, ordering.

Covers C5, C7 and the F1 canonical-state-order revision of ADR 0188.
"""
from __future__ import annotations

import pytest

from core.contracts.criteria import AcceptanceCriterion, GateRef
from pipeline.criterion_matrix import (
    CRITERION_STATE_ORDER,
    EXECUTABLE_STATE_PRECEDENCE,
    CriterionClaim,
    HumanDecisionFact,
    build_criterion_matrix,
    empty_criterion_matrix,
    gate_state_from_disposition,
)

UNIT = GateRef("unit", "after_phase", "implement")
LINT = GateRef("lint", "after_phase", "implement")


def _executable(cid="C1", refs=(UNIT,)):
    return AcceptanceCriterion(cid, f"intent {cid}", "executable", gate_refs=refs)


class TestCanonicalOrders:
    def test_serialization_order_is_the_declared_constant(self) -> None:
        assert CRITERION_STATE_ORDER == (
            "proven", "failed", "stale", "missing", "not_selected",
            "advisory", "accepted", "rejected", "pending",
        )

    def test_precedence_is_a_different_axis_from_serialization(self) -> None:
        assert EXECUTABLE_STATE_PRECEDENCE == (
            "failed", "stale", "missing", "not_selected", "proven",
        )
        assert CRITERION_STATE_ORDER[:5] != EXECUTABLE_STATE_PRECEDENCE

    def test_counts_by_state_follows_the_canonical_order(self) -> None:
        criteria = (
            _executable("C1"),
            _executable("C2", (LINT,)),
            AcceptanceCriterion("C3", "i", "agent_assertion"),
            AcceptanceCriterion("C4", "i", "human", human_instructions="do"),
            AcceptanceCriterion("C5", "i", "human", human_instructions="do"),
        )
        matrix = build_criterion_matrix(
            criteria,
            gate_states={UNIT.identity: "proven", LINT.identity: "failed"},
            gate_proof_refs={UNIT.identity: "receipt-unit"},
            claims=(CriterionClaim("C3", "claim-1"),),
            human_decisions={
                "C4": HumanDecisionFact("C4", "hd-C4-1", "accept"),
            },
        )
        counts = matrix.to_dict()["summary"]["counts_by_state"]
        assert list(counts) == ["proven", "failed", "advisory", "accepted", "pending"]
        # Insertion order alone would have produced the plan order, which is
        # the same here; assert against the canonical constant instead.
        assert list(counts) == [s for s in CRITERION_STATE_ORDER if s in counts]


class TestExecutableAlgebra:
    @pytest.mark.parametrize(
        ("states", "expected"),
        [
            (("proven",), "proven"),
            (("proven", "failed"), "failed"),
            (("proven", "stale"), "stale"),
            (("proven", "missing"), "missing"),
            (("proven", "not_selected"), "not_selected"),
            (("failed", "stale"), "failed"),
            (("stale", "missing"), "stale"),
            (("missing", "not_selected"), "missing"),
        ],
    )
    def test_multi_gate_precedence(self, states, expected) -> None:
        refs = tuple(
            GateRef(f"cmd{i}", "after_phase", "implement")
            for i in range(len(states))
        )
        by_identity = dict(zip((r.identity for r in refs), states, strict=True))
        matrix = build_criterion_matrix(
            (_executable("C1", refs),),
            gate_states=by_identity,
            # Every passing identity carries its canonical receipt, so this
            # exercises precedence rather than the missing-proof downgrade.
            gate_proof_refs={
                identity: f"receipt-{identity[0]}"
                for identity, state in by_identity.items()
                if state == "proven"
            },
        )
        row = matrix.rows[0]
        assert row.state == expected
        assert row.blocking is (expected != "proven")

    def test_a_passing_gate_without_a_receipt_is_not_proof(self) -> None:
        """ADR 0188 §3: ``proven`` is a claim about proof, so a passing
        classification with no canonical receipt behind it stays blocking."""
        matrix = build_criterion_matrix(
            (_executable(),), gate_states={UNIT.identity: "proven"},
        )
        row = matrix.rows[0]
        assert row.state == "missing"
        assert row.blocking is True
        assert row.proof_refs == ()
        assert "not proof" in row.reason
        assert matrix.summary.ready is False

    def test_one_receipted_gate_does_not_carry_an_unreceipted_sibling(
        self,
    ) -> None:
        matrix = build_criterion_matrix(
            (_executable("C1", (UNIT, LINT)),),
            gate_states={UNIT.identity: "proven", LINT.identity: "proven"},
            gate_proof_refs={UNIT.identity: "receipt-unit"},
        )
        row = matrix.rows[0]
        assert row.state == "missing"
        assert row.blocking is True
        # The receipt that does exist is still retained as a proof reference.
        assert [r.to_dict() for r in row.proof_refs] == [
            {"kind": "receipt", "id": "receipt-unit"},
        ]
        assert "lint @ after_phase implement" in row.reason

    def test_every_receipted_passing_gate_proves_the_row(self) -> None:
        matrix = build_criterion_matrix(
            (_executable("C1", (UNIT, LINT)),),
            gate_states={UNIT.identity: "proven", LINT.identity: "proven"},
            gate_proof_refs={
                UNIT.identity: "receipt-unit", LINT.identity: "receipt-lint",
            },
        )
        row = matrix.rows[0]
        assert row.state == "proven"
        assert row.blocking is False
        assert row.reason == ""
        assert len(row.proof_refs) == 2

    def test_an_empty_receipt_id_does_not_count_as_proof(self) -> None:
        matrix = build_criterion_matrix(
            (_executable(),),
            gate_states={UNIT.identity: "proven"},
            gate_proof_refs={UNIT.identity: ""},
        )
        assert matrix.rows[0].state == "missing"
        assert matrix.rows[0].proof_refs == ()

    def test_an_identity_with_no_canonical_fact_is_missing(self) -> None:
        matrix = build_criterion_matrix((_executable(),), gate_states={})
        assert matrix.rows[0].state == "missing"

    def test_receipt_proof_refs_are_retained(self) -> None:
        matrix = build_criterion_matrix(
            (_executable("C1", (UNIT, LINT)),),
            gate_states={UNIT.identity: "proven", LINT.identity: "failed"},
            gate_proof_refs={
                UNIT.identity: "receipt-1", LINT.identity: "receipt-2",
            },
        )
        assert [r.to_dict() for r in matrix.rows[0].proof_refs] == [
            {"kind": "receipt", "id": "receipt-1"},
            {"kind": "receipt", "id": "receipt-2"},
        ]

    def test_method_is_a_discriminated_gates_object(self) -> None:
        row = build_criterion_matrix((_executable(),)).rows[0].to_dict()
        assert row["method"] == {
            "kind": "gates",
            "gate_refs": [
                {"command": "unit", "hook": "after_phase", "phase": "implement"},
            ],
        }

    @pytest.mark.parametrize(
        ("disposition", "state"),
        [
            ("executed_pass", "proven"),
            ("skipped_fresh", "proven"),
            ("executed_fail", "failed"),
            ("residual_failed", "failed"),
            ("residual_stale", "stale"),
            ("residual_missing", "missing"),
            ("not_selected", "not_selected"),
            ("manual_available", "not_selected"),
            ("suggested", "not_selected"),
            (None, "missing"),
        ],
    )
    def test_canonical_disposition_mapping(self, disposition, state) -> None:
        assert gate_state_from_disposition(disposition) == state


class TestAgentAssertion:
    def test_linked_claim_is_advisory_never_proven(self) -> None:
        matrix = build_criterion_matrix(
            (AcceptanceCriterion("C2", "i", "agent_assertion"),),
            claims=(CriterionClaim("C2", "finding-3", "finding"),),
        )
        row = matrix.rows[0]
        assert row.state == "advisory"
        assert row.blocking is False
        assert row.proof_refs[0].to_dict() == {"kind": "finding", "id": "finding-3"}
        assert matrix.summary.ready is True

    def test_unlinked_assertion_is_pending_and_owned_by_reviewer(self) -> None:
        matrix = build_criterion_matrix(
            (AcceptanceCriterion("C2", "i", "agent_assertion"),),
        )
        assert matrix.rows[0].state == "pending"
        assert matrix.rows[0].executors == ("reviewer",)
        assert matrix.rows[0].method == {"kind": "inspection"}

    def test_a_claim_cannot_manufacture_a_passing_receipt(self) -> None:
        matrix = build_criterion_matrix(
            (_executable(),),
            gate_states={UNIT.identity: "missing"},
            claims=(CriterionClaim("C1", "claim-1"),),
        )
        assert matrix.rows[0].state == "missing"
        assert matrix.rows[0].proof_refs == ()


class TestHuman:
    def test_pending_until_a_typed_decision_exists(self) -> None:
        matrix = build_criterion_matrix(
            (AcceptanceCriterion("C3", "i", "human", human_instructions="do it"),),
        )
        row = matrix.rows[0]
        assert row.state == "pending"
        assert row.blocking is True
        assert row.executors == ("human",)
        assert row.method == {"kind": "manual", "instructions": "do it"}
        assert matrix.summary.pending_human_ids == ("C3",)
        assert matrix.summary.ready is False

    @pytest.mark.parametrize(
        ("decision", "state", "blocking"),
        [("accept", "accepted", False), ("reject", "rejected", True)],
    )
    def test_decision_transitions(self, decision, state, blocking) -> None:
        matrix = build_criterion_matrix(
            (AcceptanceCriterion("C3", "i", "human", human_instructions="do"),),
            human_decisions={"C3": HumanDecisionFact("C3", "hd-C3-1", decision)},
        )
        row = matrix.rows[0]
        assert (row.state, row.blocking) == (state, blocking)
        assert row.proof_refs[0].to_dict() == {
            "kind": "human_decision", "id": "hd-C3-1",
        }


class TestSummary:
    def test_ready_is_exactly_no_blocking_open(self) -> None:
        matrix = build_criterion_matrix(
            (_executable(),),
            gate_states={UNIT.identity: "proven"},
            gate_proof_refs={UNIT.identity: "receipt-unit"},
        )
        assert matrix.summary.blocking_open == 0
        assert matrix.summary.ready is True

    def test_rows_follow_plan_order(self) -> None:
        criteria = tuple(
            AcceptanceCriterion(f"C{i}", "i", "agent_assertion")
            for i in (3, 1, 2)
        )
        matrix = build_criterion_matrix(criteria)
        assert [r.criterion_id for r in matrix.rows] == ["C3", "C1", "C2"]

    def test_explicit_empty_matrix(self) -> None:
        assert empty_criterion_matrix().to_dict() == {
            "rows": [],
            "summary": {
                "total": 0, "blocking_open": 0, "ready": True,
                "counts_by_state": {}, "pending_human_ids": [],
            },
        }
        assert build_criterion_matrix(()).to_dict() == (
            empty_criterion_matrix().to_dict()
        )

    def test_declared_task_owners_win_over_the_class_fallback(self) -> None:
        matrix = build_criterion_matrix(
            (_executable(),),
            executors_by_criterion={"C1": ("task-2", "task-3")},
            gate_states={UNIT.identity: "proven"},
            gate_proof_refs={UNIT.identity: "receipt-unit"},
        )
        assert matrix.rows[0].executors == ("task-2", "task-3")
