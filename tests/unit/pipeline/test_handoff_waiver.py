"""Unit tests for ``pipeline.project.handoff_waiver`` (ADR 0073).

Covers the two waiver-state operations the implement auto-waiver path relies
on: ``apply_waiver_to_state`` (requires non-empty rationale, applier-set
``decided_by``) and ``sync_waiver_to_session`` (conflict-aware durable mirror).
"""
from __future__ import annotations

import pytest

from pipeline.project.handoff_waiver import (
    WAIVER_KEY,
    apply_waiver_to_state,
    sync_waiver_to_session,
)


class _FakeState:
    def __init__(self) -> None:
        self.extras: dict = {}


class _FakeRun:
    def __init__(self, state: _FakeState) -> None:
        self.state = state
        self.session: dict = {}


# ── apply_waiver_to_state ──────────────────────────────────────────────────

def test_apply_waiver_writes_payload_with_provenance() -> None:
    state = _FakeState()
    waiver = apply_waiver_to_state(
        state,
        handoff_id="implement:implement_handoff:1",
        phase="implement",
        waiver_text="auto-waived: criteria not closed after repair",
        decided_by="auto:on_exhausted",
    )
    assert state.extras[WAIVER_KEY] is waiver
    assert waiver["handoff_id"] == "implement:implement_handoff:1"
    assert waiver["phase"] == "implement"
    assert waiver["decided_by"] == "auto:on_exhausted"
    assert waiver["decided_at"]  # defaulted to now
    assert waiver["waiver_text"].startswith("auto-waived")


def test_apply_waiver_preserves_caller_decided_at_and_fields() -> None:
    state = _FakeState()
    waiver = apply_waiver_to_state(
        state,
        handoff_id="h1",
        phase="implement",
        waiver_text="operator accepts",
        decided_by="operator",
        note="see ticket",
        decided_at="2026-06-04T00:00:00+00:00",
        findings=["f1"],
        critique="critique text",
    )
    assert waiver["decided_at"] == "2026-06-04T00:00:00+00:00"
    assert waiver["note"] == "see ticket"
    assert waiver["findings"] == ["f1"]
    assert waiver["critique"] == "critique text"


def test_apply_waiver_empty_text_raises() -> None:
    state = _FakeState()
    with pytest.raises(ValueError, match="non-empty string"):
        apply_waiver_to_state(
            state,
            handoff_id="h1",
            phase="implement",
            waiver_text="",
            decided_by="operator",
        )


def test_apply_waiver_blank_text_raises() -> None:
    state = _FakeState()
    with pytest.raises(ValueError, match="non-empty string"):
        apply_waiver_to_state(
            state,
            handoff_id="h1",
            phase="implement",
            waiver_text="   ",
            decided_by="operator",
        )


# ── sync_waiver_to_session ─────────────────────────────────────────────────

def test_sync_no_waiver_is_noop() -> None:
    run = _FakeRun(_FakeState())
    sync_waiver_to_session(run)
    assert WAIVER_KEY not in run.session


def test_sync_mirrors_state_waiver_onto_session() -> None:
    state = _FakeState()
    run = _FakeRun(state)
    apply_waiver_to_state(
        state,
        handoff_id="h1",
        phase="implement",
        waiver_text="auto-waived",
        decided_by="auto:on_exhausted",
    )
    sync_waiver_to_session(run)
    assert run.session[WAIVER_KEY] == state.extras[WAIVER_KEY]


def test_sync_same_payload_is_noop() -> None:
    state = _FakeState()
    run = _FakeRun(state)
    apply_waiver_to_state(
        state,
        handoff_id="h1",
        phase="implement",
        waiver_text="auto-waived",
        decided_by="auto:on_exhausted",
    )
    sync_waiver_to_session(run)
    # Re-sync with the identical payload must not raise.
    sync_waiver_to_session(run)
    assert run.session[WAIVER_KEY] == state.extras[WAIVER_KEY]


def test_sync_conflicting_payload_raises() -> None:
    state = _FakeState()
    run = _FakeRun(state)
    apply_waiver_to_state(
        state,
        handoff_id="h2",
        phase="implement",
        waiver_text="new waiver",
        decided_by="auto:on_exhausted",
    )
    # A different waiver already persisted on the session.
    run.session[WAIVER_KEY] = {"handoff_id": "h1", "waiver_text": "old"}
    with pytest.raises(RuntimeError, match="different"):
        sync_waiver_to_session(run)
