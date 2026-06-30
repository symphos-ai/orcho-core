# SPDX-License-Identifier: Apache-2.0
"""Live non-terminal stall diagnostics observable during a RUNNING phase (T1).

The non-terminal path (e.g. unsafe process polling) must be observable in
status / next_actions / evidence *while the phase is still running*, before
any terminal failure and without making the run failed. This exercises the
write-through emission of a non-terminal ``agent.command_stalled`` event into a
running run's event-store and asserts the full live-observability chain:

* ``active_stall_diagnostics`` surfaces it from the event-store;
* ``compute_next_actions`` (via ``load_status``) projects a bounded, non-empty
  recovery set (interrupt the run's own subprocess group);
* the run's status stays ``running`` — no terminal failure;
* the default sink emits with ``terminal=False`` and never writes the session.
"""

from __future__ import annotations

import json
from pathlib import Path

from agents.stall_protocol import (
    EventStallDiagnosticSink,
    StalledCommand,
    StallReason,
)
from core.observability import events as _events
from sdk.evidence_slices import active_stall_diagnostics
from sdk.status import load_status


def _seed_running_run(runs_dir: Path, run_id: str = "20260624_running") -> Path:
    """A run dir whose meta.json reports a still-running phase."""
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "task": "demo",
        "status": "running",
        "profile": "feature",
        "phases": {"implement": [{"attempt": 1}]},
    }
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )
    return run_dir


def _emit_non_terminal_stall(run_dir: Path) -> None:
    """Write-through a non-terminal stall event into the running run's store."""
    payload = StalledCommand(
        phase="implement",
        elapsed_s=8.0,
        command_preview="kill -0 $(pgrep -f 'pytest -q -m')",
        output_tail="",
        reason=StallReason.UNSAFE_PROCESS_POLLING,
    ).event_payload(terminal=False)
    _events.append_event(run_dir, "agent.command_stalled", payload)


def test_live_non_terminal_stall_visible_in_diagnostics(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    run_dir = _seed_running_run(runs)
    _emit_non_terminal_stall(run_dir)

    diags = active_stall_diagnostics(run_dir)
    assert len(diags) == 1
    assert diags[0].terminal is False
    assert diags[0].reason == "unsafe_process_polling"
    assert "interrupt" in diags[0].recovery_actions


def test_live_stall_surfaces_in_status_next_actions(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    run_dir = _seed_running_run(runs)
    _emit_non_terminal_stall(run_dir)

    status = load_status("20260624_running", runs_dir=runs, cwd=None)

    # Run stays running — the live diagnostic never makes it terminal.
    assert status.meta is not None
    assert status.meta.status == "running"

    # next_actions carries a bounded, non-empty recovery set (interrupt).
    cancels = [a for a in status.next_actions if a.tool == "orcho_run_cancel"]
    assert len(cancels) == 1
    assert cancels[0].args == {"run_id": "20260624_running"}
    # The running run is NOT resumable — no resume / from-plan actions.
    assert all(a.tool == "orcho_run_cancel" for a in status.next_actions)


def test_default_sink_emits_non_terminal_without_session(tmp_path: Path) -> None:
    """The default sink emits ``terminal=False`` and is session-free."""
    run_dir = tmp_path / "run"
    _events.init_event_store(run_dir)
    try:
        sink = EventStallDiagnosticSink()
        sink.record(
            StalledCommand(
                phase="implement",
                elapsed_s=3.0,
                command_preview="pgrep -f 'pytest -q -m'",
                output_tail="",
                reason=StallReason.UNSAFE_PROCESS_POLLING,
            )
        )
    finally:
        _events.init_event_store(None)

    events = _events.read_all(run_dir)
    stall = [e for e in events if e.kind == "agent.command_stalled"]
    assert len(stall) == 1
    assert stall[0].payload["terminal"] is False
    # No run.end / failure event — the sink only records a diagnostic.
    assert not [e for e in events if e.kind == "run.end"]
    # And the live projector reads exactly this one diagnostic back.
    assert len(active_stall_diagnostics(run_dir)) == 1
