"""Unit tests for pipeline.control.handoff_labels.

Pins the operator round-label matrix and the hard invariant that no
surface ever renders an ``N/M`` fraction with ``N > M`` (the historical
``round 2/1`` bug for the one-shot human-directed retry round).
"""

from __future__ import annotations

import re

import pytest

from pipeline.control.handoff_labels import (
    human_directed_flag_from_state,
    is_human_directed_round,
    render_round_label,
)
from pipeline.runtime.handoff import HUMAN_DIRECTED_FLAG_KEY

# Matches a printed ``round N/M`` fraction so a test can assert the
# numerator never exceeds the denominator on any surface.
_FRACTION = re.compile(r"round (\d+)/(\d+)")


def _assert_no_impossible_fraction(label: str) -> None:
    for num, denom in _FRACTION.findall(label):
        assert int(num) <= int(denom), (
            f"impossible fraction in label: {label!r}"
        )


class TestAutomaticRounds:
    def test_first_automatic_reject_review(self) -> None:
        # review_changes rejected on the only automatic round.
        label = render_round_label(
            phase="review_changes", round=1, loop_max_rounds=1,
        )
        assert label == "review_changes automatic round 1/1"
        _assert_no_impossible_fraction(label)

    def test_mid_budget_automatic_round(self) -> None:
        label = render_round_label(
            phase="review_changes", round=2, loop_max_rounds=3,
        )
        assert label == "review_changes automatic round 2/3"
        _assert_no_impossible_fraction(label)

    def test_plan_phase_automatic_label(self) -> None:
        label = render_round_label(
            phase="plan", round=1, loop_max_rounds=2,
        )
        assert label == "plan automatic round 1/2"

    def test_validate_plan_automatic_label(self) -> None:
        label = render_round_label(
            phase="validate_plan", round=2, loop_max_rounds=2,
        )
        assert label == "validate_plan automatic round 2/2"
        _assert_no_impossible_fraction(label)


class TestHumanDirectedRounds:
    def test_human_retry_after_auto_budget_exhausted(self) -> None:
        # max_rounds=1 exhausted; the one-shot human-directed retry is
        # round 2. Must NOT render "round 2/1".
        label = render_round_label(
            phase="review_changes", round=2, loop_max_rounds=1,
        )
        assert label == "review_changes human retry 1 after REJECTED verdict"
        assert "2/1" not in label
        _assert_no_impossible_fraction(label)

    def test_second_handoff_after_retry_reject(self) -> None:
        # The human-directed retry round itself produced another reject
        # and a fresh pause → operator decision required.
        label = render_round_label(
            phase="review_changes",
            round=2,
            loop_max_rounds=1,
            rejected_again=True,
        )
        assert label == (
            "review_changes human retry 1 rejected; operator decision required"
        )
        _assert_no_impossible_fraction(label)

    def test_human_retry_ordinal_increments(self) -> None:
        label = render_round_label(
            phase="review_changes", round=4, loop_max_rounds=2,
        )
        assert label == "review_changes human retry 2 after REJECTED verdict"

    def test_explicit_flag_forces_human_label(self) -> None:
        # Even when round <= loop_max_rounds, an explicit human-directed
        # flag renders the human retry shape (ordinal floored at 1).
        label = render_round_label(
            phase="validate_plan",
            round=1,
            loop_max_rounds=2,
            human_directed=True,
        )
        assert label == "validate_plan human retry 1 after REJECTED verdict"

    def test_plan_human_retry_label(self) -> None:
        label = render_round_label(
            phase="plan", round=2, loop_max_rounds=1,
        )
        assert label == "plan human retry 1 after REJECTED verdict"


class TestImpossibleFractionInvariant:
    @pytest.mark.parametrize("phase", ["plan", "validate_plan", "review_changes", "repair_changes"])
    @pytest.mark.parametrize("loop_max_rounds", [1, 2, 3])
    @pytest.mark.parametrize("over", [1, 2, 5])
    def test_never_renders_numerator_over_denominator(
        self, phase: str, loop_max_rounds: int, over: int,
    ) -> None:
        # Drive the human-directed overflow range (round > max) across
        # phases and budgets; no surface may emit "round N/M" with N>M.
        label = render_round_label(
            phase=phase,
            round=loop_max_rounds + over,
            loop_max_rounds=loop_max_rounds,
        )
        _assert_no_impossible_fraction(label)
        assert "human retry" in label

    @pytest.mark.parametrize("loop_max_rounds", [1, 2, 3])
    def test_automatic_rounds_keep_valid_fraction(
        self, loop_max_rounds: int,
    ) -> None:
        for round_n in range(1, loop_max_rounds + 1):
            label = render_round_label(
                phase="review_changes",
                round=round_n,
                loop_max_rounds=loop_max_rounds,
            )
            assert "human retry" not in label
            _assert_no_impossible_fraction(label)


class TestIsHumanDirectedRound:
    def test_structural_overflow_is_human(self) -> None:
        assert is_human_directed_round(round=2, loop_max_rounds=1) is True

    def test_within_budget_is_automatic(self) -> None:
        assert is_human_directed_round(round=2, loop_max_rounds=2) is False

    def test_explicit_flag_wins(self) -> None:
        assert is_human_directed_round(
            round=1, loop_max_rounds=2, human_directed_flag=True,
        ) is True


class TestHumanDirectedFlagFromState:
    def test_flag_key_set(self) -> None:
        state = _StubState(extras={HUMAN_DIRECTED_FLAG_KEY: True})
        assert human_directed_flag_from_state(state) is True

    def test_retry_feedback_override(self) -> None:
        state = _StubState(
            extras={"phase_handoff_override": {"action": "retry_feedback"}},
        )
        assert human_directed_flag_from_state(state) is True

    def test_continue_override_is_not_human_directed(self) -> None:
        state = _StubState(
            extras={"phase_handoff_override": {"action": "continue"}},
        )
        assert human_directed_flag_from_state(state) is False

    def test_no_extras(self) -> None:
        assert human_directed_flag_from_state(_StubState(extras={})) is False

    def test_non_dict_extras_tolerated(self) -> None:
        assert human_directed_flag_from_state(_StubState(extras=None)) is False


class _StubState:
    def __init__(self, *, extras: object) -> None:
        self.extras = extras
