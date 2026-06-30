"""Budgeted produce → validate retry loop (ADR 0040 Phase C).

The cross-project planning loop (``planning_loop._run_initial_loop``)
and any future caller that drives a "produce N, review, retry if
rejected, pause or bypass when budget exhausted" shape need the same
small control-flow primitive:

* iterate ``round_n`` from 1 up to ``policy.max_rounds``;
* call ``produce(round_n, is_retry, prior_critique) -> output``;
* call ``validate(round_n, is_retry, output) -> ReviewOutcome``;
* on approval → stop, return ``status="approved"``;
* on rejection with budget remaining → carry the critique forward to
  the next produce call;
* on rejection with budget exhausted → return ``"exhausted_pause"``
  or ``"exhausted_bypass"`` per ``policy``.

This module deliberately knows nothing about:

* prompts, agents, runtime sessions;
* checkpoint / session / meta persistence;
* banners, ``log_phase``, ``vdump``, terminal rendering;
* phase-handoff payload builders, ADR 0031 payload shape;
* the single-project runtime FSM (``_run_loop_step``) — that primitive
  operates on ``PhaseStep`` / ``PipelineState`` and is intentionally
  not generalised here.

ADR 0040 forbidden-shape rule: if a future caller needs to thread a
domain-specific port (banner, checkpoint writer, etc.) into this
primitive, that caller stays open-coded against ``produce`` / ``validate``
and the side-effect happens inside its own closures — do NOT add
``DisplayPorts`` / ``PersistencePorts`` / etc. to ``run_reviewed_loop``.
The bar is the same as ``handoff_decisions``: the primitive serves two
distinct callers, same shape, with no caller's domain leaking into the
signature.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

#: Terminal status the loop returns when no further rounds will run.
ReviewedLoopStatus = Literal["approved", "exhausted_pause", "exhausted_bypass"]


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """What ``validate`` reports for a single round.

    ``critique`` carries forward to the next ``produce`` call when the
    loop retries. ``review`` is the structured reviewer-payload dict
    (verdict, findings, etc.) — the loop treats it as opaque and only
    persists it on the round entry.
    """
    approved: bool
    critique: str
    review: dict | None = None


@dataclass(frozen=True, slots=True)
class ReviewedLoopPolicy:
    """Loop budget + behaviour on exhaustion.

    ``pause_on_exhausted_reject`` and ``bypass_on_exhausted_reject`` are
    mutually exclusive; exactly one must be true. The validator below
    rejects ``(True, True)`` and ``(False, False)`` at construction so
    a misconfigured policy fails fast rather than at exhaustion time.
    """
    max_rounds: int
    pause_on_exhausted_reject: bool
    bypass_on_exhausted_reject: bool

    def __post_init__(self) -> None:
        if self.max_rounds < 1:
            raise ValueError(
                f"ReviewedLoopPolicy.max_rounds must be >= 1, "
                f"got {self.max_rounds}"
            )
        if self.pause_on_exhausted_reject == self.bypass_on_exhausted_reject:
            raise ValueError(
                "ReviewedLoopPolicy: exactly one of "
                "pause_on_exhausted_reject / bypass_on_exhausted_reject "
                "must be true (they are mutually exclusive)"
            )


@dataclass(frozen=True, slots=True)
class ReviewedRound:
    """One executed produce → validate cycle. Accumulated into
    ``ReviewedLoopResult.rounds`` in execution order."""
    round_n: int
    is_retry: bool
    output: str
    approved: bool
    critique: str
    review: dict | None


@dataclass(frozen=True, slots=True)
class ReviewedLoopResult:
    """Outcome of :func:`run_reviewed_loop`.

    ``status`` semantics:

    * ``"approved"`` — a round produced an approved verdict; ``rounds``
      ends with the approving round and ``last_review`` is its review.
    * ``"exhausted_pause"`` — every round in the budget was rejected
      and the policy asked to pause for operator decision; caller is
      expected to build a handoff payload, persist it, and return.
    * ``"exhausted_bypass"`` — every round in the budget was rejected
      and the policy asked to bypass (proceed with the last rejected
      output); caller continues as if the loop approved.

    ``last_critique`` is the critique from the final round
    (approving or rejecting). ``last_output`` is the output of that
    round.
    """
    status: ReviewedLoopStatus
    rounds: tuple[ReviewedRound, ...]
    last_output: str
    last_critique: str
    last_review: dict | None


def run_reviewed_loop(
    *,
    policy: ReviewedLoopPolicy,
    produce: Callable[[int, bool, str], str],
    validate: Callable[[int, bool, str], ReviewOutcome],
) -> ReviewedLoopResult:
    """Drive the budgeted produce → validate loop.

    Parameters:

    * ``policy`` — round budget and exhaustion behaviour.
    * ``produce(round_n, is_retry, prior_critique) -> output`` —
      caller-supplied output producer. ``is_retry`` is ``True`` for
      rounds ``> 1`` (the producer can branch between "first attempt"
      and "replan" prompts). ``prior_critique`` is the previous
      round's critique (empty string on round 1).
    * ``validate(round_n, is_retry, output) -> ReviewOutcome`` —
      caller-supplied verdict classifier. Returning
      ``ReviewOutcome(approved=True, ...)`` stops the loop;
      ``approved=False`` consumes one round of budget and feeds the
      critique into the next produce call.

    The loop is intentionally side-effect-free at the primitive layer:
    every banner / log / persistence / event emission belongs in the
    caller's ``produce`` / ``validate`` closures, NOT in this function.
    """
    rounds: list[ReviewedRound] = []
    last_critique = ""
    last_review: dict | None = None
    last_output = ""

    for round_n in range(1, policy.max_rounds + 1):
        is_retry = round_n > 1
        output = produce(round_n, is_retry, last_critique)
        last_output = output
        outcome = validate(round_n, is_retry, output)
        rounds.append(ReviewedRound(
            round_n=round_n,
            is_retry=is_retry,
            output=output,
            approved=outcome.approved,
            critique=outcome.critique,
            review=outcome.review,
        ))
        if outcome.approved:
            return ReviewedLoopResult(
                status="approved",
                rounds=tuple(rounds),
                last_output=output,
                last_critique=outcome.critique,
                last_review=outcome.review,
            )
        last_critique = outcome.critique
        last_review = outcome.review

    status: ReviewedLoopStatus = (
        "exhausted_pause" if policy.pause_on_exhausted_reject
        else "exhausted_bypass"
    )
    return ReviewedLoopResult(
        status=status,
        rounds=tuple(rounds),
        last_output=last_output,
        last_critique=last_critique,
        last_review=last_review,
    )
