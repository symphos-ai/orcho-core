"""Pure projections for the publish portion of a delivery summary.

The durable ``commit_delivery`` mapping intentionally does not persist the
effective publish gate.  Callers therefore provide that runtime fact here;
this module only classifies and formats the already-recorded delivery facts.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from core.io.ansi import strip_ansi

_PUBLISH_CAPABLE_GATES = frozenset({"auto", "always"})
_MAX_REASON_LENGTH = 140
_FALLBACK_REASON = "publication did not return a PR URL"


@dataclass(frozen=True, slots=True)
class DegradedPublishOutcome:
    """One confirmed publish attempt that did not produce a pull request."""

    branch: str
    reason: str

    @property
    def ready_text(self) -> str:
        """Compact branch fact shared by all summary surfaces."""
        return f"branch {self.branch} ready"


def project_degraded_publish(
    delivery: Mapping[str, Any] | None,
    *,
    publish_gate: object | None,
) -> DegradedPublishOutcome | None:
    """Return a degradation only when durable facts confirm a publish attempt.

    ``auto`` is intentionally not inferred from a branch alone: a local
    ``commit_on_branch`` delivery is a valid non-publishing auto path.  A
    publish-related warning or the durable ready notice is the required second
    signal.
    """
    if not isinstance(delivery, Mapping):
        return None
    if _normalize_gate(publish_gate) not in _PUBLISH_CAPABLE_GATES:
        return None
    if delivery.get("pr_url") is not None:
        return None

    branch = _one_line(delivery.get("delivery_branch"))
    warnings = _strings(delivery.get("delivery_warnings"))
    notices = _strings(delivery.get("delivery_notices"))
    publish_warning = next(
        (item for item in warnings if _is_publish_warning(item)), None,
    )
    ready_notice = next((item for item in notices if _is_ready_notice(item)), None)
    if not branch or (publish_warning is None and ready_notice is None):
        return None
    return DegradedPublishOutcome(
        branch=branch,
        reason=_clip_reason(publish_warning or _FALLBACK_REASON),
    )


def _normalize_gate(value: object | None) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _strings(value: object) -> Sequence[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _is_publish_warning(text: str) -> bool:
    normalized = _one_line(text).lower()
    return any(token in normalized for token in (
        "publish", "push", "pull request", "provider", "remote",
    ))


def _is_ready_notice(text: str) -> bool:
    normalized = _one_line(text).lower()
    return "delivery branch" in normalized and "is ready" in normalized


def _one_line(value: object) -> str:
    return " ".join(strip_ansi(str(value or "")).split())


def _clip_reason(value: object) -> str:
    text = _one_line(value)
    if len(text) <= _MAX_REASON_LENGTH:
        return text
    return text[: _MAX_REASON_LENGTH - 1] + "…"


__all__ = ["DegradedPublishOutcome", "project_degraded_publish"]
