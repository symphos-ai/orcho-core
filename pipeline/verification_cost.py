"""Typed, display-only verification command cost resolution."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

type VerificationCost = Literal["fast", "moderate", "slow", "unknown"]

VERIFICATION_COSTS: tuple[VerificationCost, ...] = (
    "fast", "moderate", "slow", "unknown",
)

# Higher rank is more conservative.  Keep this separate from policy/action:
# cost is metadata and must never affect execution control flow.
_COST_RANK: dict[VerificationCost, int] = {
    "fast": 0,
    "moderate": 1,
    "slow": 2,
    "unknown": 3,
}


def resolve_verification_cost(
    command_cost: VerificationCost | None,
    default_costs: Iterable[VerificationCost | None],
) -> VerificationCost:
    """Return command cost, or the most conservative contributing default.

    Missing declarations resolve to ``unknown``.  The result is independent of
    declaration order, which makes overlapping gate-set projections stable.
    """
    if command_cost is not None:
        return command_cost
    declared = [cost for cost in default_costs if cost is not None]
    return max(declared, key=_COST_RANK.__getitem__) if declared else "unknown"
