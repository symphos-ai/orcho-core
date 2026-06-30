"""Pure run-state reducer (Stage 0 brain).

:func:`apply_run_event` folds one event onto a :class:`RunStateSnapshot`,
returning a new snapshot. It is fully pure: no filesystem, no subprocess,
and it mutates neither the input snapshot nor the input event mapping. New
state is built with :func:`dataclasses.replace` and freshly-constructed
tuples; the event ``payload`` is read as a read-only mapping.

Several modeled event types (``run.started``, ``phase_handoff.decided``,
``run.interrupted``, ``run.halted``) are client-neutral and not emitted by
any current writer — see :class:`pipeline.run_state.types.RunEventType`.
Halt is, in practice, observed from on-disk decision artifacts by the
consistency layer, not from the event stream; the reducer models the
synthetic ``phase_handoff.decided`` / ``run.halted`` forms for completeness.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from pipeline.run_state.phase_outcome import is_phase_checkpoint_success
from pipeline.run_state.types import RunEventType, RunStateSnapshot, RunStatus

# Outcome/status tokens on a ``phase.end`` payload that mark a phase failed.
_FAILED_PHASE_TOKENS: frozenset[str] = frozenset(
    {"failed", "rejected", "error"}
)
# Terminal run statuses — reaching one sets ``terminal=True``.
_TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.DONE, RunStatus.FAILED, RunStatus.HALTED}
)
# ``run.end`` payload status → folded RunStatus.
_RUN_END_STATUS: dict[str, RunStatus] = {
    "done": RunStatus.DONE,
    "failed": RunStatus.FAILED,
    "halted": RunStatus.HALTED,
}


def apply_run_event(
    snapshot: RunStateSnapshot, event: Mapping[str, Any]
) -> RunStateSnapshot:
    """Fold one event onto ``snapshot``, returning a new snapshot.

    Pure: no I/O, no subprocess, no mutation of ``snapshot`` or ``event``.
    An unknown ``kind`` (one :meth:`RunEventType.from_kind` cannot map)
    leaves the domain state untouched and returns ``snapshot`` unchanged —
    including ``seq`` (an event the reducer does not understand should not
    silently advance the watermark).

    Tolerant of missing payload fields by default;
    :class:`pipeline.run_state.types.RunTransitionError` is reserved for
    strict callers checking logically impossible events, not raised here.
    """
    kind = event.get("kind")
    event_type = RunEventType.from_kind(kind) if isinstance(kind, str) else None
    if event_type is None:
        return snapshot

    payload = event.get("payload") or {}
    if not isinstance(payload, Mapping):
        payload = {}
    seq = event.get("seq")

    changes = _transition(snapshot, event_type, payload)

    # Advance the seq watermark for every recognized event (a no-op
    # transition still records that the event was accounted for).
    if isinstance(seq, int):
        changes["seq"] = seq

    if not changes:
        return snapshot
    return dataclasses.replace(snapshot, **changes)


def _transition(
    snapshot: RunStateSnapshot,
    event_type: RunEventType,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the field changes for ``event_type`` (excluding ``seq``)."""
    if event_type in (RunEventType.RUN_START, RunEventType.RUN_STARTED):
        return _with_status(RunStatus.RUNNING)

    if event_type is RunEventType.PHASE_START:
        return _phase_start(snapshot, payload)

    if event_type is RunEventType.PHASE_END:
        return _phase_end(snapshot, payload)

    if event_type is RunEventType.PHASE_HANDOFF_REQUESTED:
        return _handoff_requested(snapshot, payload)

    if event_type is RunEventType.PHASE_HANDOFF_DECIDED:
        return _handoff_decided(payload)

    if event_type is RunEventType.RUN_END:
        return _run_end(payload)

    if event_type is RunEventType.RUN_INTERRUPTED:
        return _with_status(RunStatus.INTERRUPTED)

    if event_type is RunEventType.RUN_HALTED:
        return _with_status(RunStatus.HALTED)

    return {}


def _with_status(status: RunStatus) -> dict[str, Any]:
    """Status change with ``terminal`` derived from the status."""
    return {"status": status, "terminal": status in _TERMINAL_STATUSES}


