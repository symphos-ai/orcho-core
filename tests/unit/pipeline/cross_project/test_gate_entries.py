"""Tests for ``pipeline.cross_project.gate_entries``."""
from __future__ import annotations

from pipeline.cross_project.gate_entries import (
    child_readiness_contract_entry,
    skipped_contract_entry,
    skipped_release_entry,
)
from pipeline.runtime import CrossGateSkipPolicy


class TestSkippedContractEntry:
    def test_operator_decision_with_feedback(self) -> None:
        e = skipped_contract_entry(
            alias="api",
            reason="operator_decision",
            source="operator",
            on_skip=CrossGateSkipPolicy.ALLOW_WITH_GAP,
            operator_feedback="Tiny docs-only change.",
        )
        assert e["approved"] is False
        assert e["verdict"] == "SKIPPED"
        assert e["skipped"] is True
        assert e["skip_reason"] == "operator_decision"
        assert e["source"] == "operator"
        assert e["on_skip"] == "allow_with_gap"
        assert e["operator_feedback"] == "Tiny docs-only change."
        assert e["findings"] == []
        assert e["risks"] == []
        assert e["checks"] == []

    def test_policy_never_no_feedback_field(self) -> None:
        e = skipped_contract_entry(
            alias="web",
            reason="policy_never",
            source="policy",
            on_skip=CrossGateSkipPolicy.BLOCK,
        )
        assert e["skip_reason"] == "policy_never"
        assert e["source"] == "policy"
        # Empty feedback omits the field entirely.
        assert "operator_feedback" not in e

    def test_never_sets_approved_true(self) -> None:
        for reason, source, on_skip in [
            ("policy_disabled", "policy", CrossGateSkipPolicy.BLOCK),
            ("policy_never", "policy", CrossGateSkipPolicy.ALLOW),
            ("operator_decision", "operator", CrossGateSkipPolicy.ALLOW_WITH_GAP),
        ]:
            e = skipped_contract_entry(
                alias="x", reason=reason, source=source, on_skip=on_skip,
            )
            assert e["approved"] is False


def test_child_readiness_entry_is_not_a_policy_skip() -> None:
    entry = child_readiness_contract_entry(
        alias="core",
        child_status="halted",
        child_reason="operator requested stop",
    )
    assert entry == {
        "approved": False,
        "verdict": "NOT_EVALUABLE",
        "not_evaluable": True,
        "source": "precondition",
        "reason": "child_readiness",
        "child_status": "halted",
        "child_reason": "operator requested stop",
        "short_summary": (
            "contract_check not evaluable for [core]: child is halted "
            "(operator requested stop)."
        ),
        "findings": [],
        "risks": [],
        "checks": [],
    }
    assert "skipped" not in entry
    assert "on_skip" not in entry


class TestSkippedReleaseEntry:
    def test_policy_disabled_shape(self) -> None:
        e = skipped_release_entry(
            reason="policy_disabled", source="policy",
        )
        assert e["approved"] is False
        assert e["verdict"] == "SKIPPED"
        assert e["ship_ready"] is False
        assert e["skipped"] is True
        assert e["skip_reason"] == "policy_disabled"
        assert e["source"] == "policy"
        assert e["release_blockers"] == []
        assert e["verification_gaps"] == []
        cs = e["contract_status"]
        assert set(cs) == {
            "task_contract", "interfaces", "persistence", "tests",
        }
        assert all(v == "not_applicable" for v in cs.values())
