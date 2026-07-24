"""Pure terminal delivery projections from durable session facts.

This module deliberately has no delivery-policy side effects.  It only reads
the canonical ``commit_delivery`` audit record and, when present, the durable
operator-override marker written by the rejected-release reducer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, get_args

from core.io.delivery_summary import project_degraded_publish
from pipeline.engine.commit_delivery import CommitDeliveryStatus
from pipeline.run_state.release_verdict import is_rejected


class TerminalDeliveryDisposition(StrEnum):
    """What the durable delivery facts establish for terminal presentation."""

    DELIVERED = "delivered"
    DELIVERED_BY_OPERATOR_OVERRIDE = "delivered_by_operator_override"
    NOT_DELIVERED = "not_delivered"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TerminalDeliveryOutcome:
    """Typed, read-only terminal delivery outcome.

    ``status`` carries the recognized canonical status when available.  It is
    retained for typed consumers and diagnostics; presentation decisions use
    ``disposition`` instead of trying to infer delivery from a branch, SHA, or
    terminal text.
    """

    disposition: TerminalDeliveryDisposition
    status: CommitDeliveryStatus | None = None


_CANONICAL_STATUSES: frozenset[str] = frozenset(get_args(CommitDeliveryStatus))
_DELIVERED_STATUSES: frozenset[str] = frozenset({
    "committed",
    "applied_uncommitted",
})
_NOT_DELIVERED_STATUSES: frozenset[str] = (
    _CANONICAL_STATUSES - _DELIVERED_STATUSES
)
# Destination lines intentionally remain narrower than disposition: parked and
# pre-terminal canonical statuses have no settled destination to print.
_DESTINATION_NOT_DELIVERED_STATUSES: frozenset[str] = frozenset({
    "halted",
    "target_dirty",
    "commit_failed",
    "apply_failed",
    "verification_blocked",
})


def _canonical_status(record: Mapping[str, Any] | None) -> CommitDeliveryStatus | None:
    if not isinstance(record, Mapping):
        return None
    value = record.get("status")
    if not isinstance(value, str) or value not in _CANONICAL_STATUSES:
        return None
    return value  # type: ignore[return-value]  # narrowed by _CANONICAL_STATUSES


def _has_consistent_override(
    override: object, *, status: CommitDeliveryStatus,
) -> bool:
    """Return whether a durable override marker corroborates applied delivery."""
    if not isinstance(override, Mapping):
        return False
    return (
        override.get("reason") == "final_acceptance_rejected_override"
        and override.get("status") == "done"
        and is_rejected(override.get("release_verdict"))
        and override.get("delivery_status") == status
    )


def project_terminal_delivery(
    session: Mapping[str, Any],
) -> TerminalDeliveryOutcome:
    """Classify terminal delivery from canonical durable facts only.

    An operator override is established only by an applied delivery status and
    a marker that explicitly agrees with it.  Every other canonical status is
    known not-delivered; absent or unrecognized records are intentionally
    ``UNKNOWN`` rather than a false claim that no delivery happened.
    """
    status = _canonical_status(session.get("commit_delivery"))
    if status is None:
        return TerminalDeliveryOutcome(TerminalDeliveryDisposition.UNKNOWN)
    if status in _DELIVERED_STATUSES:
        if _has_consistent_override(session.get("delivery_override"), status=status):
            return TerminalDeliveryOutcome(
                TerminalDeliveryDisposition.DELIVERED_BY_OPERATOR_OVERRIDE,
                status,
            )
        return TerminalDeliveryOutcome(TerminalDeliveryDisposition.DELIVERED, status)
    if status in _NOT_DELIVERED_STATUSES:
        return TerminalDeliveryOutcome(
            TerminalDeliveryDisposition.NOT_DELIVERED,
            status,
        )
    return TerminalDeliveryOutcome(TerminalDeliveryDisposition.UNKNOWN)


def render_delivery_destination_lines(
    session: Mapping[str, Any],
    *,
    publish_gate: object | None = None,
) -> tuple[str, ...]:
    """Return compact delivery-destination lines from ``commit_delivery``."""
    record = session.get("commit_delivery")
    if not isinstance(record, Mapping):
        return ()
    status = _canonical_status(record)
    if status is None:
        return ()
    if status == "committed":
        degraded = project_degraded_publish(record, publish_gate=publish_gate)
        if degraded is not None:
            return (f"Delivery: {degraded.ready_text} — reason: {degraded.reason}",)
        branch = str(record.get("delivery_branch") or "")
        sha = str(record.get("commit_sha") or "")
        pr_url = str(record.get("pr_url") or "")
        if branch and not sha:
            if pr_url:
                return (f"Delivery: branch {branch} → PR {pr_url}",)
            return (
                f"Delivery: branch {branch} ready — "
                "push if needed, then open a PR",
            )
        if sha and not branch:
            return (f"Delivery: committed {sha[:7]} to project checkout",)
        if sha and branch:
            if pr_url:
                return (f"Delivery: committed {sha[:7]} onto {branch} → PR {pr_url}",)
            return (f"Delivery: committed {sha[:7]} onto {branch}",)
        return ("Delivery: committed to project checkout",)
    if status == "applied_uncommitted":
        return ("Delivery: applied to project checkout (uncommitted)",)
    if status == "skipped":
        return ("Delivery: skipped — diff retained",)
    if status in _DESTINATION_NOT_DELIVERED_STATUSES:
        return (f"Delivery: not delivered ({status})",)
    return ()
