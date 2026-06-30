"""Event projector for the run-state layer (Stage 0 brain).

Folds a run's event stream into a :class:`RunStateSnapshot` via the pure
:func:`pipeline.run_state.reducer.apply_run_event`. The only durable input
is ``events.jsonl`` — read through
:func:`core.observability.events.read_all`, the single sanctioned reader.

``meta.json`` is deliberately never read here: the projector's whole point
is to derive run state from the event stream alone so the consistency layer
can compare that projection against ``meta.json`` without the comparison
becoming tautological.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from core.observability.events import read_all
from pipeline.run_state.reducer import apply_run_event
from pipeline.run_state.types import RunStateSnapshot


def project_events(
    events: Iterable[Mapping[str, Any]],
) -> RunStateSnapshot:
    """Fold ``events`` into a snapshot, starting from :meth:`initial`."""
    snapshot = RunStateSnapshot.initial()
    for event in events:
        snapshot = apply_run_event(snapshot, event)
    return snapshot


def project_run_dir(run_dir: Path | str) -> RunStateSnapshot:
    """Project the run-state snapshot from a run dir's ``events.jsonl``.

    Reads ONLY ``events.jsonl`` via :func:`read_all` (tolerant of partial /
    corrupt lines) and never touches ``meta.json``. A missing or empty
    ``events.jsonl`` yields ``read_all() == []``, so the projection is the
    seed snapshot (``status=UNKNOWN``) — no exception is raised.
    """
    path = Path(run_dir)
    events = (
        {
            "seq": event.seq,
            "ts": event.ts,
            "kind": event.kind,
            "phase": event.phase,
            "payload": event.payload,
        }
        for event in read_all(path)
    )
    return project_events(events)


__all__ = ["project_events", "project_run_dir"]