def _phase_start(
    snapshot: RunStateSnapshot, payload: Mapping[str, Any]
) -> dict[str, Any]:
    phase = _phase_name(payload)
    if phase is None:
        return {}
    return {
        "seen_phases": _append_unique(snapshot.seen_phases, phase),
        "active_phase": phase,
    }


def _phase_end(
    snapshot: RunStateSnapshot, payload: Mapping[str, Any]
) -> dict[str, Any]:
    phase = _phase_name(payload)
    if phase is None:
        return {}
    changes: dict[str, Any] = {
        "seen_phases": _append_unique(snapshot.seen_phases, phase),
    }
    # Three-branch classification of the phase outcome:
    #   * failed token ('failed'/'rejected'/'error')   → failed_phases
    #   * checkpoint-success token ('ok'/'skipped*')    → completed_phases
    #   * anything else (halted:*/incomplete/no_verdict/handoff/unknown/'DONE')
    #     → neither — a phase.end exists but the phase is NOT a completed
    #     checkpoint, so it stays only in seen_phases. Treating bare presence
    #     of phase.end as completion is the bug this closes.
    outcome = _phase_outcome_token(payload)
    if _is_failed_outcome(payload):
        changes["failed_phases"] = _append_unique(snapshot.failed_phases, phase)
    elif is_phase_checkpoint_success(outcome):
        changes["completed_phases"] = _append_unique(
            snapshot.completed_phases, phase
        )
    if snapshot.active_phase == phase:
        changes["active_phase"] = None
    return changes


def _handoff_requested(
    snapshot: RunStateSnapshot, payload: Mapping[str, Any]
) -> dict[str, Any]:
    handoff_id = payload.get("handoff_id")
    phase = payload.get("phase")
    changes: dict[str, Any] = {"status": RunStatus.AWAITING_PHASE_HANDOFF}
    if isinstance(handoff_id, str) and handoff_id:
        changes["active_handoff_id"] = handoff_id
        changes["seen_handoff_ids"] = _append_unique(
            snapshot.seen_handoff_ids, handoff_id
        )
    if isinstance(phase, str) and phase:
        changes["active_handoff_phase"] = phase
    return changes


def _handoff_decided(payload: Mapping[str, Any]) -> dict[str, Any]:
    """``phase_handoff.decided``: only ``action='halt'`` changes Stage-0 state.

    Halt clears the active handoff pointer and goes terminal, but
    ``seen_handoff_ids`` is preserved (requirement F2 — the history must
    survive so consistency can tell "event happened" from "never
    requested"). Every other action is a no-op at Stage 0.
    """
    if payload.get("action") != "halt":
        return {}
    return {
        "status": RunStatus.HALTED,
        "terminal": True,
        "active_handoff_id": None,
        "active_handoff_phase": None,
    }


def _run_end(payload: Mapping[str, Any]) -> dict[str, Any]:
    """``run.end``: map the payload status to a terminal RunStatus.

    An unknown / missing status is handled conservatively: do not raise and
    do not force a status — leave the snapshot's status as-is (the run
    ended but with an unrecognized outcome we won't invent).
    """
    status = _RUN_END_STATUS.get(payload.get("status"))
    if status is None:
        return {}
    return _with_status(status)


def _phase_name(payload: Mapping[str, Any]) -> str | None:
    """Resolve a phase label from a payload (``title`` preferred, then ``phase``)."""
    for key in ("title", "phase"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _phase_outcome_token(payload: Mapping[str, Any]) -> str | None:
    """Resolve the phase outcome token (``outcome`` preferred, then ``status``)."""
    for key in ("outcome", "status"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _is_failed_outcome(payload: Mapping[str, Any]) -> bool:
    for key in ("outcome", "status"):
        value = payload.get(key)
        if isinstance(value, str) and value.lower() in _FAILED_PHASE_TOKENS:
            return True
    return False


def _append_unique(existing: tuple[str, ...], value: str) -> tuple[str, ...]:
    """Return ``existing`` with ``value`` appended if not already present."""
    if value in existing:
        return existing
    return (*existing, value)


__all__ = ["apply_run_event"]
