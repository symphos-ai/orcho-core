"""Unit tests for the pure terminal-state writers (Stage 3b).

Pins the load-bearing contract: ``done`` / ``halted`` clear a stale active
``phase_handoff``; ``failed`` / ``interrupted`` preserve it; the
``phase_handoff_halt`` shape matches what :mod:`pipeline.run_state.repair`
heals a torn halt to; and every helper is a pure in-place mutation (no IO).
"""
from __future__ import annotations

from pipeline.run_state import (
    mark_run_done,
    mark_run_failed,
    mark_run_halted,
    mark_run_interrupted,
)
from pipeline.run_state.terminal import (
    mark_run_done as mark_run_done_direct,
)


def _state_with_handoff() -> dict:
    return {
        "status": "awaiting_phase_handoff",
        "phase_handoff": {"id": "h1", "phase": "validate_plan"},
    }


def test_mark_run_done_clears_phase_handoff() -> None:
    state = _state_with_handoff()
    mark_run_done(state)
    assert state["status"] == "done"
    assert "phase_handoff" not in state
    # halt_reason is never introduced on the success terminal.
    assert "halt_reason" not in state


def test_mark_run_done_returns_none_and_mutates_in_place() -> None:
    state = _state_with_handoff()
    result = mark_run_done_direct(state)
    assert result is None


def test_mark_run_halted_clears_phase_handoff_and_sets_reason() -> None:
    state = _state_with_handoff()
    mark_run_halted(state, halt_reason="phase_handoff_halt")
    assert state["status"] == "halted"
    assert state["halt_reason"] == "phase_handoff_halt"
    assert "phase_handoff" not in state
    # No halted_at supplied → none stamped (no behavioural drift).
    assert "halted_at" not in state


def test_mark_run_halted_stamps_halted_at_when_supplied() -> None:
    state = _state_with_handoff()
    mark_run_halted(
        state, halt_reason="phase_handoff_halt", halted_at="2026-06-07T10:00:00+00:00",
    )
    assert state["halted_at"] == "2026-06-07T10:00:00+00:00"


def test_mark_run_halted_phase_handoff_shape_matches_repair() -> None:
    # The post-halt body the repair layer heals a torn halt to: status,
    # halt_reason='phase_handoff_halt', halted_at, and a cleared
    # phase_handoff. mark_run_halted must produce the identical shape.
    decided_at = "2026-06-07T10:00:00+00:00"
    state = {
        "status": "interrupted",
        "halt_reason": None,
        "phase_handoff": {"id": "h1", "phase": "validate_plan"},
        "run_id": "20260607_100000",
    }
    repaired = dict(state)
    repaired["status"] = "halted"
    repaired["halt_reason"] = "phase_handoff_halt"
    repaired["halted_at"] = decided_at
    repaired.pop("phase_handoff", None)

    mark_run_halted(state, halt_reason="phase_handoff_halt", halted_at=decided_at)
    assert state == repaired


def test_mark_run_failed_preserves_phase_handoff() -> None:
    state = _state_with_handoff()
    mark_run_failed(state, halt_reason="phase_failure:RuntimeError")
    assert state["status"] == "failed"
    assert state["halt_reason"] == "phase_failure:RuntimeError"
    # An active handoff is preserved on failure.
    assert state["phase_handoff"] == {"id": "h1", "phase": "validate_plan"}


def test_mark_run_interrupted_preserves_phase_handoff_and_stamps_time() -> None:
    state = _state_with_handoff()
    mark_run_interrupted(state, interrupted_at="2026-06-07T10:00:00")
    assert state["status"] == "interrupted"
    assert state["interrupted_at"] == "2026-06-07T10:00:00"
    assert state["halt_reason"] == "interrupted"
    # Undecided handoff must survive — repair refuses to flip it.
    assert state["phase_handoff"] == {"id": "h1", "phase": "validate_plan"}


def test_mark_run_interrupted_custom_reason() -> None:
    state = {"status": "running"}
    mark_run_interrupted(
        state, interrupted_at="2026-06-07T10:00:00", halt_reason="cancelled",
    )
    assert state["halt_reason"] == "cancelled"


def test_helpers_are_idempotent() -> None:
    # Re-applying any terminal writer yields the same shape (no toggling,
    # no re-introduction of cleared keys).
    halted = _state_with_handoff()
    mark_run_halted(halted, halt_reason="phase_handoff_halt", halted_at="t")
    once = dict(halted)
    mark_run_halted(halted, halt_reason="phase_handoff_halt", halted_at="t")
    assert halted == once

    done = _state_with_handoff()
    mark_run_done(done)
    once_done = dict(done)
    mark_run_done(done)
    assert done == once_done

    interrupted = _state_with_handoff()
    mark_run_interrupted(interrupted, interrupted_at="t")
    once_int = dict(interrupted)
    mark_run_interrupted(interrupted, interrupted_at="t")
    assert interrupted == once_int
