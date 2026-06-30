"""Direct unit tests for :mod:`pipeline.control.reviewed_loop`.

The primitive must stay strictly inside its contract — budgeted rounds
+ verdict classifier + exhaustion-pause-or-bypass status. These tests
lock that contract:

* policy validation rejects unworkable combinations at construction;
* round 1 sees ``is_retry=False`` and empty prior critique;
* rounds 2+ see ``is_retry=True`` and the previous round's critique;
* approval stops iteration immediately (no further produce calls);
* exhaustion routes to ``"exhausted_pause"`` or ``"exhausted_bypass"``
  depending on policy;
* the rounds tuple is accumulated in execution order with all fields
  populated.
"""

from __future__ import annotations

import pytest

from pipeline.control import (
    ReviewedLoopPolicy,
    ReviewedRound,
    ReviewOutcome,
    run_reviewed_loop,
)

# ── policy validation ──────────────────────────────────────────────────────


class TestPolicyValidation:
    def test_max_rounds_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="max_rounds must be >= 1"):
            ReviewedLoopPolicy(
                max_rounds=0,
                pause_on_exhausted_reject=True,
                bypass_on_exhausted_reject=False,
            )

    def test_pause_and_bypass_are_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            ReviewedLoopPolicy(
                max_rounds=3,
                pause_on_exhausted_reject=True,
                bypass_on_exhausted_reject=True,
            )

    def test_one_of_pause_or_bypass_must_be_set(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            ReviewedLoopPolicy(
                max_rounds=3,
                pause_on_exhausted_reject=False,
                bypass_on_exhausted_reject=False,
            )


# ── approval paths ─────────────────────────────────────────────────────────


def _approving_validator(approve_at: int):
    """Return a validate fn that approves on the given round, rejects before."""
    def _validate(round_n: int, _is_retry: bool, output: str) -> ReviewOutcome:
        if round_n >= approve_at:
            return ReviewOutcome(
                approved=True,
                critique="",
                review={"verdict": "APPROVED", "round": round_n},
            )
        return ReviewOutcome(
            approved=False,
            critique=f"reject critique r{round_n}: needs more on {output!r}",
            review={"verdict": "REJECTED", "round": round_n},
        )
    return _validate


class TestApproval:
    def test_approves_on_round_1_emits_single_round(self) -> None:
        produce_calls: list[tuple[int, bool, str]] = []

        def _produce(round_n, is_retry, prior_critique):
            produce_calls.append((round_n, is_retry, prior_critique))
            return f"plan-r{round_n}"

        result = run_reviewed_loop(
            policy=ReviewedLoopPolicy(
                max_rounds=3,
                pause_on_exhausted_reject=True,
                bypass_on_exhausted_reject=False,
            ),
            produce=_produce,
            validate=_approving_validator(approve_at=1),
        )

        assert result.status == "approved"
        assert len(result.rounds) == 1
        assert produce_calls == [(1, False, "")]
        assert result.rounds[0] == ReviewedRound(
            round_n=1,
            is_retry=False,
            output="plan-r1",
            approved=True,
            critique="",
            review={"verdict": "APPROVED", "round": 1},
        )
        assert result.last_output == "plan-r1"

    def test_reject_then_approve_threads_critique_forward(self) -> None:
        produce_calls: list[tuple[int, bool, str]] = []

        def _produce(round_n, is_retry, prior_critique):
            produce_calls.append((round_n, is_retry, prior_critique))
            return f"plan-r{round_n}"

        result = run_reviewed_loop(
            policy=ReviewedLoopPolicy(
                max_rounds=3,
                pause_on_exhausted_reject=True,
                bypass_on_exhausted_reject=False,
            ),
            produce=_produce,
            validate=_approving_validator(approve_at=2),
        )

        assert result.status == "approved"
        assert len(result.rounds) == 2
        # Round 1: produce called with empty critique, is_retry=False.
        # Round 2: is_retry=True, prior_critique threaded from r1.
        assert produce_calls[0] == (1, False, "")
        assert produce_calls[1][0] == 2
        assert produce_calls[1][1] is True
        assert "reject critique r1" in produce_calls[1][2]
        # Round entries reflect execution order, approval on r2.
        assert result.rounds[0].approved is False
        assert result.rounds[1].approved is True
        assert result.rounds[0].is_retry is False
        assert result.rounds[1].is_retry is True
        assert result.last_review == {"verdict": "APPROVED", "round": 2}

    def test_approval_short_circuits_remaining_budget(self) -> None:
        """Once a round approves, ``produce`` must not be called again."""
        produce_count = 0

        def _produce(_round_n, _is_retry, _prior):
            nonlocal produce_count
            produce_count += 1
            return "x"

        run_reviewed_loop(
            policy=ReviewedLoopPolicy(
                max_rounds=5,
                pause_on_exhausted_reject=True,
                bypass_on_exhausted_reject=False,
            ),
            produce=_produce,
            validate=_approving_validator(approve_at=2),
        )

        assert produce_count == 2  # not 5


# ── exhaustion paths ───────────────────────────────────────────────────────


def _always_rejecting_validator(round_n: int, _is_retry: bool, _output: str) -> ReviewOutcome:
    return ReviewOutcome(
        approved=False,
        critique=f"reject r{round_n}",
        review={"verdict": "REJECTED", "round": round_n},
    )


class TestExhaustion:
    def test_exhausted_with_pause_policy_returns_exhausted_pause(self) -> None:
        result = run_reviewed_loop(
            policy=ReviewedLoopPolicy(
                max_rounds=3,
                pause_on_exhausted_reject=True,
                bypass_on_exhausted_reject=False,
            ),
            produce=lambda r, _retry, _c: f"plan-r{r}",
            validate=_always_rejecting_validator,
        )

        assert result.status == "exhausted_pause"
        assert len(result.rounds) == 3
        assert all(r.approved is False for r in result.rounds)
        assert result.last_output == "plan-r3"
        assert result.last_critique == "reject r3"
        assert result.last_review == {"verdict": "REJECTED", "round": 3}

    def test_exhausted_with_bypass_policy_returns_exhausted_bypass(self) -> None:
        result = run_reviewed_loop(
            policy=ReviewedLoopPolicy(
                max_rounds=2,
                pause_on_exhausted_reject=False,
                bypass_on_exhausted_reject=True,
            ),
            produce=lambda r, _retry, _c: f"plan-r{r}",
            validate=_always_rejecting_validator,
        )

        assert result.status == "exhausted_bypass"
        assert len(result.rounds) == 2

    def test_single_round_budget_rejects_to_exhaustion(self) -> None:
        """max_rounds=1 reduces the loop to a single produce+validate;
        rejection terminates with the exhaustion status immediately."""
        result = run_reviewed_loop(
            policy=ReviewedLoopPolicy(
                max_rounds=1,
                pause_on_exhausted_reject=True,
                bypass_on_exhausted_reject=False,
            ),
            produce=lambda r, _retry, _c: "single",
            validate=_always_rejecting_validator,
        )

        assert result.status == "exhausted_pause"
        assert len(result.rounds) == 1
        assert result.rounds[0].is_retry is False


# ── observer-free invariant ────────────────────────────────────────────────


def test_primitive_has_no_observer_hooks() -> None:
    """ADR 0040 guardrail: the primitive must NOT grow display /
    persistence / event ports. Locking the keyword-only signature here
    so a future change that adds ``on_round_complete`` / ``ports`` /
    similar trips this test and forces re-review.
    """
    import inspect
    sig = inspect.signature(run_reviewed_loop)
    # Keyword-only, no var-keyword. Exactly three parameters.
    params = sig.parameters
    assert set(params) == {"policy", "produce", "validate"}
    for p in params.values():
        assert p.kind is inspect.Parameter.KEYWORD_ONLY
