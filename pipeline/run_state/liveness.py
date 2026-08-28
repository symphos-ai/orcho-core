"""Read-only launch, process, and durable-progress facts for a run.

This module is the shared, tolerant boundary for consumers that need to
reason about an apparently running detached process.  It reads a bounded
snapshot of ``run_supervisor.json`` and ``events.jsonl`` once; it neither polls
nor writes files or starts background work.  Callers retain ownership of their
status policy and any repair mutation.

Event-stream timestamps are read under the machine-local convention the
public event reader documents: a naive stamp keeps its wall-clock components
and takes this machine's offset, because that is what the event writer has
always emitted.  The launch artifact and a caller-injected clock are held to
the stricter offset-aware rule.

``pid_is_alive`` can only establish that a PID currently exists.  Since an OS
may reuse a PID, an ``alive`` result is deliberately not proof that it belongs
to this run.  Conversely, a ``dead`` result must be combined with stale (or
absent and old) durable progress and no terminal event before a caller treats
the run as stalled.
"""
from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from core.infra.config import AppConfig
from core.io.process_tree import pid_is_alive
from core.observability.events import read_all

_LAUNCH_FILENAME = "run_supervisor.json"
_TERMINAL_EVENT_KINDS = frozenset({"run.end", "run.interrupted"})

# Last-resort grace when neither the caller nor configuration supplies a usable
# one. Ageing must never be disabled by bad input: a budget that can't fire is
# indistinguishable from having no detector at all.
_FALLBACK_GRACE_S = 120.0


class LaunchState(StrEnum):
    """Whether the launch artifact was usable enough to inspect."""

    PRESENT = "present"
    MISSING = "missing"
    UNKNOWN = "unknown"


class PidState(StrEnum):
    """Result of an optional, single PID probe."""

    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


class DurableProgressState(StrEnum):
    """Age classification of the latest durable event."""

    ABSENT = "absent"
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RunLivenessFacts:
    """One immutable observation of launch, PID, and event-stream facts.

    ``progress_state`` describes the last event itself.  A run with no event is
    ``ABSENT`` rather than ``STALE`` so callers can preserve that distinction.
    :attr:`durable_progress_is_stale` additionally treats an absent stream as
    stale only when a valid launch timestamp has exceeded the same grace.
    """

    run_dir: Path
    grace_seconds: float
    observed_at: datetime
    launch_state: LaunchState
    pid: int | None
    launch_started_at: datetime | None
    pid_state: PidState
    last_event_kind: str | None
    last_event_at: datetime | None
    terminal_event_kind: str | None
    progress_state: DurableProgressState

    @property
    def has_terminal_event(self) -> bool:
        """Whether durable history has crossed a terminal event boundary."""
        return self.terminal_event_kind is not None

    @property
    def launch_is_stale(self) -> bool:
        """Whether a valid launch timestamp is at least one grace period old."""
        return _is_stale(self.launch_started_at, self.observed_at, self.grace_seconds)

    @property
    def durable_progress_is_stale(self) -> bool:
        """Whether progress is stale, including an absent old launch stream."""
        return self.progress_state is DurableProgressState.STALE or (
            self.progress_state is DurableProgressState.ABSENT and self.launch_is_stale
        )

    @property
    def dead_process_with_stale_progress(self) -> bool:
        """Conservative dead-process stall predicate for diagnosis or repair.

        A PID verdict alone is not sufficient: terminal history and unknown
        timestamps are intentionally no-ops.
        """
        return (
            self.pid_state is PidState.DEAD
            and not self.has_terminal_event
            and self.durable_progress_is_stale
        )


def read_liveness_facts(
    run_dir: Path | str,
    *,
    pid_probe: Callable[[int], bool] = pid_is_alive,
    grace_seconds: float | None = None,
    now: datetime | None = None,
) -> RunLivenessFacts:
    """Read a single tolerant liveness snapshot without mutating ``run_dir``.

    ``pid_probe`` is injectable for deterministic callers and tests.  Invalid
    launch data, unreadable events, malformed/non-offset-aware timestamps, an
    invalid grace, or an exception/non-boolean result from the probe degrades
    to an unknown/no-op fact rather than a dead-process conclusion.
    """
    path = Path(run_dir)
    observed_at = _normalise_timestamp(now) if now is not None else datetime.now(UTC)
    # A caller-provided naive or otherwise unusable clock cannot safely age
    # progress.  Retain readable facts but classify time-based facts unknown.
    clock = observed_at
    grace = _resolve_grace_seconds(grace_seconds)
    launch_state, launch = _read_launch(path)
    pid = _positive_int(launch.get("pid")) if launch is not None else None
    # The launch artifact is written by one writer that always stamps UTC, so
    # a naive value there is foreign evidence, not the local-time convention:
    # it stays strict, and cannot age absent history into a stall.
    started_at = _normalise_timestamp(launch.get("started_at")) if launch is not None else None
    probe_state = _probe_pid(pid, pid_probe)
    last_kind, last_at, terminal_kind, events_read = _read_event_facts(path)
    progress_state = _classify_progress(last_kind, last_at, events_read, clock, grace)

    return RunLivenessFacts(
        run_dir=path,
        grace_seconds=grace,
        observed_at=clock if clock is not None else datetime.min.replace(tzinfo=UTC),
        launch_state=launch_state,
        pid=pid,
        launch_started_at=started_at,
        pid_state=probe_state,
        last_event_kind=last_kind,
        last_event_at=last_at,
        terminal_event_kind=terminal_kind,
        progress_state=progress_state,
    )


