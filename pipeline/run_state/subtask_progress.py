"""Partial subtask-DAG progress detection (Stage 0 brain).

A pure, event-sourced detector answering one question: which subtask ids in a
run's event stream *started* but never reached a successful terminal? Such a
subtask is the signature of a torn IMPLEMENT — a halted/interrupted DAG that
left work mid-flight. Resuming straight into review against that state would
march the run on against partial work, so the IMPLEMENT phase uses this to fail
early with an instructive message instead.

Semantics (last-write-wins per ``subtask_id``): events are folded in stream
order; for each id the *last* event decides its state.

* last event is ``subtask.start`` (no following ``subtask.end``) → unfinished.
* last event is ``subtask.end`` with ``ok`` truthy → finished.
* last event is ``subtask.end`` with ``ok`` not ``True`` (``False``, missing —
  i.e. no DONE/ATTESTATION close) → unfinished.

Last-write-wins is load-bearing: a retry that re-runs a previously-unfinished
subtask and this time emits ``subtask.end ok=True`` clears it from the set; a
retry that re-starts but does not finish leaves it unfinished. On a fresh run
(no subtask events at all) the set is empty, so the detector is inert.

Pure: :func:`unfinished_subtask_ids` does no I/O. The thin run-dir reader
:func:`unfinished_subtask_ids_in_run_dir` folds ``events.jsonl`` through the
single sanctioned reader (:func:`core.observability.events.read_all`) rather
than re-parsing the stream.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from core.observability.events import read_all

_START_KIND = "subtask.start"
_END_KIND = "subtask.end"


def unfinished_subtask_ids(events: Iterable[Mapping[str, Any]]) -> set[str]:
    """Return the set of subtask ids that started but never finished.

    ``events`` is any iterable of event mappings shaped like ``{"kind": str,
    "payload": Mapping}`` (the projector's read shape). Non-subtask events and
    events without a non-empty string ``payload.subtask_id`` are ignored.
    """
    finished: dict[str, bool] = {}
    for event in events:
        kind = event.get("kind")
        if kind not in (_START_KIND, _END_KIND):
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}
        sid = payload.get("subtask_id")
        if not isinstance(sid, str) or not sid:
            continue
        if kind == _START_KIND:
            finished[sid] = False
        else:  # subtask.end — finished only on an explicit ok=True close.
            finished[sid] = payload.get("ok") is True
    return {sid for sid, done in finished.items() if not done}


def unfinished_subtask_ids_in_run_dir(run_dir: Path | str) -> set[str]:
    """Fold a run dir's ``events.jsonl`` into the unfinished-subtask set.

    Thin reader: defers to :func:`core.observability.events.read_all` (tolerant
    of partial / corrupt lines; a missing or empty stream yields ``[]``) and
    then the pure :func:`unfinished_subtask_ids`. A missing ``events.jsonl``
    therefore yields an empty set rather than raising.
    """
    path = Path(run_dir)
    events = (
        {"kind": event.kind, "payload": event.payload}
        for event in read_all(path)
    )
    return unfinished_subtask_ids(events)


__all__ = ["unfinished_subtask_ids", "unfinished_subtask_ids_in_run_dir"]
