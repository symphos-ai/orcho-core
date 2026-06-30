"""
log_phase() ↔ event-store wiring.

Verifies the bridge installed in core.observability.logging.log_phase:
on START it tags the phase + emits phase.start; on END/DONE it emits
phase.end (carrying the still-active phase) and clears the tag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.observability import events as evstore
from core.observability.logging import log_phase, set_progress_log


@pytest.fixture(autouse=True)
def _reset(tmp_path: Path):
    evstore.init_event_store(tmp_path)
    set_progress_log(None)  # don't clutter the test workspace with progress.log
    yield
    evstore.init_event_store(None)


def test_phase_start_tags_and_emits(tmp_path: Path):
    log_phase("PLAN", "PLAN")
    [e] = evstore.read_all(tmp_path)
    assert e.kind == "phase.start"
    assert e.phase == "PLAN"
    # attempt defaults to 1 and is always emitted; phase_kind is None
    # for milestone-style callers and gets dropped by _clean_payload.
    # Phase 7.10-followup: ``round`` is auto-injected from the
    # phase-context (= attempt fallback when caller doesn't pass it
    # explicitly).
    assert e.payload == {"title": "PLAN", "attempt": 1, "round": 1}


def test_phase_end_carries_phase_tag(tmp_path: Path):
    log_phase("PLAN", "PLAN")
    log_phase("PLAN", "PLAN", "END", "approved")
    events = evstore.read_all(tmp_path)
    assert [e.kind for e in events] == ["phase.start", "phase.end"]
    end = events[1]
    # The phase tag must still be set when phase.end is emitted (the bridge
    # clears it AFTER emit, not before — otherwise the chip-log shows
    # "None done").
    assert end.phase == "PLAN"
    assert end.payload == {
        "title": "PLAN", "outcome": "approved", "attempt": 1, "round": 1,
    }


def test_phase_kind_and_attempt_propagate(tmp_path: Path):
    """phase_kind/attempt must flow into the event payload so the dashboard
 reducer can group attempts by canonical kind."""
    log_phase("REVIEW_CHANGES", "REVIEW round 2", phase_kind="REVIEW", attempt=2)
    log_phase("REVIEW_CHANGES", "REVIEW round 2", "END", "lgtm",
              phase_kind="REVIEW", attempt=2)
    events = evstore.read_all(tmp_path)
    assert events[0].payload == {
        "title": "REVIEW round 2", "phase_kind": "REVIEW", "attempt": 2,
        "round": 2,
    }
    assert events[1].payload == {
        "title": "REVIEW round 2", "outcome": "lgtm",
        "phase_kind": "REVIEW", "attempt": 2, "round": 2,
    }


def test_phase_key_and_round_in_payload(tmp_path: Path):
    """Phase 7.10-followup: ``phase_key`` (lowercase handler key) and
 ``round`` (loop counter) flow into every event payload via the
 phase-context so machine consumers don't need to derive them from
 the display string."""
    log_phase(
        "VALIDATE_PLAN", "round 2", phase_kind="VALIDATE_PLAN",
        attempt=2, phase_key="validate_plan", round=2,
    )
    [e] = evstore.read_all(tmp_path)
    assert e.payload["phase_key"] == "validate_plan"
    assert e.payload["round"] == 2


def test_done_status_treated_as_end(tmp_path: Path):
    """The orchestrator emits log_phase("DONE",..., "DONE") at the very
 end. That's a non-START status and must be mapped to phase.end."""
    log_phase("DONE", "Pipeline complete", "DONE")
    [e] = evstore.read_all(tmp_path)
    assert e.kind == "phase.end"
    assert e.payload["outcome"] == "DONE"


def test_phase_tag_resets_between_phases(tmp_path: Path):
    log_phase("PLAN",          "PLAN")
    log_phase("PLAN",          "PLAN", "END", "approved")
    log_phase("VALIDATE_PLAN", "validate_plan")
    log_phase("VALIDATE_PLAN", "validate_plan", "END", "approved")
    events = evstore.read_all(tmp_path)
    phases = [e.phase for e in events]
    assert phases == ["PLAN", "PLAN", "VALIDATE_PLAN", "VALIDATE_PLAN"]


def test_no_op_when_store_disabled(tmp_path: Path):
    evstore.init_event_store(None)
    # Should not raise even though store is disabled.
    log_phase("PLAN", "PLAN")
    # And the file is gone, so read_all returns [].
    assert evstore.read_all(tmp_path) == []
