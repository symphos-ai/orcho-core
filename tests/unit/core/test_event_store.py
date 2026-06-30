"""
JSONL event-store correctness.

Targets:
 1. init_event_store creates the file, resets seq.
 2. emit() is no-op when not initialized.
 3. Schema fields (seq, ts, kind, phase, payload) round-trip via read_all.
 4. Concurrent emit() from multiple threads never interleaves lines.
 5. tail() yields new events past since_seq and skips partial last lines.
 6. set_phase tags subsequent events; unset → phase=None.
 7. Long string fields are truncated with marker.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from core.observability import events as evstore


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Each test starts with a clean event-store. The module uses globals
 (one writer per process), so we explicitly reset between tests."""
    evstore.init_event_store(None)
    yield
    evstore.init_event_store(None)


def test_emit_is_noop_without_init(tmp_path: Path):
    # Without init, emit silently swallows.
    evstore.emit("phase.start", phase="PLAN")
    # Nothing was written anywhere.
    assert not list(tmp_path.iterdir())


def test_init_creates_file_resets_seq(tmp_path: Path):
    p = evstore.init_event_store(tmp_path)
    assert p == tmp_path / "events.jsonl"
    assert p.exists()
    assert p.read_text() == ""

    evstore.emit("run.start", task="hello")
    evstore.emit("run.end", status="success")

    lines = p.read_text().splitlines()
    assert len(lines) == 2
    e0 = json.loads(lines[0])
    e1 = json.loads(lines[1])
    assert e0["seq"] == 1 and e1["seq"] == 2
    assert e0["kind"] == "run.start" and e1["kind"] == "run.end"


def test_read_all_roundtrip(tmp_path: Path):
    evstore.init_event_store(tmp_path)
    evstore.set_phase("PLAN")
    evstore.emit("phase.start", title="PLAN")
    evstore.set_phase(None)
    evstore.emit("agent.text", text="hello world")

    events = evstore.read_all(tmp_path)
    assert [e.seq for e in events] == [1, 2]
    assert events[0].kind == "phase.start"
    assert events[0].phase == "PLAN"
    assert events[0].payload == {"title": "PLAN"}
    assert events[1].phase is None
    assert events[1].payload == {"text": "hello world"}


def test_read_all_missing_returns_empty(tmp_path: Path):
    assert evstore.read_all(tmp_path) == []


def test_concurrent_emit_no_interleave(tmp_path: Path):
    """100 threads × 10 emits = 1000 events. Lines must be valid JSON,
 seq must be a permutation of 1..1000."""
    evstore.init_event_store(tmp_path)

    def worker(tid: int):
        for i in range(10):
            evstore.emit("agent.text", text=f"t{tid}-i{i}", thread=tid)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = evstore.read_all(tmp_path)
    assert len(events) == 1000
    seqs = sorted(e.seq for e in events)
    assert seqs == list(range(1, 1001))
    # Every event has the expected keys (no torn writes)
    for e in events:
        assert e.kind == "agent.text"
        assert "text" in e.payload and "thread" in e.payload


def test_tail_yields_new_events(tmp_path: Path):
    evstore.init_event_store(tmp_path)
    evstore.emit("phase.start", title="PLAN")    # seq 1
    evstore.emit("agent.text", text="first")     # seq 2

    stop = threading.Event()
    seen: list[evstore.Event] = []

    def consumer():
        for ev in evstore.tail(tmp_path, since_seq=1, poll=0.05,
                               stop_predicate=stop.is_set):
            seen.append(ev)
            if len(seen) >= 2:
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    # Let the tail loop start polling
    time.sleep(0.1)
    evstore.emit("agent.text", text="second")    # seq 3

    t.join(timeout=2.0)
    assert not t.is_alive(), "tail did not stop after stop_predicate"
    seqs = [e.seq for e in seen]
    assert seqs == [2, 3]
    assert seen[1].payload["text"] == "second"


def test_tail_skips_partial_last_line(tmp_path: Path):
    """A torn write (writer crashed mid-line) must not break tail."""
    evstore.init_event_store(tmp_path)
    evstore.emit("phase.start", title="PLAN")    # seq 1

    p = tmp_path / "events.jsonl"
    # Append a partial line (no closing brace, no newline)
    with p.open("a") as f:
        f.write('{"seq": 99, "kind": "broken"')

    stop = threading.Event()
    seen: list[evstore.Event] = []

    def consumer():
        for ev in evstore.tail(tmp_path, since_seq=0, poll=0.05,
                               stop_predicate=stop.is_set):
            seen.append(ev)
            if len(seen) >= 1:
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    t.join(timeout=2.0)

    # Only the well-formed event is yielded; partial line is silently skipped.
    assert [e.seq for e in seen] == [1]


