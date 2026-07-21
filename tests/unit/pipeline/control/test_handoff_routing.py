from __future__ import annotations

import pytest

from pipeline.control.handoff_routing import GateIdentity, classify_handoff_route


def _active(*, phase: str, trigger: str, artifacts: dict | None = None) -> dict:
    return {"phase": phase, "trigger": trigger, "artifacts": artifacts or {}}


def test_verification_trigger_wins_over_final_acceptance_phase() -> None:
    result = classify_handoff_route(_active(
        phase="final_acceptance", trigger="verification_gate_failed",
        artifacts={"gate_identity": {
            "command": "pytest-unit", "hook": "before_delivery", "phase": "",
        }},
    ))
    assert result.route == "verification_retry"
    assert result.gate_identity == GateIdentity("pytest-unit", "before_delivery", "")


@pytest.mark.parametrize(("active", "expected"), [
    (_active(phase="final_acceptance", trigger="scope_expansion:out_of_plan"), "scope_expansion"),
    (_active(phase="implement", trigger="incomplete"), "implement_incomplete"),
    (_active(phase="review_changes", trigger="reject"), "review_retry"),
    (_active(phase="validate_plan", trigger="reject"), "plan_retry"),
])
def test_neighbor_handoff_matrix(active: dict, expected: str) -> None:
    assert classify_handoff_route(active).route == expected


def test_ambiguous_legacy_verification_identity_is_blocked() -> None:
    result = classify_handoff_route(
        _active(
            phase="implement", trigger="verification_gate_failed",
            artifacts={"gate_command": "pytest"},
        ),
        ledger_identities=(
            GateIdentity("pytest", "after_phase", "implement"),
            GateIdentity("pytest", "before_delivery", ""),
        ),
    )
    assert result.route == "blocked"
    assert "ambiguous" in (result.blocker or "")


def test_legacy_verification_identity_recovers_from_one_ledger_row() -> None:
    result = classify_handoff_route(
        _active(
            phase="final_acceptance", trigger="verification_gate_failed",
            artifacts={"gate_command": "pytest-unit"},
        ),
        ledger_identities=(GateIdentity("pytest-unit", "after_phase", "implement"),),
    )
    assert result.route == "verification_retry"
    assert result.gate_identity == GateIdentity("pytest-unit", "after_phase", "implement")
