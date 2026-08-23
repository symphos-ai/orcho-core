from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from core.io.bounded_proc import TimedOut, run_bounded
from core.observability import events
from pipeline.project.profile_dispatch import emit_phase_banner
from pipeline.project.startup_watchdog import (
    StartupWatchdog,
    arm_startup_watchdog,
    checkpoint_startup_watchdog,
    disarm_startup_watchdog,
    startup_watchdog_scope,
)


def _session() -> dict:
    return {"status": "running", "phases": {}, "task": "watchdog"}


def _events(run_dir: Path) -> list[dict]:
    return [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]


def test_sleeping_service_command_halts_with_durable_breadcrumb(tmp_path: Path) -> None:
    events.init_event_store(tmp_path)
    events.emit("run.start", run_kind="single_project", task="t", project="/p", profile="feature")
    started = time.monotonic()
    with startup_watchdog_scope(tmp_path) as watchdog:
        watchdog.budget_s = 0.04
        watchdog.arm()
        outcome = run_bounded(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout_s=5, reap_budget_s=0.2, cwd=str(tmp_path),
        )
        assert watchdog.checkpoint(_session()) is True
    elapsed = time.monotonic() - started

    assert isinstance(outcome, TimedOut)
    assert elapsed < 0.4
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["status"] == "halted"
    assert meta["halt_reason"] == "startup_stalled"
    assert meta["halt"]["phase"] == "startup"
    assert meta["halt"]["command"]["cwd"] == str(tmp_path)
    assert meta["halt"]["command"]["started_at"]
    assert meta["halt"]["budget_s"] == 0.04
    assert len([event for event in _events(tmp_path) if event["kind"] == "run.end"]) == 1
    assert [event["kind"] for event in _events(tmp_path)] == ["run.start", "run.end"]


def test_event_or_output_progress_or_first_phase_disarms_without_halt(tmp_path: Path) -> None:
    events.init_event_store(tmp_path)
    events.emit("run.start", run_kind="single_project", task="t", project="/p", profile="feature")
    with startup_watchdog_scope(tmp_path) as watchdog:
        watchdog.budget_s = 0.001
        watchdog.arm()
        (tmp_path / "output.log").write_text("progress")
        time.sleep(0.01)
        assert watchdog.checkpoint(_session()) is False
        assert watchdog.disarmed is True

    event_dir = tmp_path / "event"
    events.init_event_store(event_dir)
    events.emit("run.start", run_kind="single_project", task="t", project="/p", profile="feature")
    with startup_watchdog_scope(event_dir) as watchdog:
        watchdog.budget_s = 0.001
        watchdog.arm()
        events.emit("agent.text", text="setup made progress")
        time.sleep(0.01)
        assert watchdog.checkpoint(_session()) is False
        assert watchdog.disarmed is True

    phase_dir = tmp_path / "phase"
    events.init_event_store(phase_dir)
    events.emit("run.start", run_kind="single_project", task="t", project="/p", profile="feature")
    with startup_watchdog_scope(phase_dir) as watchdog:
        watchdog.arm()
        emit_phase_banner(
            "plan", SimpleNamespace(extras={}, phase_config=None), terminal=False,
        )
        assert watchdog.disarmed is True
    assert not (tmp_path / "meta.json").exists()


def test_module_helpers_are_inert_without_an_active_scope() -> None:
    """The helpers are called unconditionally from the setup sequence, so
    outside a watchdog scope they must be no-ops rather than errors."""
    arm_startup_watchdog()
    disarm_startup_watchdog()

    assert checkpoint_startup_watchdog({"status": "running"}) is False


def test_arming_is_idempotent_and_a_disarmed_watchdog_stays_closed(tmp_path: Path) -> None:
    watchdog = StartupWatchdog(tmp_path)
    watchdog.arm()
    first_deadline = watchdog.deadline
    watchdog.arm()

    assert watchdog.deadline == first_deadline

    watchdog.disarm()
    watchdog.arm()

    assert watchdog.deadline == first_deadline


def test_a_watchdog_without_a_run_dir_reports_no_progress_and_no_halt() -> None:
    """``output_dir`` is optional on the setup path; the watchdog must degrade
    to inert rather than crash on a missing run directory."""
    watchdog = StartupWatchdog(None)
    watchdog.arm()

    assert watchdog.checkpoint({"status": "running"}) is False


def test_checkpoint_without_a_session_cannot_halt(tmp_path: Path) -> None:
    """The halt is a durable session mutation; with no session there is nothing
    to write, and the watchdog must not pretend it halted."""
    watchdog = StartupWatchdog(tmp_path)
    watchdog.arm()
    watchdog.deadline = 0.0

    assert watchdog.checkpoint(None) is False
