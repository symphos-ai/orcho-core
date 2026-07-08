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


def format_estimated_entries_footer(
    count: int,
    source: str,
    age_warning: str = "",
) -> str:
    """Format the estimated-entries footnote of a cost report.

    The count identifies only the estimated entries (priced from a local
    table); runtime-reported entries are excluded. The wording never names a
    runtime or model and never reads like a billing line.
    """
    unit = "entry" if count == 1 else "entries"
    return f"  ↳ {count} phase {unit} estimated from {source}{age_warning}"


def runtime_accounting_hint(runtime_id: str) -> str:
    """Return a short accounting-mode hint for known wrapper runtimes."""
    normalized = runtime_id.strip().lower()
    if normalized == "claude-glm" or normalized.startswith("claude-glm-"):
        return "subscription/quota runtime; not API billing"
    return ""


ACCOUNTING_REFERENCE_NOTE = (
    "Cost reference is usage accounting, not a billing receipt. Runtime-reported "
    "values come from the active runtime/endpoint; estimated-api values use "
    "Orcho pricing tables. Wrapper runtime rows keep their own runtime identity. "
    "Subscription plans may bill differently."
)


__all__ = [
    "ACCOUNTING_REFERENCE_NOTE",
    "CostReferenceDisplay",
    "cost_reference_display",
    "format_cost_reference",
    "format_cost_reference_key_value",
    "format_cost_reference_summary",
    "format_estimated_entries_footer",
    "runtime_accounting_hint",
]
