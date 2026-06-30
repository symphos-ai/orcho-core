"""SDK event-read surface."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdk import RunEvent, RunNotFound, list_events


def _write_event(
    run_dir: Path,
    *,
    seq: int,
    kind: str,
    phase: str | None = None,
    payload: dict | None = None,
) -> None:
    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "seq": seq,
            "ts": f"2026-05-25T10:00:0{seq}.000",
            "kind": kind,
            "phase": phase,
            "payload": payload or {},
        }) + "\n")


def test_list_events_returns_typed_events_in_seq_order(runs_root: Path) -> None:
    run_dir = runs_root / "run_with_events"
    run_dir.mkdir()
    _write_event(
        run_dir,
        seq=1,
        kind="run.start",
        payload={"task": "demo"},
    )
    _write_event(
        run_dir,
        seq=2,
        kind="phase.start",
        phase="PLAN",
        payload={"name": "plan"},
    )

    events = list_events("run_with_events", runs_dir=runs_root)

    assert events == (
        RunEvent(
            seq=1,
            ts="2026-05-25T10:00:01.000",
            kind="run.start",
            phase=None,
            payload={"task": "demo"},
        ),
        RunEvent(
            seq=2,
            ts="2026-05-25T10:00:02.000",
            kind="phase.start",
            phase="PLAN",
            payload={"name": "plan"},
        ),
    )


def test_list_events_missing_file_returns_empty_tuple(runs_root: Path) -> None:
    (runs_root / "run_without_events").mkdir()

    assert list_events("run_without_events", runs_dir=runs_root) == ()


def test_list_events_unknown_run_raises_run_not_found(runs_root: Path) -> None:
    with pytest.raises(RunNotFound):
        list_events("missing", runs_dir=runs_root)
