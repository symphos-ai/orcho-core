"""Unit tests for the pure run-state reducer."""
from __future__ import annotations

import copy

from pipeline.run_state import RunStateSnapshot, RunStatus, apply_run_event


def _event(kind: str, seq: int = 1, **payload: object) -> dict:
    return {"seq": seq, "ts": "t", "kind": kind, "phase": None, "payload": payload}


def test_run_start_sets_running() -> None:
    out = apply_run_event(
        RunStateSnapshot.initial(),
        _event("run.start", task="t", run_kind="single_project"),
    )
    assert out.status is RunStatus.RUNNING
    assert not out.terminal


def test_run_started_alias_sets_running() -> None:
    out = apply_run_event(RunStateSnapshot.initial(), _event("run.started"))
    assert out.status is RunStatus.RUNNING


def test_phase_start_records_seen_and_active() -> None:
    out = apply_run_event(
        RunStateSnapshot.initial(), _event("phase.start", title="PLAN")
    )
    assert out.seen_phases == ("PLAN",)
    assert out.active_phase == "PLAN"


def test_phase_end_completed_clears_active() -> None:
    started = apply_run_event(
        RunStateSnapshot.initial(), _event("phase.start", title="PLAN")
    )
    out = apply_run_event(
        started, _event("phase.end", seq=2, title="PLAN", outcome="ok")
    )
    assert out.completed_phases == ("PLAN",)
    assert out.failed_phases == ()
    assert out.active_phase is None


def test_phase_end_failed_outcome_goes_to_failed() -> None:
    started = apply_run_event(
        RunStateSnapshot.initial(), _event("phase.start", title="REVIEW")
    )
    out = apply_run_event(
        started, _event("phase.end", seq=2, title="REVIEW", outcome="rejected")
    )
    assert out.failed_phases == ("REVIEW",)
    assert out.completed_phases == ()


def test_phase_end_skipped_outcome_is_completed() -> None:
    started = apply_run_event(
        RunStateSnapshot.initial(), _event("phase.start", title="REVIEW")
    )
    out = apply_run_event(
        started,
        _event(
            "phase.end", seq=2, title="REVIEW",
            outcome="skipped: review clean",
        ),
    )
    assert out.completed_phases == ("REVIEW",)
    assert out.failed_phases == ()
    assert out.active_phase is None


def test_phase_end_halted_outcome_not_completed() -> None:
    started = apply_run_event(
        RunStateSnapshot.initial(), _event("phase.start", title="IMPLEMENT")
    )
    out = apply_run_event(
        started,
        _event(
            "phase.end", seq=2, title="IMPLEMENT",
            outcome="halted: manual stop",
        ),
    )
    # A halted phase has a phase.end but is NOT a completed checkpoint:
    # neither completed nor failed; it stays only in seen_phases.
    assert out.completed_phases == ()
    assert out.failed_phases == ()
    assert "IMPLEMENT" in out.seen_phases
    assert out.active_phase is None


def test_phase_end_unknown_outcome_is_neither() -> None:
    for token in ("operator_handoff_required", "incomplete", "no_verdict",
                  "DONE", "some unknown future token"):
        started = apply_run_event(
            RunStateSnapshot.initial(), _event("phase.start", title="PLAN")
        )
        out = apply_run_event(
            started, _event("phase.end", seq=2, title="PLAN", outcome=token)
        )
        assert out.completed_phases == (), token
        assert out.failed_phases == (), token
        assert "PLAN" in out.seen_phases


def test_handoff_requested_sets_active_and_history() -> None:
    out = apply_run_event(
        RunStateSnapshot.initial(),
        _event(
            "phase.handoff_requested",
            handoff_id="h1",
            phase="validate_plan",
        ),
    )
    assert out.status is RunStatus.AWAITING_PHASE_HANDOFF
    assert out.active_handoff_id == "h1"
    assert out.active_handoff_phase == "validate_plan"
    assert out.seen_handoff_ids == ("h1",)


def test_handoff_decided_halt_preserves_seen_handoff_ids() -> None:
    requested = apply_run_event(
        RunStateSnapshot.initial(),
        _event("phase.handoff_requested", handoff_id="h1", phase="validate_plan"),
    )
    out = apply_run_event(
        requested, _event("phase_handoff.decided", seq=2, action="halt")
    )
    assert out.status is RunStatus.HALTED
    assert out.terminal
    assert out.active_handoff_id is None
    assert out.active_handoff_phase is None
    # F2: history must survive the halt.
    assert out.seen_handoff_ids == ("h1",)


def test_handoff_decided_non_halt_is_noop_status() -> None:
    requested = apply_run_event(
        RunStateSnapshot.initial(),
        _event("phase.handoff_requested", handoff_id="h1", phase="validate_plan"),
    )
    out = apply_run_event(
        requested, _event("phase_handoff.decided", seq=2, action="continue")
    )
    assert out.status is RunStatus.AWAITING_PHASE_HANDOFF
    assert out.active_handoff_id == "h1"


def test_run_end_done() -> None:
    out = apply_run_event(RunStateSnapshot.initial(), _event("run.end", status="done"))
    assert out.status is RunStatus.DONE
    assert out.terminal


def test_run_end_failed() -> None:
    out = apply_run_event(
        RunStateSnapshot.initial(), _event("run.end", status="failed")
    )
    assert out.status is RunStatus.FAILED
    assert out.terminal


def test_run_end_halted() -> None:
    out = apply_run_event(
        RunStateSnapshot.initial(), _event("run.end", status="halted")
    )
    assert out.status is RunStatus.HALTED
    assert out.terminal


def test_run_end_unknown_status_is_conservative() -> None:
    out = apply_run_event(
        RunStateSnapshot.initial(), _event("run.end", seq=7, status="weird")
    )
    assert out.status is RunStatus.UNKNOWN
    assert not out.terminal
    assert out.seq == 7


def test_run_interrupted() -> None:
    out = apply_run_event(RunStateSnapshot.initial(), _event("run.interrupted"))
    assert out.status is RunStatus.INTERRUPTED
    assert not out.terminal


def test_run_halted() -> None:
    out = apply_run_event(RunStateSnapshot.initial(), _event("run.halted"))
    assert out.status is RunStatus.HALTED
    assert out.terminal


def test_unknown_kind_is_ignored() -> None:
    base = apply_run_event(RunStateSnapshot.initial(), _event("run.start"))
    out = apply_run_event(base, _event("totally.unknown", seq=99, foo="bar"))
    # No domain change and no seq advance for an uninterpretable event.
    assert out is base


def test_apply_run_event_does_not_mutate_inputs() -> None:
    snapshot = apply_run_event(
        RunStateSnapshot.initial(),
        _event("phase.handoff_requested", handoff_id="h1", phase="validate_plan"),
    )
    snapshot_copy = copy.deepcopy(snapshot)
    event = _event("phase_handoff.decided", seq=2, action="halt")
    event_copy = copy.deepcopy(event)

    result = apply_run_event(snapshot, event)

    # Input snapshot unchanged.
    assert snapshot == snapshot_copy
    assert snapshot.seen_handoff_ids == ("h1",)
    assert snapshot.active_handoff_id == "h1"
    # Input event dict unchanged.
    assert event == event_copy
    # Result is a distinct, new object.
    assert result is not snapshot
    assert result.status is RunStatus.HALTED
