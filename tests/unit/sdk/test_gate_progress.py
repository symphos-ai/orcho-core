"""Unit tests for sdk/gate_progress.py (ADR 0190).

The SDK reader is an artifact-only projection over ``events.jsonl``. It returns
the LATEST invocation's live snapshot, or ``None`` for a run with no progress
data, a settled latest invocation, or a terminal run — it never advertises a
finished command as running. These build the event stream directly (via
``append_event``) so the projection is tested in isolation from the producer.
"""
from __future__ import annotations

from pathlib import Path

from core.observability.events import append_event
from sdk.gate_progress import GateProgressSnapshot, read_active_gate_progress


def _run(tmp_path: Path, run_id: str = "20260101_000000") -> tuple[Path, str]:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    return runs_dir, run_id


def _progress(
    run_dir: Path, *, invocation_id: str, name: str = "unit",
    phase: str = "IMPLEMENT", elapsed_s: float = 1.0, has_output: bool = True,
    stdout_tail: str = "", stderr_tail: str = "", last_output_at: str | None = None,
) -> None:
    payload = {
        "name": name,
        "invocation_id": invocation_id,
        "elapsed_s": elapsed_s,
        "has_output": has_output,
        "hook": "after_phase",
    }
    if stdout_tail:
        payload["stdout_tail"] = stdout_tail
    if stderr_tail:
        payload["stderr_tail"] = stderr_tail
    if last_output_at is not None:
        payload["last_output_at"] = last_output_at
    append_event(run_dir, "gate.progress", payload, phase=phase)


def _read(runs_dir: Path, run_id: str) -> GateProgressSnapshot | None:
    return read_active_gate_progress(run_id, runs_dir=str(runs_dir))


# ── active projection ─────────────────────────────────────────────────────


def test_active_gate_snapshot_projects_the_running_command(tmp_path: Path) -> None:
    runs_dir, run_id = _run(tmp_path)
    run_dir = runs_dir / run_id
    append_event(
        run_dir, "gate.start",
        {"name": "unit", "gate_kind": "scheduled", "invocation_id": "inv-1"},
        phase="IMPLEMENT",
    )
    _progress(
        run_dir, invocation_id="inv-1", elapsed_s=3.5,
        stdout_tail="OUTMARKER\n...", stderr_tail="ERRMARKER\n...",
        last_output_at="2026-01-01T00:00:03+00:00",
    )

    snap = _read(runs_dir, run_id)
    assert snap is not None
    assert snap.command == "unit"
    assert snap.hook == "after_phase"
    assert snap.phase == "IMPLEMENT"
    assert snap.invocation_id == "inv-1"
    assert snap.execution_state == "running"
    assert snap.exit_code is None
    assert snap.outcome is None
    assert snap.elapsed_s == 3.5
    assert "OUTMARKER" in snap.stdout_tail
    assert "ERRMARKER" in snap.stderr_tail
    assert snap.has_output is True
    assert snap.last_output_at == "2026-01-01T00:00:03+00:00"
    # started_at is paired from the gate.start boundary of the same invocation.
    assert snap.started_at


def test_latest_progress_event_wins(tmp_path: Path) -> None:
    runs_dir, run_id = _run(tmp_path)
    run_dir = runs_dir / run_id
    _progress(run_dir, invocation_id="inv-1", elapsed_s=1.0, stdout_tail="early")
    _progress(run_dir, invocation_id="inv-1", elapsed_s=9.0, stdout_tail="late")

    snap = _read(runs_dir, run_id)
    assert snap is not None
    assert snap.elapsed_s == 9.0
    assert snap.stdout_tail == "late"


# ── None cases ────────────────────────────────────────────────────────────


def test_historical_run_without_progress_is_readable(tmp_path: Path) -> None:
    """Old runs (pre ADR 0190) have gate.start/gate.end but no gate.progress."""
    runs_dir, run_id = _run(tmp_path)
    run_dir = runs_dir / run_id
    append_event(run_dir, "gate.start", {"name": "unit", "gate_kind": "scheduled"})
    append_event(
        run_dir, "gate.end",
        {"name": "unit", "outcome": "passed", "duration_s": 2.0},
    )
    assert _read(runs_dir, run_id) is None