def test_payload_truncation(tmp_path: Path):
    evstore.init_event_store(tmp_path)
    huge = "x" * 20000
    evstore.emit("agent.text", text=huge)
    [e] = evstore.read_all(tmp_path)
    assert len(e.payload["text"]) == 16384
    assert e.payload["_text_truncated"] == 20000


def test_payload_drops_none(tmp_path: Path):
    evstore.init_event_store(tmp_path)
    evstore.emit("agent.tool_use", tool_name="Bash", command="ls", error=None)
    [e] = evstore.read_all(tmp_path)
    assert e.payload == {"tool_name": "Bash", "command": "ls"}
    assert "error" not in e.payload


def test_init_truncates_existing(tmp_path: Path):
    """Calling init twice on the same dir resets the file (new run with
 same timestamp shouldn't happen in practice, but be defensive)."""
    evstore.init_event_store(tmp_path)
    evstore.emit("phase.start", title="PLAN")
    assert (tmp_path / "events.jsonl").read_text() != ""

    evstore.init_event_store(tmp_path)
    assert (tmp_path / "events.jsonl").read_text() == ""
    evstore.emit("phase.start", title="REPLAY")
    [e] = evstore.read_all(tmp_path)
    assert e.seq == 1 and e.payload["title"] == "REPLAY"


def test_current_run_dir_tracks_init(tmp_path: Path):
    assert evstore.current_run_dir() is None
    evstore.init_event_store(tmp_path)
    assert evstore.current_run_dir() == tmp_path
    evstore.init_event_store(None)
    assert evstore.current_run_dir() is None


# ── resume mode ──────────────────────────────────────────────────────────────


class TestResumeMode:
    """init_event_store(resume=True) preserves prior events and continues
 seq numbering. Used by the orchestrator's --resume path so the
 parent run's phase.start / validate_plan.verdict / gate_blocked events
 survive into the resumed run's events.jsonl."""

    def test_resume_preserves_prior_events(self, tmp_path: Path) -> None:
        evstore.init_event_store(tmp_path)
        evstore.emit("phase.start", title="PLAN")
        evstore.emit("phase.end",   title="PLAN", outcome="rejected")
        evstore.init_event_store(None)   # close current handle

        evstore.init_event_store(tmp_path, resume=True)
        evstore.emit("phase.start", title="BUILD")
        evs = evstore.read_all(tmp_path)
        kinds = [e.kind for e in evs]
        assert kinds == ["phase.start", "phase.end", "phase.start"]
        titles = [e.payload.get("title") for e in evs]
        assert titles == ["PLAN", "PLAN", "BUILD"]

    def test_resume_continues_seq(self, tmp_path: Path) -> None:
        evstore.init_event_store(tmp_path)
        evstore.emit("phase.start", title="A")
        evstore.emit("phase.start", title="B")
        evstore.emit("phase.start", title="C")
        evstore.init_event_store(None)

        evstore.init_event_store(tmp_path, resume=True)
        evstore.emit("phase.start", title="D")
        evstore.emit("phase.start", title="E")
        evs = evstore.read_all(tmp_path)
        assert [e.seq for e in evs] == [1, 2, 3, 4, 5]
        assert [e.payload["title"] for e in evs] == ["A", "B", "C", "D", "E"]

    def test_resume_on_empty_file_starts_fresh(self, tmp_path: Path) -> None:
        # Empty events.jsonl (or no file) → resume behaves like a fresh init.
        (tmp_path / "events.jsonl").write_text("", encoding="utf-8")
        evstore.init_event_store(tmp_path, resume=True)
        evstore.emit("phase.start", title="FIRST")
        [e] = evstore.read_all(tmp_path)
        assert e.seq == 1

    def test_default_init_still_truncates(self, tmp_path: Path) -> None:
        """Default behaviour unchanged — the explicit truncation guarantee
 callers rely on for isolated tests must keep working."""
        evstore.init_event_store(tmp_path)
        evstore.emit("phase.start", title="OLD")
        evstore.init_event_store(None)

        # default kwargs → truncate, fresh seq.
        evstore.init_event_store(tmp_path)
        assert (tmp_path / "events.jsonl").read_text() == ""
        evstore.emit("phase.start", title="NEW")
        [e] = evstore.read_all(tmp_path)
        assert e.seq == 1
        assert e.payload["title"] == "NEW"
