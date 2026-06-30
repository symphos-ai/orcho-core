"""Unit tests for the phase-outcome checkpoint-success classifier."""
from __future__ import annotations

import pytest

from pipeline.run_state import is_phase_checkpoint_success
from pipeline.run_state.phase_outcome import (
    is_phase_checkpoint_success as direct_import,
)


def test_public_export_matches_module_symbol() -> None:
    assert is_phase_checkpoint_success is direct_import


@pytest.mark.parametrize(
    "outcome",
    [
        "ok",
        "OK",
        "  ok  ",
        "skipped",
        "Skipped",
        "skipped: review clean",
        "skipped: completed earlier in this run (resumed)",
        "SKIPPED: completed earlier in this run (resumed)",
        "  skipped: nothing to do  ",
    ],
)
def test_success_outcomes_return_true(outcome: str) -> None:
    assert is_phase_checkpoint_success(outcome) is True


@pytest.mark.parametrize(
    "outcome",
    [
        "halted: delivery produced no diff",
        "halted: halt",
        "HALTED: operator stop",
        "failed",
        "rejected",
        "error",
        "incomplete",
        "no_verdict",
        "handoff_required",
        "operator_handoff_required",
        "DONE",
        "done",
        "direction validated",
        "all rejected",
        "approved",
        "some unknown future token",
        "okayish",  # only exact "ok" counts, not a prefix match
        "skip",  # must start with the full "skipped"
        None,
        "",
        "   ",
    ],
)
def test_non_success_outcomes_return_false(outcome: str | None) -> None:
    assert is_phase_checkpoint_success(outcome) is False
