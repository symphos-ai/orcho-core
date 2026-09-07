"""Rejected-release override marker attribution by delivery provenance (ADR 0191)."""
from __future__ import annotations

from pipeline.run_state.terminal_outcome import resolve_rejected_release_terminal


def _settle(provenance: str) -> dict:
    session = {"status": "done"}
    resolve_rejected_release_terminal(
        session,
        rejected=True,
        delivery_status="committed",
        verdict="REJECTED",
        blockers=[],
        short_summary="looks fine",
        delivery_provenance=provenance,
    )
    return session


def test_engine_resolved_override_shape_is_unchanged() -> None:
    session = _settle("")
    assert session["status"] == "done"
    override = session["delivery_override"]
    assert "provenance" not in override
    assert override["message"].startswith("Operator override")


def test_resume_adopted_override_keeps_the_operator_wording_and_stamps_provenance() -> None:
    override = _settle("resume_adopted")["delivery_override"]
    assert override["provenance"] == "resume_adopted"
    assert override["message"].startswith("Operator override")


def test_reconciled_delivery_is_never_called_an_operator_override() -> None:
    session = _settle("reconciled")
    assert session["status"] == "done"
    override = session["delivery_override"]
    assert override["provenance"] == "reconciled"
    assert "Operator override" not in override["message"]
    assert "reconciliation" in override["message"]
    assert "not an approval" in override["message"]
    assert override["reason"] == "final_acceptance_rejected_override"
