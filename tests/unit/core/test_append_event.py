"""P2.5 — ``core.observability.events.append_event`` contract tests.

The supervisor in orcho-mcp uses this to write into a run's events.jsonl
from a different process than the one that originated the run. Tests
exercise: empty-file initial seq, sequential numbering, payload cleaning,
concurrent-append safety on POSIX.
"""
from __future__ import annotations

import json
from pathlib import Path

from core.observability.events import append_event


def _read_events(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_first_event_assigns_seq_1(tmp_path):
    seq = append_event(tmp_path, "run.start", {"task": "hello"})
    assert seq == 1
    events = _read_events(tmp_path)
    assert len(events) == 1
    assert events[0]["seq"] == 1
    assert events[0]["kind"] == "run.start"
    assert events[0]["payload"]["task"] == "hello"


def test_subsequent_events_increment_seq(tmp_path):
    append_event(tmp_path, "run.start")
    append_event(tmp_path, "phase.start", {"name": "plan"}, phase="plan")
    seq = append_event(tmp_path, "run.end", {"status": "done"})
    assert seq == 3
    events = _read_events(tmp_path)
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert events[1]["phase"] == "plan"


def test_continues_after_existing_events(tmp_path):
    """Picking up an existing events.jsonl: max_seq + 1."""
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"seq": 5, "ts": "2026-05-06T12:00:00.000", "kind": "run.start", "phase": null, "payload": {}}\n'
        '{"seq": 6, "ts": "2026-05-06T12:00:01.000", "kind": "phase.start", "phase": "plan", "payload": {}}\n',
        encoding="utf-8",
    )
    seq = append_event(tmp_path, "phase.end", {"name": "plan"})
    assert seq == 7


def test_creates_run_dir_if_missing(tmp_path):
    target = tmp_path / "new_run_dir"
    assert not target.exists()
    append_event(target, "run.orphaned", {"reason": "test"})
    assert (target / "events.jsonl").is_file()


def test_payload_cleaning_drops_none(tmp_path):
    """``None`` values are dropped from payload (matches emit() rules)."""
    append_event(tmp_path, "test", {"keep": "yes", "drop": None})
    events = _read_events(tmp_path)
    assert events[0]["payload"] == {"keep": "yes"}


def test_payload_truncation_long_string(tmp_path):
    huge = "x" * 17000
    append_event(tmp_path, "test", {"data": huge})
    events = _read_events(tmp_path)
    assert len(events[0]["payload"]["data"]) == 16384
    assert events[0]["payload"]["_data_truncated"] == 17000


def test_tolerates_partial_last_line(tmp_path):
    """A writer crash mid-line shouldn't break next append."""
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"seq": 1, "ts": "2026-05-06T12:00:00.000", "kind": "run.start", "phase": null, "payload": {}}\n'
        '{"seq": 2, "ts": "2026-05',  # truncated — writer died
        encoding="utf-8",
    )
    seq = append_event(tmp_path, "run.recovered")
    # max_seq = 1 (truncated line skipped); next seq = 2
    assert seq == 2


def test_concurrent_appends_have_distinct_seq(tmp_path):
    """Two threads appending shouldn't end up with duplicate seq numbers.

 This is the file-lock contract — under fcntl.flock POSIX exclusive lock,
 seq assignment is serialised even across multiple writers.
 """
    import threading

    def writer(n_each):
        for i in range(n_each):
            append_event(tmp_path, "test", {"i": i})

    threads = [threading.Thread(target=writer, args=(20,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = _read_events(tmp_path)
    seqs = [e["seq"] for e in events]
    assert len(seqs) == 80
    assert len(set(seqs)) == 80, f"duplicate seqs: {[s for s in seqs if seqs.count(s) > 1]}"
    assert sorted(seqs) == list(range(1, 81))
