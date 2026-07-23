"""Pure durable delivery projection contracts for terminal rendering."""

from __future__ import annotations

import pytest

from pipeline.project.terminal_delivery import (
    TerminalDeliveryDisposition,
    project_terminal_delivery,
)


@pytest.mark.parametrize("status", ["committed", "applied_uncommitted"])
def test_applied_delivery_with_consistent_override_is_operator_override(
    status: str,
) -> None:
    outcome = project_terminal_delivery({
        "commit_delivery": {"status": status},
        "delivery_override": {
            "reason": "final_acceptance_rejected_override",
            "status": "done",
            "release_verdict": "REJECTED",
            "delivery_status": status,
        },
    })

    assert outcome.disposition is TerminalDeliveryDisposition.DELIVERED_BY_OPERATOR_OVERRIDE
    assert outcome.status == status


@pytest.mark.parametrize("status", ["committed", "applied_uncommitted"])
def test_applied_delivery_without_consistent_override_is_delivered(status: str) -> None:
    outcome = project_terminal_delivery({
        "commit_delivery": {"status": status},
        "delivery_override": {
            "reason": "final_acceptance_rejected_override",
            "status": "done",
            "release_verdict": "REJECTED",
            "delivery_status": "committed" if status == "applied_uncommitted" else "other",
        },
    })

    assert outcome.disposition is TerminalDeliveryDisposition.DELIVERED
    assert outcome.status == status


@pytest.mark.parametrize(
    "status",
    ["disabled", "not_applicable", "no_diff", "pending", "fix_requested", "skipped", "halted", "commit_failed", "apply_failed", "target_dirty", "verification_blocked"],
)
def test_canonical_non_delivery_status_is_not_delivered(status: str) -> None:
    outcome = project_terminal_delivery({"commit_delivery": {"status": status}})

    assert outcome.disposition is TerminalDeliveryDisposition.NOT_DELIVERED
    assert outcome.status == status


@pytest.mark.parametrize("session", [{}, {"commit_delivery": {}}, {"commit_delivery": {"status": "new_status"}}])
def test_absent_or_unknown_delivery_status_is_unknown(session: dict) -> None:
    outcome = project_terminal_delivery(session)

    assert outcome.disposition is TerminalDeliveryDisposition.UNKNOWN
    assert outcome.status is None
