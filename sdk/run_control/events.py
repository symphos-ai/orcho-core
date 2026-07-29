"""Typed read / tail of a run's structured event stream.

Thin wrappers over the existing event layer — this module owns no JSONL
parser of its own:

- :func:`read_run_events` delegates to :func:`sdk.events.list_events`,
  which already projects ``core.observability.events.read_all`` into
  :class:`sdk.types.RunEvent`;
- :func:`tail_run_events` resolves the run dir via
  :func:`sdk.runs.find_run` and wraps
  :func:`core.observability.events.tail`, converting each core ``Event``
  into a :class:`RunEvent` while preserving the open ``payload`` dict
  (unknown fields are never dropped — forward-compatible).

No printing, no terminal renderer.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

from core.observability.events import Event as _CoreEvent, tail as _tail
from sdk.events import list_events
from sdk.run_control.types import RunEvent
from sdk.runs import _CWD_DEFAULT, find_run

__all__ = ["read_run_events", "tail_run_events"]


def _last_valid_event_position(events_path: Path, *, block_size: int = 8192) -> tuple[int | None, str | None]:
    """Return ``(seq, ts)`` from the last well-formed event in a JSONL file.

    This deliberately reads backwards in small binary blocks rather than using
    the public event reader: status/liveness callers only need one position and
    must not materialise a run's entire event history.  Any unavailable or
    partially-written evidence degrades to the latest preceding valid event.
    """
    try:
        with events_path.open("rb") as stream:
            stream.seek(0, 2)
            offset = stream.tell()
            suffix = b""

            while offset:
                start = max(0, offset - block_size)
                stream.seek(start)
                chunk = stream.read(offset - start)
                parts = (chunk + suffix).split(b"\n")

                # The first part can begin mid-line.  Every other part ends at
                # a newline and is therefore a complete JSONL record.
                first_complete = 0 if start == 0 else 1
                for line in reversed(parts[first_complete:]):
                    if not line:
                        continue
                    try:
                        record = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    seq, ts = record.get("seq"), record.get("ts")
                    if isinstance(seq, int) and not isinstance(seq, bool) and isinstance(ts, str) and ts:
                        return seq, ts

                suffix = parts[0]
                offset = start
    except (OSError, UnicodeDecodeError):
        pass
    return None, None


def _to_run_event(event: _CoreEvent) -> RunEvent:
    """Project a core ``Event`` into the public :class:`RunEvent`.

    ``payload`` is copied into a fresh dict so the open escape-hatch keeps
    every field verbatim without aliasing the source event.
    """
    return RunEvent(
        seq=event.seq,
        ts=event.ts,
        kind=event.kind,
        phase=event.phase,
        payload=dict(event.payload or {}),
    )


def read_run_events(
    run_id: str,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> tuple[RunEvent, ...]:
    """Return every recorded event for ``run_id`` in seq order.

    Delegates to :func:`sdk.events.list_events`. Raises ``NoWorkspace`` /
    ``RunNotFound`` through ``find_run``; a run with no ``events.jsonl``
    yields an empty tuple.
    """
    return list_events(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)


def tail_run_events(
    run_id: str,
    *,
    since_seq: int = 0,
    poll: float = 0.3,
    stop_predicate: Callable[[], bool] | None = None,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> Iterator[RunEvent]:
    """Yield events with ``seq > since_seq`` as they appear, as ``RunEvent``.

    Resolves the run dir via :func:`sdk.runs.find_run` (propagating
    ``NoWorkspace`` / ``RunNotFound``) and wraps
    :func:`core.observability.events.tail`, forwarding ``since_seq``,
    ``poll``, and ``stop_predicate`` verbatim. The caller controls
    termination via ``stop_predicate`` (or by breaking out of the
    iterator).
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    for event in _tail(ref.run_dir, since_seq, poll, stop_predicate):
        yield _to_run_event(event)
