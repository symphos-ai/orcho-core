"""Pure run-root retention decisions for workspace cleanup.

This is deliberately the only owner of run-root age arithmetic.  Checkout
cleanup has a different safety predicate and passes its already-normalized
facts here rather than reimplementing any temporal rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pipeline.run_state.status_vocab import (
    INTERRUPTED_STATUS,
    PAUSE_STATUS,
    STOPPED_RETENTION_STATUSES,
)

RETENTION_UNSET = object()
_FORCE_RECLAIM_REASONS = frozenset(
    {
        "uncommitted_changes",
        "unpushed_commits",
        "active_handoff_or_gate",
        "checkpoint_handoff_active",
    }
)


@dataclass(frozen=True, slots=True)
class RunRootFacts:
    root_id: str
    run_dir: Path
    meta: Mapping[str, Any] | None
    meta_exists: bool
    status: str | None
    retention_until: Any
    active_handoff: bool
    checkpoint_handoff: bool
    active_gate: bool
    path_safe: bool
    nested_checkout_unidentified: bool
    checkout_blocker: str | None
    dependency_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class RunRootVerdict:
    facts: RunRootFacts
    selected: bool
    reason: str
    detail: str


def resolve_retention_deadline(
    root_id: str,
    retention_until: Any,
    *,
    older_than: timedelta,
) -> tuple[datetime | None, str | None]:
    """Return the one permitted deadline, or a fail-closed reason code.

    A present stamp is authoritative even when malformed.  Only an absent
    stamp permits the legacy root-id timestamp fallback; filesystem mtime is
    intentionally never consulted.
    """
    if retention_until is not RETENTION_UNSET:
        deadline = _parse_utc_deadline(retention_until)
        return (deadline, None) if deadline is not None else (None, "retention_invalid")
    try:
        created = datetime.strptime(root_id[:15], "%Y%m%d_%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None, "root_id_timestamp_invalid"
    return created + older_than, None


def evaluate_run_root(
    facts: RunRootFacts, *, now: datetime, older_than: timedelta, force: bool = False
) -> RunRootVerdict:
    """Evaluate a root, applying the narrow force override after all guards."""
    verdict = _evaluate_run_root(facts, now=now, older_than=older_than)
    if facts.meta is None:
        # Force reclaims known-abandoned work, never unknown state. With
        # unreadable metadata a checkpoint can still surface a gate/handoff
        # reason from the allowlist — that protection must stand.
        return verdict
    forced_reason = forced_reclaim_reason(
        facts.root_id, verdict.reason, now=now, older_than=older_than, force=force
    )
    if forced_reason is None:
        return verdict
    return RunRootVerdict(facts, True, forced_reason, f"force reclaimed: {verdict.detail}")


def forced_reclaim_reason(
    root_id: str,
    reason: str,
    *,
    now: datetime,
    older_than: timedelta,
    force: bool,
) -> str | None:
    """Map only allowlisted protections for roots strictly older than cutoff.

    Unlike normal retention, force deliberately derives age only from the
    timestamp prefix of the root id.  A retention stamp can be extended and is
    not evidence that the underlying workspace itself is old.
    """
    if not force or reason not in _FORCE_RECLAIM_REASONS:
        return None
    try:
        created = datetime.strptime(root_id[:15], "%Y%m%d_%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    if created + older_than >= now:
        return None
    return f"forced_reclaim_{reason}"


def _evaluate_run_root(
    facts: RunRootFacts, *, now: datetime, older_than: timedelta
) -> RunRootVerdict:
    if not facts.path_safe:
        return _protected(
            facts, "run_root_path_unsafe", "run root is not a direct safe child of runs"
        )
    if facts.nested_checkout_unidentified:
        return _protected(
            facts,
            "nested_checkout_unidentified",
            "run root contains a checkout not represented by a safe cleanup fact",
        )
    if facts.meta is None:
        if facts.active_gate:
            return _protected(facts, "active_handoff_or_gate", "a real active gate remains")
        if facts.checkpoint_handoff:
            return _protected(
                facts, "checkpoint_handoff_active", "checkpoint-only handoff remains unresolved"
            )
        if facts.meta_exists:
            return _protected(facts, "meta_unreadable", "meta.json is unreadable or malformed")
        deadline, error = resolve_retention_deadline(
            facts.root_id, RETENTION_UNSET, older_than=older_than
        )
        if error:
            return _protected(facts, error, "root has no metadata and no valid legacy timestamp")
        assert deadline is not None
        if deadline > now:
            return _protected(
                facts, "retention_active", "legacy root retention deadline has not expired"
            )
        return _checkout_verdict(facts, "retention_expired")
    if not isinstance(facts.status, str) or facts.status not in STOPPED_RETENTION_STATUSES | {
        PAUSE_STATUS
    }:
        return _protected(
            facts, "status_not_stopped", "run is live or has an unknown lifecycle status"
        )
    deadline, error = resolve_retention_deadline(
        facts.root_id, facts.retention_until, older_than=older_than
    )
    if error:
        return _protected(facts, error, "retention deadline is malformed or unavailable")
    assert deadline is not None
    if facts.checkpoint_handoff:
        return _protected(
            facts, "checkpoint_handoff_active", "checkpoint-only handoff remains unresolved"
        )
    # A real active gate is a live coordination point, not a stale pause: it
    # stays fail-closed regardless of the retention deadline (ADR 0169). Only a
    # stale open handoff expires with retention, and only once its deadline has
    # passed.
    if facts.active_gate or (facts.active_handoff and deadline > now):
        return _protected(facts, "active_handoff_or_gate", "an active handoff or gate remains")
    if deadline > now:
        return _protected(facts, "retention_active", "retention deadline has not expired")
    pause = facts.status == PAUSE_STATUS or (
        facts.status == INTERRUPTED_STATUS and facts.active_handoff
    )
    return _checkout_verdict(facts, "pause_retention_expired" if pause else "retention_expired")


def _checkout_verdict(facts: RunRootFacts, reason: str) -> RunRootVerdict:
    if facts.checkout_blocker is not None:
        return _protected(facts, facts.checkout_blocker, "a dependent checkout remains protected")
    return RunRootVerdict(
        facts, True, reason, "stopped root and all checkout dependencies are inert or selected"
    )


def _protected(facts: RunRootFacts, reason: str, detail: str) -> RunRootVerdict:
    return RunRootVerdict(facts, False, reason, detail)


def _parse_utc_deadline(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(UTC)
