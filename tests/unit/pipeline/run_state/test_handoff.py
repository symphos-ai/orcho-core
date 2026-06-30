"""Unit tests for the pure active phase-handoff transition writers (T1).

Pins the load-bearing contract for :mod:`pipeline.run_state.handoff`:

* request marks ``awaiting_phase_handoff`` + stamps the active payload;
  continue / continue_with_waiver / retry_feedback flip back to ``running``
  and clear the payload (idempotent when none is present);
* the derived ``phase_handoff_override`` / ``phase_handoff_waiver`` /
  ``human_feedback`` dicts are byte-identical (shape **and** key order) to
  the historical inline construction in ``pipeline/project/handoff.py``, and
  carry no extra keys;
* a ``retry_feedback`` transition carries a typed plan/repair mode
  distinguishable without parsing the paused phase string;
* halt is **not** implemented here (it stays terminal); and
* the module is pure — no IO / subprocess / provider / runtime imports.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import pipeline.run_state.handoff as handoff_mod
from pipeline.run_state import (
    HandoffAction,
    HandoffRetryMode,
    HandoffTransition,
    build_handoff_payload,
    build_human_feedback,
    build_phase_handoff_override,
    build_phase_handoff_waiver,
    clear_active_handoff,
    continue_handoff,
    continue_with_waiver_handoff,
    request_active_handoff,
    retry_feedback_handoff,
)


def _state_with_handoff() -> dict:
    return {
        "status": "awaiting_phase_handoff",
        "phase_handoff": {"id": "h1", "phase": "validate_plan"},
    }


# ── request / clear (in-place mapping mutation) ────────────────────────


def test_request_active_handoff_sets_status_and_payload() -> None:
    state: dict = {"status": "running"}
    payload = {"id": "h9", "phase": "plan"}
    result = request_active_handoff(state, payload=payload)
    assert result is None
    assert state["status"] == "awaiting_phase_handoff"
    assert state["phase_handoff"] is payload


def test_clear_active_handoff_pops_payload_and_runs() -> None:
    state = _state_with_handoff()
    clear_active_handoff(state)
    assert state["status"] == "running"
    assert "phase_handoff" not in state


def test_clear_active_handoff_idempotent_without_payload() -> None:
    state: dict = {"status": "awaiting_phase_handoff"}
    clear_active_handoff(state)
    assert state["status"] == "running"
    assert "phase_handoff" not in state


# ── build_handoff_payload byte-equivalence + key order ─────────────────


def test_build_handoff_payload_matches_legacy_shape() -> None:
    avail = ["continue", "halt"]
    arts = {"findings": ["x"]}
    payload = build_handoff_payload(
        handoff_id="h1",
        phase="validate_plan",
        handoff_type="on_reject",
        trigger="reject",
        verdict="REJECTED",
        approved=False,
        round_extras_key="plan_round",
        round_n=2,
        loop_max_rounds=2,
        available_actions=avail,
        artifacts=arts,
        last_output="prior",
    )
    # Legacy inline shape from apply_phase_handoff_pause (key order intact).
    assert list(payload.items()) == [
        ("id", "h1"),
        ("phase", "validate_plan"),
        ("type", "on_reject"),
        ("trigger", "reject"),
        ("verdict", "REJECTED"),
        ("approved", False),
        ("round_extras_key", "plan_round"),
        ("round", 2),
        ("loop_max_rounds", 2),
        ("available_actions", ["continue", "halt"]),
        ("artifacts", {"findings": ["x"]}),
        ("last_output", "prior"),
    ]
    # available_actions / artifacts are defensively copied, not aliased.
    assert payload["available_actions"] is not avail
    assert payload["artifacts"] is not arts


# ── continue ───────────────────────────────────────────────────────────


def test_continue_handoff_clears_and_builds_override() -> None:
    state = _state_with_handoff()
    tr = continue_handoff(
        state, handoff_id="h1", note="n", decided_at="2026-01-01T00:00:00Z",
    )
    assert state["status"] == "running"
    assert "phase_handoff" not in state
    assert isinstance(tr, HandoffTransition)
    assert list(tr.override.items()) == [
        ("handoff_id", "h1"),
        ("action", "continue"),
        ("feedback", None),
        ("note", "n"),
        ("decided_at", "2026-01-01T00:00:00Z"),
    ]
    # A bare continue carries no waiver / human_feedback / retry mode.
    assert tr.waiver is None
    assert tr.human_feedback is None
    assert tr.retry_mode is None


# ── continue_with_waiver ────────────────────────────────────────────────


def test_continue_with_waiver_builds_override_and_waiver() -> None:
    state = _state_with_handoff()
    tr = continue_with_waiver_handoff(
        state,
        handoff_id="h1",
        phase="review_changes",
        feedback="accepted: known flaky test",
        note="ticket-123",
        decided_at="2026-02-02T00:00:00Z",
        findings=["f1", "f2"],
        critique="reviewer said no",
    )
    assert state["status"] == "running"
    assert "phase_handoff" not in state
    assert list(tr.override.items()) == [
        ("handoff_id", "h1"),
        ("action", "continue_with_waiver"),
        ("feedback", "accepted: known flaky test"),
        ("note", "ticket-123"),
        ("decided_at", "2026-02-02T00:00:00Z"),
    ]
    assert tr.waiver is not None
    assert list(tr.waiver.items()) == [
        ("handoff_id", "h1"),
        ("phase", "review_changes"),
        ("waiver_text", "accepted: known flaky test"),
        ("note", "ticket-123"),
        ("decided_at", "2026-02-02T00:00:00Z"),
        ("findings", ["f1", "f2"]),
        ("critique", "reviewer said no"),
    ]
    # The reviewer verdict (feedback) is preserved as the waiver reason.
    assert tr.waiver["waiver_text"] == tr.override["feedback"]
    assert tr.human_feedback is None
    assert tr.retry_mode is None


# ── retry_feedback (plan vs repair) ─────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "expected_mode"),
    [
        (HandoffRetryMode.PLAN, "plan"),
        (HandoffRetryMode.REPAIR, "repair"),
    ],
)
def test_retry_feedback_carries_typed_mode(
    mode: HandoffRetryMode, expected_mode: str,
) -> None:
    state = _state_with_handoff()
    tr = retry_feedback_handoff(
        state,
        handoff_id="h1",
        mode=mode,
        feedback="please redo subtask 3",
        note=None,
        decided_at="2026-03-03T00:00:00Z",
    )
    assert state["status"] == "running"
    assert "phase_handoff" not in state
    # The plan/repair distinction is a typed enum, NOT a phase string.
    assert tr.retry_mode is mode
    assert str(tr.retry_mode) == expected_mode
    assert list(tr.override.items()) == [
        ("handoff_id", "h1"),
        ("action", "retry_feedback"),
        ("feedback", "please redo subtask 3"),
        ("note", None),
        ("decided_at", "2026-03-03T00:00:00Z"),
    ]
    assert tr.human_feedback is not None
    assert list(tr.human_feedback.items()) == [
        ("handoff_id", "h1"),
        ("feedback", "please redo subtask 3"),
        ("decided_at", "2026-03-03T00:00:00Z"),
    ]
    assert tr.waiver is None


def test_retry_mode_distinguishes_without_phase_parsing() -> None:
    plan = retry_feedback_handoff(
        dict(_state_with_handoff()),
        handoff_id="h1", mode=HandoffRetryMode.PLAN,
        feedback="f", note=None, decided_at=None,
    )
    repair = retry_feedback_handoff(
        dict(_state_with_handoff()),
        handoff_id="h1", mode=HandoffRetryMode.REPAIR,
        feedback="f", note=None, decided_at=None,
    )
    # Two retries with identical override/feedback are still distinguishable
    # purely by the typed mode — no phase string lives in the override.
    assert plan.override == repair.override
    assert plan.retry_mode is not repair.retry_mode
    assert "phase" not in plan.override


# ── builders: exact key sets (nothing extra) ────────────────────────────


def test_override_has_exactly_five_keys() -> None:
    ov = build_phase_handoff_override(
        handoff_id="h1", action=HandoffAction.CONTINUE,
        feedback=None, note=None, decided_at=None,
    )
    assert set(ov) == {"handoff_id", "action", "feedback", "note", "decided_at"}


def test_waiver_has_exactly_seven_keys() -> None:
    w = build_phase_handoff_waiver(
        handoff_id="h1", phase="review_changes", waiver_text="t",
        note=None, decided_at=None, findings=None, critique="",
    )
    assert set(w) == {
        "handoff_id", "phase", "waiver_text", "note",
        "decided_at", "findings", "critique",
    }


def test_human_feedback_has_exactly_three_keys() -> None:
    hf = build_human_feedback(handoff_id="h1", feedback="f", decided_at=None)
    assert set(hf) == {"handoff_id", "feedback", "decided_at"}


def test_override_action_stored_as_plain_string() -> None:
    ov = build_phase_handoff_override(
        handoff_id="h1", action=HandoffAction.CONTINUE_WITH_WAIVER,
        feedback="f", note=None, decided_at=None,
    )
    # Stored as the bare value string, byte-identical to the legacy literal.
    assert ov["action"] == "continue_with_waiver"
    assert type(ov["action"]) is str


# ── purity: halt absent, no forbidden imports ───────────────────────────


def test_halt_not_implemented_here() -> None:
    # Halt is terminal; this module must expose no halt transition.
    assert not hasattr(handoff_mod, "halt_handoff")
    assert not hasattr(handoff_mod, "mark_run_halted")
    assert "halt" not in {*handoff_mod.__all__}


def test_module_imports_only_run_state_types_and_typing() -> None:
    src = Path(handoff_mod.__file__).read_text()
    tree = ast.parse(src)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
    # Only typing + the sibling pure types module are permitted.
    assert imported_modules <= {
        "__future__", "typing", "pipeline.run_state.types",
    }
    forbidden = ("runtime", "resume", "finaliz", "provider", "subprocess", "os")
    for mod in imported_modules:
        assert not any(bad in mod for bad in forbidden), mod
