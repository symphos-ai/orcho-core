"""Liveness ages the timestamps the run's own event writer produces (J1).

``core.observability.events`` has always written naive local wall-clock
stamps (``2026-08-28T18:44:46.234``). The liveness reader required an
offset-aware value and degraded anything else to ``UNKNOWN``, so on every
real run ``progress_state`` was UNKNOWN, ``durable_progress_is_stale`` was
False, and ``dead_process_with_stale_progress`` — the predicate `orcho
run-diagnose` and the repair boundary both key on — could never become True.
The detector was inert against its own producer: a run whose process had
been gone for hours still classified as ``active``.

These tests drive the reader from the *real writer* rather than a
hand-written offset-aware fixture, which is what let the gap survive.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.observability.events import append_event
from pipeline.run_state.liveness import (
    DurableProgressState,
    read_liveness_facts,
)


def _write_launch(run_dir: Path, *, pid: int, age_hours: float) -> None:
    started = datetime.now(UTC) - timedelta(hours=age_hours)
    (run_dir / "run_supervisor.json").write_text(
        json.dumps({"pid": pid, "started_at": started.isoformat()}),
        encoding="utf-8",
    )


def test_writer_stamped_progress_is_aged_not_unknown(tmp_path: Path) -> None:
    """The event writer's own format must produce a usable progress fact."""
    _write_launch(tmp_path, pid=123, age_hours=2)
    append_event(tmp_path, "agent.tool_use", {})

    facts = read_liveness_facts(tmp_path, pid_probe=lambda _pid: False, grace_seconds=60)

    assert facts.last_event_kind == "agent.tool_use"
    assert facts.last_event_at is not None
    assert facts.progress_state is DurableProgressState.FRESH


def test_a_dead_process_with_writer_stamped_stale_progress_is_detected(
    tmp_path: Path,
) -> None:
    """The predicate diagnosis and repair key on must reach True in practice."""
    _write_launch(tmp_path, pid=2_147_480_000, age_hours=8)
    append_event(tmp_path, "agent.tool_use", {})

    facts = read_liveness_facts(
        # grace of 0.0 is rejected as unusable; age the one real event instead
        # by asking the question from far enough in the future.
        tmp_path,
        pid_probe=lambda _pid: False,
        grace_seconds=60,
        now=datetime.now(UTC) + timedelta(hours=8),
    )

    assert facts.progress_state is DurableProgressState.STALE
    assert facts.durable_progress_is_stale
    assert facts.dead_process_with_stale_progress


def test_a_terminal_event_still_blocks_the_stall_verdict(tmp_path: Path) -> None:
    """A run that recorded its end is finished, however old the stream is."""
    _write_launch(tmp_path, pid=2_147_480_000, age_hours=8)
    append_event(tmp_path, "agent.tool_use", {})
    append_event(tmp_path, "run.end", {})

    facts = read_liveness_facts(
        tmp_path,
        pid_probe=lambda _pid: False,
        grace_seconds=60,
        now=datetime.now(UTC) + timedelta(hours=8),
    )

    assert facts.has_terminal_event
    assert not facts.dead_process_with_stale_progress


def test_an_unparseable_event_stamp_is_still_unknown(tmp_path: Path) -> None:
    """Leniency is for the writer's format, not for junk."""
    _write_launch(tmp_path, pid=123, age_hours=2)
    (tmp_path / "events.jsonl").write_text(
        json.dumps({"seq": 1, "ts": "not-a-time", "kind": "agent.text"}) + "\n",
        encoding="utf-8",
    )

    facts = read_liveness_facts(tmp_path, pid_probe=lambda _pid: False, grace_seconds=60)

    assert facts.last_event_at is None
    assert facts.progress_state is DurableProgressState.UNKNOWN
    assert not facts.dead_process_with_stale_progress
