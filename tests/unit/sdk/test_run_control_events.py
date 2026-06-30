"""Unit tests for :mod:`sdk.run_control.events`.

Covers ordered replay, the empty/absent stream, tail filtering by
``since_seq`` with a ``stop_predicate``, and payload forward-compat
(arbitrary unknown keys survive the projection to :class:`RunEvent`).

Hermetic: each test writes its own ``events.jsonl`` under a tmp
``runs_dir`` and passes ``runs_dir=`` / ``cwd=None``.
"""
from __future__ import annotations

import json
from pathlib import Path

from sdk.run_control.events import read_run_events, tail_run_events
from sdk.run_control.types import RunEvent

# ── helpers ──────────────────────────────────────────────────────────────────


def _write_events(runs_dir: Path, run_id: str, events: list[dict]) -> Path:
    """Materialise a run dir with an events.jsonl in the durable line format."""
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    lines = [json.dumps(e) for e in events]
    (run_dir / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run_dir


def _event(seq: int, kind: str, *, phase: str | None = None, payload: dict | None = None) -> dict:
    return {"seq": seq, "ts": f"2026-06-06T00:00:0{seq}.000", "kind": kind, "phase": phase, "payload": payload or {}}


# ── read_run_events ──────────────────────────────────────────────────────────


class TestReadRunEvents:
    def test_returns_events_in_seq_order_as_tuple(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _write_events(
            runs,
            "r1",
            [
                _event(1, "run.start"),
                _event(2, "phase.start", phase="plan"),
                _event(3, "run.end"),
            ],
        )

        events = read_run_events("r1", runs_dir=runs, cwd=None)

        assert isinstance(events, tuple)
        assert all(isinstance(e, RunEvent) for e in events)
        assert [e.seq for e in events] == [1, 2, 3]
        assert [e.kind for e in events] == ["run.start", "phase.start", "run.end"]
        assert events[1].phase == "plan"

    def test_missing_events_file_returns_empty_tuple(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        (runs / "r2").mkdir()  # run dir exists, no events.jsonl

        assert read_run_events("r2", runs_dir=runs, cwd=None) == ()

    def test_preserves_unknown_payload_keys(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        payload = {"some_future_field": {"nested": [1, 2]}, "phase_key": "plan"}
        _write_events(runs, "r3", [_event(1, "custom.kind", payload=payload)])

        events = read_run_events("r3", runs_dir=runs, cwd=None)

        assert events[0].payload == payload
        assert events[0].payload["some_future_field"] == {"nested": [1, 2]}


# ── tail_run_events ──────────────────────────────────────────────────────────


class TestTailRunEvents:
    def test_stop_predicate_terminates_and_filters_since_seq(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _write_events(
            runs,
            "r4",
            [_event(1, "a"), _event(2, "b"), _event(3, "c")],
        )

        collected = list(
            tail_run_events(
                "r4",
                since_seq=1,
                poll=0.01,
                stop_predicate=lambda: True,
                runs_dir=runs,
                cwd=None,
            )
        )

        # since_seq=1 → only seq 2 and 3; stop_predicate ends iteration.
        assert [e.seq for e in collected] == [2, 3]
        assert all(isinstance(e, RunEvent) for e in collected)

    def test_tail_preserves_unknown_payload_keys(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        payload = {"weird": "value", "round": 2}
        _write_events(runs, "r5", [_event(1, "k", payload=payload)])

        collected = list(
            tail_run_events(
                "r5",
                poll=0.01,
                stop_predicate=lambda: True,
                runs_dir=runs,
                cwd=None,
            )
        )

        assert collected[0].payload == payload
