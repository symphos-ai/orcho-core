"""Direct unit tests for :mod:`pipeline.control.handoff_decisions`.

The engine is the load + validate + classify primitive shared by the
single-project and cross-project resume paths (ADR 0040 Phase B). The
behaviour matrix locked here:

* Valid halt / continue / retry_feedback artifacts parse to the
  matching ``HandoffDecisionResult.action`` literal.
* ``feedback`` is normalised to ``""`` when the SDK returns ``None``
  (halt / continue do not carry feedback).
* ``note`` and ``decided_at`` are passed through unchanged.
* A missing artifact for an active handoff raises ``RuntimeError`` with
  the default operator-guidance message (or the caller-supplied
  ``missing_message`` when set).
* A corrupt artifact raises ``RuntimeError`` wrapping the underlying
  ``InvalidPhaseHandoffState``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.control import (
    HandoffDecisionContext,
    HandoffDecisionResult,
    load_handoff_decision,
)


def _seed_decision_artifact(
    run_dir: Path,
    *,
    handoff_id: str,
    action: str,
    feedback: str | None = None,
    note: str | None = None,
    phase: str = "validate_plan",
    decided_at: str = "2026-05-24T12:00:00+00:00",
    overrides: dict | None = None,
) -> Path:
    """Write a decision artifact matching the SDK's persisted shape.

    Bypasses the active-handoff guard in ``phase_handoff_decide`` so the
    tests isolate the decision-read path from pause emission.
    """
    from sdk.phase_handoff import safe_handoff_id

    decisions_dir = run_dir / "phase_handoff_decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "run_id":      run_dir.name,
        "handoff_id":  handoff_id,
        "phase":       phase,
        "action":      action,
        "feedback":    feedback,
        "note":        note,
        "decided_at":  decided_at,
    }
    if overrides:
        payload.update(overrides)
    path = decisions_dir / f"{safe_handoff_id(handoff_id)}.json"
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _ctx(run_dir: Path, handoff_id: str, **kw) -> HandoffDecisionContext:
    return HandoffDecisionContext(
        run_id=run_dir.name,
        handoff_id=handoff_id,
        runs_dir=run_dir.parent,
        cwd=None,
        **kw,
    )


# ── valid actions ───────────────────────────────────────────────────────────


def test_load_halt_decision(tmp_path: Path) -> None:
    run = tmp_path / "20260524_120000"
    run.mkdir(parents=True)
    _seed_decision_artifact(
        run,
        handoff_id="validate_plan:plan_round:2",
        action="halt",
        note="release blocker confirmed",
    )

    result = load_handoff_decision(
        _ctx(run, "validate_plan:plan_round:2"),
    )

    assert isinstance(result, HandoffDecisionResult)
    assert result.action == "halt"
    assert result.feedback == ""           # None → "" normalisation
    assert result.note == "release blocker confirmed"
    assert result.decided_at == "2026-05-24T12:00:00+00:00"
    assert result.handoff_id == "validate_plan:plan_round:2"


def test_load_continue_decision(tmp_path: Path) -> None:
    run = tmp_path / "20260524_120100"
    run.mkdir(parents=True)
    _seed_decision_artifact(
        run,
        handoff_id="validate_plan:plan_round:1",
        action="continue",
        note="operator override",
    )

    result = load_handoff_decision(
        _ctx(run, "validate_plan:plan_round:1"),
    )

    assert result.action == "continue"
    assert result.feedback == ""
    assert result.note == "operator override"


def test_load_retry_feedback_decision_with_feedback(tmp_path: Path) -> None:
    run = tmp_path / "20260524_120200"
    run.mkdir(parents=True)
    _seed_decision_artifact(
        run,
        handoff_id="cross_plan:cross_plan_round:1",
        phase="cross_plan",
        action="retry_feedback",
        feedback="tighten the migration ordering",
        note=None,
    )

    result = load_handoff_decision(
        _ctx(run, "cross_plan:cross_plan_round:1"),
    )

    assert result.action == "retry_feedback"
    assert result.feedback == "tighten the migration ordering"
    assert result.note is None
    # Action literal must be one of the four runtime values — narrow check.
    assert result.action in (
        "halt", "continue", "retry_feedback", "continue_with_waiver",
    )


def test_load_continue_with_waiver_decision_with_feedback(tmp_path: Path) -> None:
    run = tmp_path / "20260524_120250"
    run.mkdir(parents=True)
    _seed_decision_artifact(
        run,
        handoff_id="review_changes:repair_round:1",
        phase="review_changes",
        action="continue_with_waiver",
        feedback="accepted risk: legacy shim stays this release",
        note="operator waiver",
    )

    result = load_handoff_decision(
        _ctx(run, "review_changes:repair_round:1"),
    )

    assert result.action == "continue_with_waiver"
    assert result.feedback == "accepted risk: legacy shim stays this release"
    assert result.note == "operator waiver"


# ── error paths ─────────────────────────────────────────────────────────────


def test_missing_decision_raises_runtime_error(tmp_path: Path) -> None:
    run = tmp_path / "20260524_120300"
    run.mkdir(parents=True)
    # No artifact seeded — strict reader returns None, engine fail-fasts.
    with pytest.raises(RuntimeError, match="no decision artifact was found"):
        load_handoff_decision(
            _ctx(run, "validate_plan:plan_round:1"),
        )


def test_missing_decision_honours_custom_message(tmp_path: Path) -> None:
    run = tmp_path / "20260524_120400"
    run.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="ORCO-XYZ: call decide first"):
        load_handoff_decision(
            _ctx(
                run, "validate_plan:plan_round:1",
                missing_message="ORCO-XYZ: call decide first.",
            ),
        )


def test_invalid_artifact_raises_runtime_error(tmp_path: Path) -> None:
    """Corrupt decision (handoff_id mismatch between path and payload)
    must surface as RuntimeError, not silent absence — silently
    downgrading would let resume trust a tampered audit record."""
    run = tmp_path / "20260524_120500"
    run.mkdir(parents=True)
    _seed_decision_artifact(
        run,
        handoff_id="validate_plan:plan_round:1",
        action="continue",
        overrides={
            # Strict reader compares persisted handoff_id with the path-
            # derived one and refuses to materialise mismatches.
            "handoff_id": "validate_plan:plan_round:9999",
        },
    )

    with pytest.raises(RuntimeError, match="failed strict validation"):
        load_handoff_decision(
            _ctx(run, "validate_plan:plan_round:1"),
        )


def test_invalid_artifact_honours_custom_prefix(tmp_path: Path) -> None:
    run = tmp_path / "20260524_120600"
    run.mkdir(parents=True)
    _seed_decision_artifact(
        run,
        handoff_id="validate_plan:plan_round:1",
        action="continue",
        overrides={"handoff_id": "validate_plan:plan_round:9999"},
    )

    with pytest.raises(RuntimeError, match="ORCO-CROSS audit drift"):
        load_handoff_decision(
            _ctx(
                run, "validate_plan:plan_round:1",
                invalid_message_prefix="ORCO-CROSS audit drift",
            ),
        )


# ── action narrowing ────────────────────────────────────────────────────────


def test_unknown_action_through_strict_reader_is_blocked() -> None:
    """The SDK strict reader rejects unknown actions before the engine
    sees them, so the narrowing branch in ``_narrow_action`` is a
    defence-in-depth check. Exercise the narrower directly to lock the
    raise behaviour for a hypothetical future-SDK leak."""
    from pipeline.control.handoff_decisions import _narrow_action

    assert _narrow_action("halt") == "halt"
    assert _narrow_action("continue") == "continue"
    assert _narrow_action("retry_feedback") == "retry_feedback"
    assert _narrow_action("continue_with_waiver") == "continue_with_waiver"

    with pytest.raises(RuntimeError, match="Unknown handoff decision action"):
        _narrow_action("future_action_we_havent_taught_about")
