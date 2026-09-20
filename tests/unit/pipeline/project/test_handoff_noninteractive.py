# SPDX-License-Identifier: Apache-2.0
"""Unit tests for unattended phase-handoff policy."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline.project.handoff_noninteractive import (
    UNATTENDED_HALT_REASON,
    resolve_unattended_handoff,
)


def _signal(**overrides):
    fields = {
        "handoff_id": "h1",
        "phase": "validate_plan",
        "trigger": "rejected",
        "available_actions": ("continue", "retry_feedback", "halt"),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_advisory_handoff_continues_with_policy_note() -> None:
    resolution = resolve_unattended_handoff(
        _signal(),
        ci_stop_state="budget_exhausted",
        ci_stop_reason="budget_exhausted",
    )

    assert resolution.kind == "continue"
    assert resolution.reason == "advisory_continue"
    assert "auto-decided by unattended policy" in resolution.note
    assert "ci_stop=budget_exhausted:budget_exhausted" in resolution.note


@pytest.mark.parametrize(
    ("signal", "reason"),
    [
        (_signal(trigger="scope_expansion:out_of_plan"), "scope_expansion"),
        (_signal(phase="implement"), "implement_handoff"),
        (_signal(available_actions=("retry_feedback", "halt")), "continue_unavailable"),
    ],
)
def test_authoritative_or_unsafe_handoffs_halt(signal, reason) -> None:
    resolution = resolve_unattended_handoff(signal)

    assert UNATTENDED_HALT_REASON == "phase_handoff_unattended_halt"
    assert resolution.kind == "halt"
    assert resolution.reason == reason
    assert "auto-halted by unattended policy" in resolution.note


def test_verification_rerun_menu_still_halts_unattended() -> None:
    """``retry_verification`` is an operator action: it presumes somebody
    repaired the environment. Unattended policy must never pick it — with no
    ``continue`` on the menu the run halts exactly as before."""
    resolution = resolve_unattended_handoff(
        _signal(
            trigger="verification_gate_failed",
            available_actions=(
                "retry_verification", "continue_with_waiver", "halt",
            ),
        ),
    )

    assert resolution.kind == "halt"
    assert resolution.reason == "continue_unavailable"
    assert "retry_verification" not in resolution.note
