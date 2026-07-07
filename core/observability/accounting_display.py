"""Shared display wording for dollar-denominated usage accounting.

The stored metric key stays ``cost_usd_equivalent`` for compatibility, but
terminal output must not look like a billing receipt. Runtime-reported values
come from the active runtime/endpoint. Estimated values come from Orcho's local
pricing table.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CostReferenceDisplay:
    """Resolved labels for a cost reference value."""

    source: str
    marker: str


def cost_reference_display(*, estimated: bool = False) -> CostReferenceDisplay:
    """Return source wording and marker for a cost reference value."""
    if estimated:
        return CostReferenceDisplay(source="estimated-api", marker="~$")
    return CostReferenceDisplay(source="runtime-reported", marker="$")


def format_cost_reference(
    cost: float,
    *,
    estimated: bool = False,
    precision: int = 2,
    thousands: bool = False,
) -> str:
    """Format a cost reference with source wording and the right marker."""
    display = cost_reference_display(estimated=estimated)
    amount = f"{float(cost):,.{precision}f}" if thousands else f"{float(cost):.{precision}f}"
    return f"{display.source} {display.marker}{amount}"


def format_cost_reference_key_value(
    cost: float,
    *,
    estimated: bool = False,
    key: str = "cost_ref",
    precision: int = 2,
    thousands: bool = False,
) -> str:
    """Format compact ``key=source:$amount`` usage text."""
    display = cost_reference_display(estimated=estimated)
    amount = f"{float(cost):,.{precision}f}" if thousands else f"{float(cost):.{precision}f}"
    return f"{key}={display.source}:{display.marker}{amount}"


def format_cost_reference_summary(
    cost: float,
    *,
    estimated: bool = False,
    precision: int = 2,
) -> str:
    """Format the short one-line summary label."""
    return f"Cost ref: {format_cost_reference(cost, estimated=estimated, precision=precision)}"


ACCOUNTING_REFERENCE_NOTE = (
    "Cost reference is usage accounting, not a billing receipt. Runtime-reported "
    "values come from the active runtime/endpoint; estimated-api values use "
    "Orcho pricing tables. Subscription plans may bill differently."
)


__all__ = [
    "ACCOUNTING_REFERENCE_NOTE",
    "CostReferenceDisplay",
    "cost_reference_display",
    "format_cost_reference",
    "format_cost_reference_key_value",
    "format_cost_reference_summary",
]