def test_empty_run_returns_none(tmp_path: Path) -> None:
    runs_dir, run_id = _run(tmp_path)
    assert _read(runs_dir, run_id) is None


def test_settled_invocation_is_not_running(tmp_path: Path) -> None:
    runs_dir, run_id = _run(tmp_path)
    run_dir = runs_dir / run_id
    _progress(run_dir, invocation_id="inv-1", stdout_tail="done")
    append_event(
        run_dir, "gate.end",
        {"name": "unit", "outcome": "passed", "duration_s": 2.0,
         "invocation_id": "inv-1"},
    )
    assert _read(runs_dir, run_id) is None


def test_terminal_run_never_advertises_a_running_gate(tmp_path: Path) -> None:
    runs_dir, run_id = _run(tmp_path)
    run_dir = runs_dir / run_id
    _progress(run_dir, invocation_id="inv-1", stdout_tail="mid")
    # No gate.end for inv-1, but the run is over.
    append_event(run_dir, "run.end", {"status": "done"})
    assert _read(runs_dir, run_id) is None


# ── rerun isolation via distinct invocation_id ────────────────────────────


def test_rerun_isolation_projects_only_the_latest_invocation(tmp_path: Path) -> None:
    runs_dir, run_id = _run(tmp_path)
    run_dir = runs_dir / run_id
    # First execution settled.
    _progress(run_dir, invocation_id="inv-1", elapsed_s=5.0, stdout_tail="run-1")
    append_event(
        run_dir, "gate.end",
        {"name": "unit", "outcome": "failed", "duration_s": 5.0,
         "invocation_id": "inv-1"},
    )
    # Repair-loop rerun is a distinct invocation, still running.
    _progress(run_dir, invocation_id="inv-2", elapsed_s=2.0, stdout_tail="run-2")

    snap = _read(runs_dir, run_id)
    assert snap is not None
    assert snap.invocation_id == "inv-2"
    assert snap.stdout_tail == "run-2"
    assert snap.elapsed_s == 2.0


# ── no-output vs empty-completed distinction ──────────────────────────────


def test_no_output_yet_snapshot(tmp_path: Path) -> None:
    runs_dir, run_id = _run(tmp_path)
    run_dir = runs_dir / run_id
    _progress(run_dir, invocation_id="inv-1", has_output=False)

    snap = _read(runs_dir, run_id)
    assert snap is not None
    assert snap.has_output is False
    assert snap.last_output_at is None
    assert snap.stdout_tail == ""
    assert snap.stderr_tail == ""


def test_empty_completed_stream_is_settled_not_no_output(tmp_path: Path) -> None:
    """A stream that produced nothing then completed is settled (→ None), a
    different fact from 'no output yet' (a running snapshot with has_output
    False)."""
    runs_dir, run_id = _run(tmp_path)
    run_dir = runs_dir / run_id
    _progress(run_dir, invocation_id="inv-1", has_output=False)
    append_event(
        run_dir, "gate.end",
        {"name": "unit", "outcome": "passed", "duration_s": 0.1,
         "invocation_id": "inv-1"},
    )
    assert _read(runs_dir, run_id) is None


def test_resumed_run_exposes_new_invocation_after_old_run_end(tmp_path):
    runs_dir, run_id = _run(tmp_path)
    run_dir = runs_dir / run_id
    _progress(run_dir, invocation_id="old", stdout_tail="old")
    append_event(run_dir, "run.end", {"status": "halted"})
    append_event(run_dir, "run.start", {"status": "running"})
    _progress(run_dir, invocation_id="new", stdout_tail="new")
    snap = _read(runs_dir, run_id)
    assert snap is not None and snap.invocation_id == "new"
    assert snap.stdout_tail == "new"
    append_event(run_dir, "run.end", {"status": "done"})
    assert _read(runs_dir, run_id) is None
