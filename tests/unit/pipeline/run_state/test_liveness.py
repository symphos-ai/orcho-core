"""Focused coverage for the read-only run liveness facts boundary."""
from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pipeline.run_state.liveness import (
    DurableProgressState,
    LaunchState,
    PidState,
    read_liveness_facts,
)

_NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


def _write_launch(run_dir: Path, **values: object) -> None:
    (run_dir / "run_supervisor.json").write_text(json.dumps(values), encoding="utf-8")


def _write_events(run_dir: Path, *events: dict[str, object]) -> None:
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )


def _event(*, ts: datetime, kind: str = "phase.start") -> dict[str, object]:
    return {"seq": 1, "ts": ts.isoformat(), "kind": kind, "payload": {}}


def test_pid_probe_alive_dead_and_unknown_are_explicit(tmp_path: Path) -> None:
    _write_launch(tmp_path, pid=123, started_at=(_NOW - timedelta(minutes=3)).isoformat())

    alive = read_liveness_facts(tmp_path, pid_probe=lambda _pid: True, now=_NOW, grace_seconds=60)
    dead = read_liveness_facts(tmp_path, pid_probe=lambda _pid: False, now=_NOW, grace_seconds=60)
    unknown = read_liveness_facts(
        tmp_path, pid_probe=lambda _pid: (_ for _ in ()).throw(OSError("no probe")),
        now=_NOW, grace_seconds=60,
    )

    assert alive.pid_state is PidState.ALIVE
    assert dead.pid_state is PidState.DEAD
    assert unknown.pid_state is PidState.UNKNOWN


@pytest.mark.parametrize(("payload", "pid_state"), [
    ({"pid": True, "started_at": "2026-08-24T10:00:00Z"}, PidState.UNKNOWN),
    ({"pid": "123", "started_at": "2026-08-24T10:00:00Z"}, PidState.UNKNOWN),
    ({"pid": -1, "started_at": "not-a-time"}, PidState.UNKNOWN),
    # A valid PID is independently observable, but malformed launch time must
    # still prevent absent history from becoming a stale-progress conclusion.
    ({"pid": 123, "started_at": "2026-08-24T10:00:00"}, PidState.DEAD),
])
def test_malformed_launch_values_are_unknown_no_ops(
    tmp_path: Path, payload: dict[str, object], pid_state: PidState,
) -> None:
    _write_launch(tmp_path, **payload)

    facts = read_liveness_facts(tmp_path, pid_probe=lambda _pid: False, now=_NOW, grace_seconds=60)

    assert facts.launch_state is LaunchState.PRESENT
    assert facts.pid_state is pid_state
    assert facts.dead_process_with_stale_progress is False


def test_malformed_or_unreadable_launch_state_is_unknown(tmp_path: Path) -> None:
    (tmp_path / "run_supervisor.json").write_text("[not an object]", encoding="utf-8")

    facts = read_liveness_facts(tmp_path, now=_NOW, grace_seconds=60)

    assert facts.launch_state is LaunchState.UNKNOWN
    assert facts.pid is None
    assert facts.pid_state is PidState.UNKNOWN


def test_absent_events_remain_distinct_but_old_launch_makes_progress_stale(tmp_path: Path) -> None:
    _write_launch(tmp_path, pid=123, started_at=(_NOW - timedelta(minutes=3)).isoformat())

    facts = read_liveness_facts(tmp_path, pid_probe=lambda _pid: False, now=_NOW, grace_seconds=60)

    assert facts.progress_state is DurableProgressState.ABSENT
    assert facts.durable_progress_is_stale is True
    assert facts.dead_process_with_stale_progress is True


def test_recent_and_stale_events_are_distinguished(tmp_path: Path) -> None:
    _write_events(tmp_path, _event(ts=_NOW - timedelta(seconds=30)))
    recent = read_liveness_facts(tmp_path, now=_NOW, grace_seconds=60)

    _write_events(tmp_path, _event(ts=_NOW - timedelta(seconds=61)))
    stale = read_liveness_facts(tmp_path, now=_NOW, grace_seconds=60)

    assert recent.progress_state is DurableProgressState.FRESH
    assert recent.durable_progress_is_stale is False
    assert stale.progress_state is DurableProgressState.STALE
    assert stale.durable_progress_is_stale is True


@pytest.mark.parametrize("kind", ["run.end", "run.interrupted"])
def test_terminal_event_blocks_dead_process_stall_even_when_progress_is_stale(
    tmp_path: Path, kind: str,
) -> None:
    _write_launch(tmp_path, pid=123, started_at=(_NOW - timedelta(minutes=3)).isoformat())
    _write_events(tmp_path, _event(ts=_NOW - timedelta(minutes=2), kind=kind))

    facts = read_liveness_facts(tmp_path, pid_probe=lambda _pid: False, now=_NOW, grace_seconds=60)

    assert facts.terminal_event_kind == kind
    assert facts.has_terminal_event is True
    assert facts.dead_process_with_stale_progress is False


def test_liveness_is_read_only_and_does_not_import_sdk(tmp_path: Path) -> None:
    _write_launch(tmp_path, pid=123, started_at=(_NOW - timedelta(minutes=3)).isoformat())
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    read_liveness_facts(tmp_path, pid_probe=lambda _pid: False, now=_NOW, grace_seconds=60)

    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    source = Path(__import__("pipeline.run_state.liveness", fromlist=["_"]).__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not any(
        (isinstance(node, ast.Import) and any(alias.name == "sdk" or alias.name.startswith("sdk.") for alias in node.names))
        or (isinstance(node, ast.ImportFrom) and node.module and (node.module == "sdk" or node.module.startswith("sdk.")))
        for node in ast.walk(tree)
    )