def _read_launch(run_dir: Path) -> tuple[LaunchState, dict[str, Any] | None]:
    """Return a mapping only for a readable object-valued launch artifact."""
    artifact = run_dir / _LAUNCH_FILENAME
    try:
        if not artifact.exists():
            return LaunchState.MISSING, None
        if not artifact.is_file():
            return LaunchState.UNKNOWN, None
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return LaunchState.UNKNOWN, None
    if not isinstance(payload, dict):
        return LaunchState.UNKNOWN, None
    return LaunchState.PRESENT, payload


def _read_event_facts(
    run_dir: Path,
) -> tuple[str | None, datetime | None, str | None, bool]:
    """Read event facts once; unreadable history returns an unknown marker."""
    try:
        events = read_all(run_dir)
    except (OSError, UnicodeError):
        return None, None, None, False
    if not events:
        return None, None, None, True

    terminal_kind: str | None = None
    for event in events:
        if event.kind in _TERMINAL_EVENT_KINDS:
            terminal_kind = event.kind
    latest = events[-1]
    return (
        latest.kind or None,
        _normalise_durable_timestamp(latest.ts),
        terminal_kind,
        True,
    )


def _classify_progress(
    last_kind: str | None,
    last_at: datetime | None,
    events_read: bool,
    now: datetime | None,
    grace_seconds: float,
) -> DurableProgressState:
    """Classify only a valid, offset-aware latest event timestamp."""
    if not events_read:
        return DurableProgressState.UNKNOWN
    if last_kind is None:
        return DurableProgressState.ABSENT
    if last_at is None or now is None:
        return DurableProgressState.UNKNOWN
    elapsed = (now - last_at).total_seconds()
    if elapsed < 0:
        return DurableProgressState.UNKNOWN
    return (
        DurableProgressState.STALE
        if elapsed >= grace_seconds
        else DurableProgressState.FRESH
    )


def _probe_pid(pid: int | None, probe: Callable[[int], bool]) -> PidState:
    """Probe only a validated PID; any uncertainty is intentionally unknown."""
    if pid is None:
        return PidState.UNKNOWN
    try:
        alive = probe(pid)
    except Exception:  # noqa: BLE001 - an environment probe must fail closed
        return PidState.UNKNOWN
    if alive is True:
        return PidState.ALIVE
    if alive is False:
        return PidState.DEAD
    return PidState.UNKNOWN


def _resolve_grace_seconds(value: float | None) -> float:
    """Use the configured startup grace unless a valid explicit value is given.

    ``NaN`` and the infinities have to be rejected explicitly: they survive
    ``float()`` and are not ``<= 0``, so they would install themselves as the
    budget, and every ageing comparison against them is false. That silently
    disables the detector rather than failing it — the exact shape of blindness
    this module exists to remove.
    """
    explicit = _usable_seconds(value)
    if explicit is not None:
        return explicit
    configured = _usable_seconds(AppConfig.load().startup_stall_seconds)
    return configured if configured is not None else _FALLBACK_GRACE_S


def _usable_seconds(value: Any) -> float | None:
    """Return a positive finite float, or None for anything that is not one."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _positive_int(value: Any) -> int | None:
    """Accept a real positive integer PID, never bools or numeric strings."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _normalise_durable_timestamp(value: Any) -> datetime | None:
    """Parse an event-stream timestamp into UTC, else unknown.

    The event writer emits naive local wall-clock stamps
    (``2026-08-28T18:44:46.234``), and always has. Rejecting those as
    unusable made every time-based fact ``UNKNOWN`` on real runs, which made
    :attr:`RunLivenessFacts.dead_process_with_stale_progress` unreachable
    outside offset-aware fixtures — the detector was inert against its own
    producer, so a run whose process had been gone for hours still aged as
    "we cannot tell". Durable stamps therefore follow the same rule the
    public event reader documents: keep the wall-clock components and attach
    this machine's local offset.

    This leniency is for the event stream only. A caller-injected clock and
    the launch artifact stay strict (:func:`_normalise_timestamp`): the first
    is an argument rather than evidence, and the second has exactly one
    writer, which always stamps UTC.
    """
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            try:
                return parsed.astimezone().astimezone(UTC)
            except (OverflowError, ValueError, OSError):
                return None
    return _normalise_timestamp(value)


def _normalise_timestamp(value: Any) -> datetime | None:
    """Parse an offset-aware ISO timestamp into UTC, otherwise return unknown."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        return None
    try:
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def _is_stale(timestamp: datetime | None, now: datetime, grace_seconds: float) -> bool:
    """Return False for unavailable/future clock values (conservative no-op)."""
    if timestamp is None:
        return False
    elapsed = (now - timestamp).total_seconds()
    return elapsed >= grace_seconds


__all__ = [
    "DurableProgressState",
    "LaunchState",
    "PidState",
    "RunLivenessFacts",
    "read_liveness_facts",
]
