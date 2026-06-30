"""Run event reads exposed through the public SDK."""
from __future__ import annotations

from pathlib import Path

from core.observability.events import read_all as _read_events
from sdk.runs import _CWD_DEFAULT, find_run
from sdk.types import RunEvent


def list_events(
    run_id: str,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> tuple[RunEvent, ...]:
    """Return every event recorded for ``run_id`` in seq order.

    Raises ``NoWorkspace`` / ``RunNotFound`` through ``find_run``. A run
    with no ``events.jsonl`` returns an empty tuple.
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    return tuple(
        RunEvent(
            seq=e.seq,
            ts=e.ts,
            kind=e.kind,
            phase=e.phase,
            payload=dict(e.payload or {}),
        )
        for e in _read_events(ref.run_dir)
    )


__all__ = ["list_events"]
