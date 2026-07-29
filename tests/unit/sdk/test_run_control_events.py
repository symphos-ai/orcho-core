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

from sdk.run_control.events import _last_valid_event_position, read_run_events, tail_run_events
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


# ── private last-event position probe ───────────────────────────────────────


class TestLastValidEventPosition:
    def test_returns_last_line_of_multiline_file(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        path.write_text("\n".join(json.dumps(_event(i, "event")) for i in range(1, 4)), encoding="utf-8")

        assert _last_valid_event_position(path) == (3, "2026-06-06T00:00:03.000")

    def test_accepts_trailing_newline_and_falls_back_from_malformed_tail(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        path.write_bytes(
            f"{json.dumps(_event(1, 'event'))}\n{json.dumps(_event(2, 'event'))}\n".encode()
            + b'{"seq": "not-an-int", "ts": "2026-06-06"}\n{bad json\n\xff\n'
        )

        assert _last_valid_event_position(path) == (2, "2026-06-06T00:00:02.000")

    def test_missing_empty_and_unreadable_files_degrade_without_error(self, tmp_path: Path, monkeypatch) -> None:
        missing = tmp_path / "missing.jsonl"
        empty = tmp_path / "empty.jsonl"
        empty.touch()

        assert _last_valid_event_position(missing) == (None, None)
        assert _last_valid_event_position(empty) == (None, None)

        def fail_open(self: Path, *args: object, **kwargs: object) -> object:
            raise OSError("denied")

        monkeypatch.setattr(Path, "open", fail_open)
        assert _last_valid_event_position(tmp_path / "unreadable.jsonl") == (None, None)

    def test_reads_only_tail_blocks_not_full_history(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "events.jsonl"
        path.write_bytes(
            b"".join(json.dumps(_event(i, "event")).encode() + b"\n" for i in range(1, 10_001))
        )
        bytes_read = 0
        original_open = Path.open

        class CountingStream:
            def __init__(self, stream: object) -> None:
                self._stream = stream

            def __enter__(self) -> CountingStream:
                self._stream.__enter__()  # type: ignore[union-attr]
                return self

            def __exit__(self, *args: object) -> object:
                return self._stream.__exit__(*args)  # type: ignore[union-attr]

            def __getattr__(self, name: str) -> object:
                return getattr(self._stream, name)

            def read(self, size: int = -1) -> bytes:
                nonlocal bytes_read
                data = self._stream.read(size)  # type: ignore[union-attr]
                bytes_read += len(data)
                return data

        def counting_open(self: Path, *args: object, **kwargs: object) -> CountingStream:
            return CountingStream(original_open(self, *args, **kwargs))

        monkeypatch.setattr(Path, "open", counting_open)
        assert _last_valid_event_position(path) == (10_000, "2026-06-06T00:00:010000.000")
        assert bytes_read < path.stat().st_size // 100
