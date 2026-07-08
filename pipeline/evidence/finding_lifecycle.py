"""Finding lifecycle projection for evidence renderers.

The raw evidence surface records every finding that any reviewer phase emitted.
This helper adds a reader-facing lifecycle state so renderers can distinguish
active findings from ones that were fixed by a later pass, accepted by an
approved phase, or explicitly waived by an operator.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

RELEASE_PHASES: tuple[str, ...] = ("final_acceptance", "cross_final_acceptance")

ACTIVE_FINDING_STATUSES: frozenset[str] = frozenset({"open", "final_rejected"})
FINDING_STATUS_ORDER: tuple[str, ...] = (
    "final_rejected",
    "open",
    "waived",
    "fixed",
    "accepted",
)


def annotate_finding_lifecycle(
    findings: list[dict[str, Any]],
    phase_attempts: Mapping[str, list[dict[str, Any]]],
    *,
    waiver: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return findings with additive ``status`` and ``status_reason`` fields."""
    if not findings:
        return []

    waived = _waived_fingerprints(waiver)
    approved_attempts = _latest_approved_attempts(phase_attempts)
    release_approved = _has_approved_release(phase_attempts)

    out: list[dict[str, Any]] = []
    for finding in findings:
        entry = dict(finding)
        status, reason = _classify_finding(
            entry,
            waived=waived,
            approved_attempts=approved_attempts,
            release_approved=release_approved,
        )
        entry["status"] = status
        entry["status_reason"] = reason
        out.append(entry)
    return out


def is_active_finding(finding: Mapping[str, Any]) -> bool:
    return str(finding.get("status") or "open") in ACTIVE_FINDING_STATUSES


def finding_status_sort_key(finding: Mapping[str, Any]) -> tuple[int, str, int, str]:
    status = str(finding.get("status") or "open")
    try:
        status_rank = FINDING_STATUS_ORDER.index(status)
    except ValueError:
        status_rank = len(FINDING_STATUS_ORDER)
    phase = str(finding.get("phase") or "")
    try:
        attempt = int(finding.get("attempt") or 0)
    except (TypeError, ValueError):
        attempt = 0
    return status_rank, phase, attempt, str(finding.get("title") or "")


def _classify_finding(
    finding: Mapping[str, Any],
    *,
    waived: set[str],
    approved_attempts: Mapping[str, int],
    release_approved: bool,
) -> tuple[str, str]:
    fingerprint = _finding_fingerprint(finding)
    phase = str(finding.get("phase") or "")
    attempt = _coerce_int(finding.get("attempt"), 0)

    if fingerprint in waived:
        return "waived", "accepted by operator waiver"
    if phase in RELEASE_PHASES and _is_rejected_release_finding(finding):
        return "final_rejected", "final acceptance rejected this finding"
    if _is_source_approved(finding):
        return "accepted", "source phase approved with this finding present"
    if approved_attempts.get(phase, 0) > attempt:
        return "fixed", f"later {phase} attempt approved"
    if release_approved and phase not in RELEASE_PHASES:
        return "fixed", "final acceptance approved after this finding"
    return "open", "no approved follow-up or waiver found"


def _latest_approved_attempts(
    phase_attempts: Mapping[str, list[dict[str, Any]]],
) -> dict[str, int]:
    out: dict[str, int] = {}
    for phase, attempts in phase_attempts.items():
        for idx, attempt in enumerate(attempts, start=1):
            if _attempt_is_approved(attempt):
                out[phase] = max(
                    out.get(phase, 0),
                    _coerce_int(attempt.get("attempt"), idx),
                )
    return out


def _has_approved_release(
    phase_attempts: Mapping[str, list[dict[str, Any]]],
) -> bool:
    return any(
        _attempt_is_approved(attempt)
        for phase in RELEASE_PHASES
        for attempt in phase_attempts.get(phase, [])
    )


def _attempt_is_approved(attempt: Mapping[str, Any]) -> bool:
    if attempt.get("approved") is True or attempt.get("ship_ready") is True:
        return True
    return str(attempt.get("verdict") or "").upper() == "APPROVED"


def _is_source_approved(finding: Mapping[str, Any]) -> bool:
    if finding.get("source_approved") is True or finding.get("source_ship_ready") is True:
        return True
    return str(finding.get("source_verdict") or "").upper() == "APPROVED"


def _is_rejected_release_finding(finding: Mapping[str, Any]) -> bool:
    if finding.get("source_ship_ready") is False:
        return True
    if finding.get("source_approved") is False:
        return True
    return str(finding.get("source_verdict") or "").upper() == "REJECTED"


def _waived_fingerprints(waiver: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(waiver, Mapping):
        return set()
    findings = waiver.get("findings")
    if not isinstance(findings, list):
        return set()
    out: set[str] = set()
    for finding in findings:
        if isinstance(finding, Mapping):
            out.add(_finding_fingerprint(finding))
    return out


def _finding_fingerprint(finding: Mapping[str, Any]) -> str:
    finding_id = str(finding.get("id") or "").strip()
    if finding_id:
        return f"id:{finding_id}"
    return "|".join((
        "fingerprint",
        str(finding.get("severity") or ""),
        str(finding.get("title") or ""),
        str(finding.get("file") or ""),
        str(finding.get("line") or ""),
    ))


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "ACTIVE_FINDING_STATUSES",
    "FINDING_STATUS_ORDER",
    "annotate_finding_lifecycle",
    "finding_status_sort_key",
    "is_active_finding",
]
