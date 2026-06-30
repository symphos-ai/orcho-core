# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the stalled-command recovery slice and durable builder (T1).

Pins:

* the provider-neutral terminal failure builder
  (``pipeline.run_state.stalled_command``) — bounded fields + the consistent
  recovery verb set;
* ``sdk.evidence_slices.active_stall_diagnostics`` — live non-terminal
  diagnostics projected from emitted ``agent.command_stalled`` events;
* ``sdk.evidence_slices.list_stall_recovery`` — both sources (terminal +
  live non-terminal) behind one slice.
"""

from __future__ import annotations

from pathlib import Path

from agents.stall_protocol import StalledCommand, StallReason
from core.observability import events as _events
from core.observability.events import Event
from pipeline.run_state.stalled_command import (
    STALL_RECOVERY_VERBS,
    STALLED_COMMAND_FAILURE_KIND,
    build_stall_recovery_actions,
    build_stalled_command_failure,
)
from sdk.evidence_slices import (
    StalledCommandRecovery,
    active_stall_diagnostics,
)

# ── durable builder ──────────────────────────────────────────────────────────


def test_recovery_verbs_consistent_order() -> None:
    assert STALL_RECOVERY_VERBS == (
        "interrupt", "resume_from_checkpoint", "halt",
    )
    assert build_stall_recovery_actions() == [
        {"action": "interrupt"},
        {"action": "resume_from_checkpoint"},
        {"action": "halt"},
    ]


def test_build_failure_carries_bounded_fields() -> None:
    stalled = StalledCommand(
        phase="implement",
        elapsed_s=130.0,
        command_preview="pytest -q -m 'not e2e'",
        output_tail="…",
        reason=StallReason.SILENT_CHILD_COMMAND,
        process_group=4242,
    )
    failure = build_stalled_command_failure(stalled)
    assert failure["failure_kind"] == STALLED_COMMAND_FAILURE_KIND
    assert failure["failed_phase"] == "implement"
    assert failure["reason"] == "silent_child_command"
    assert failure["elapsed_s"] == 130.0
    assert failure["process_group"] == 4242
    assert failure["command_preview"] == "pytest -q -m 'not e2e'"
    # The durable recovery list matches the consistent verb set.
    assert [a["action"] for a in failure["recovery_actions"]] == list(
        STALL_RECOVERY_VERBS
    )


def test_build_failure_omits_empty_optional_fields() -> None:
    stalled = StalledCommand(
        phase="review_changes",
        elapsed_s=5.0,
        command_preview="",
        output_tail="",
        reason=StallReason.OUTPUT_INACTIVITY,
    )
    failure = build_stalled_command_failure(stalled)
    assert "command_preview" not in failure
    assert "output_tail" not in failure
    assert "process_group" not in failure


# ── live non-terminal projector ──────────────────────────────────────────────


def _stall_event(*, terminal: bool, seq: int = 1) -> Event:
    return Event(
        seq=seq,
        ts="2026-06-24T00:00:00.000",
        kind="agent.command_stalled",
        phase="IMPLEMENT",
        payload={
            "phase": "implement",
            "reason": "unsafe_process_polling",
            "elapsed_s": 12.0,
            "terminal": terminal,
            "command_preview": "kill -0 $(pgrep -f 'pytest -q -m')",
        },
    )


def test_active_stall_diagnostics_reads_non_terminal_only() -> None:
    events = [
        _stall_event(terminal=False, seq=1),
        _stall_event(terminal=True, seq=2),   # terminal escalation — excluded
        Event(seq=3, ts="t", kind="agent.text", phase=None, payload={}),
    ]
    diags = active_stall_diagnostics(events=events)
    assert len(diags) == 1
    diag = diags[0]
    assert isinstance(diag, StalledCommandRecovery)
    assert diag.source == "live_non_terminal"
    assert diag.terminal is False
    assert diag.phase == "implement"
    assert diag.reason == "unsafe_process_polling"
    # The projector fills in the consistent recovery verb set even when the
    # event payload omits it.
    assert diag.recovery_actions == tuple(STALL_RECOVERY_VERBS)


def test_active_stall_diagnostics_reads_from_disk(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _events.init_event_store(run_dir)
    sink_payload = StalledCommand(
        phase="implement",
        elapsed_s=9.0,
        command_preview="pgrep -f 'pytest -q -m'",
        output_tail="",
        reason=StallReason.UNSAFE_PROCESS_POLLING,
    ).event_payload(terminal=False)
    _events.emit("agent.command_stalled", **sink_payload)
    _events.init_event_store(None)  # detach store

    diags = active_stall_diagnostics(run_dir)
    assert len(diags) == 1
    assert diags[0].reason == "unsafe_process_polling"
    assert diags[0].command_preview == "pgrep -f 'pytest -q -m'"


def test_active_stall_diagnostics_empty_when_no_events(tmp_path: Path) -> None:
    assert active_stall_diagnostics(tmp_path / "missing") == []
