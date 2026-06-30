"""Unit tests for the read-only run-state consistency checker."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.run_state import validate_run_state


def _write_events(run_dir: Path, lines: list[dict]) -> None:
    run_dir.joinpath("events.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def _write_meta(run_dir: Path, meta: dict) -> None:
    run_dir.joinpath("meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _write_decision(run_dir: Path, name: str, decision: dict) -> None:
    dd = run_dir / "phase_handoff_decisions"
    dd.mkdir(exist_ok=True)
    dd.joinpath(f"{name}.json").write_text(json.dumps(decision), encoding="utf-8")


def _handoff_event(handoff_id: str, phase: str = "validate_plan", seq: int = 1) -> dict:
    return {
        "seq": seq,
        "ts": "t",
        "kind": "phase.handoff_requested",
        "phase": phase,
        "payload": {"handoff_id": handoff_id, "phase": phase},
    }


def _codes(report) -> set[str]:
    return {issue.code for issue in report.issues}


def _severity(report, code: str) -> str:
    return next(issue.severity for issue in report.issues if issue.code == code)


def test_missing_everything_is_ok(tmp_path: Path) -> None:
    report = validate_run_state(tmp_path)
    assert report.ok
    assert report.issues == ()
    assert report.meta_status is None


def test_happy_path_consistent_state_is_ok(tmp_path: Path) -> None:
    _write_events(tmp_path, [
        {"seq": 1, "ts": "t", "kind": "run.start", "phase": None,
         "payload": {"task": "t", "run_kind": "single_project"}},
        {"seq": 2, "ts": "t", "kind": "phase.start", "phase": "PLAN",
         "payload": {"title": "PLAN"}},
        {"seq": 3, "ts": "t", "kind": "phase.end", "phase": "PLAN",
         "payload": {"title": "PLAN", "outcome": "ok"}},
    ])
    _write_meta(tmp_path, {"status": "running"})
    report = validate_run_state(tmp_path)
    assert report.ok
    assert report.issues == ()
    assert report.meta_status == "running"


def test_interrupted_with_active_handoff(tmp_path: Path) -> None:
    _write_events(tmp_path, [_handoff_event("h1")])
    _write_meta(tmp_path, {"status": "interrupted", "phase_handoff": {"id": "h1"}})
    report = validate_run_state(tmp_path)
    assert "interrupted_with_active_handoff" in _codes(report)
    assert _severity(report, "interrupted_with_active_handoff") == "warning"
    assert report.ok  # warning does not flip ok


def test_terminal_with_stale_handoff(tmp_path: Path) -> None:
    _write_events(tmp_path, [_handoff_event("h1")])
    _write_meta(tmp_path, {"status": "done", "phase_handoff": {"id": "h1"}})
    report = validate_run_state(tmp_path)
    assert "terminal_with_stale_handoff" in _codes(report)
    assert _severity(report, "terminal_with_stale_handoff") == "warning"


def test_active_handoff_without_decision(tmp_path: Path) -> None:
    _write_events(tmp_path, [_handoff_event("h1")])
    _write_meta(
        tmp_path,
        {"status": "awaiting_phase_handoff", "phase_handoff": {"id": "h1"}},
    )
    report = validate_run_state(tmp_path)
    assert "active_handoff_without_decision" in _codes(report)
    assert _severity(report, "active_handoff_without_decision") == "info"
    assert report.ok


def test_halt_decision_without_halted_meta(tmp_path: Path) -> None:
    _write_events(tmp_path, [_handoff_event("h1")])
    _write_meta(tmp_path, {"status": "running"})
    _write_decision(tmp_path, "h1", {"action": "halt", "handoff_id": "h1"})
    report = validate_run_state(tmp_path)
    assert "halt_decision_without_halted_meta" in _codes(report)
    assert _severity(report, "halt_decision_without_halted_meta") == "error"
    assert not report.ok


def test_meta_handoff_without_event(tmp_path: Path) -> None:
    # Event stream has h1, but meta points at an id that never appeared.
    _write_events(tmp_path, [_handoff_event("h1")])
    _write_meta(
        tmp_path,
        {"status": "awaiting_phase_handoff", "phase_handoff": {"id": "ghost"}},
    )
    report = validate_run_state(tmp_path)
    assert "meta_handoff_without_event" in _codes(report)
    assert _severity(report, "meta_handoff_without_event") == "error"
    assert not report.ok


def test_f2_halt_cleared_active_but_event_existed_no_false_positive(
    tmp_path: Path,
) -> None:
    """F2: a handoff event was emitted (id in seen_handoff_ids) and the run
    later halted (projection clears active_handoff_id). meta.json still
    carries a stale meta.phase_handoff with the SAME id and status=halted —
    so the trigger condition for meta_handoff_without_event (meta.phase_handoff
    present) IS satisfied. The check must NOT fire, because the id is in the
    event history. terminal_with_stale_handoff is expected instead.
    """
    _write_events(tmp_path, [
        _handoff_event("H", seq=1),
        {"seq": 2, "ts": "t", "kind": "phase_handoff.decided", "phase": None,
         "payload": {"action": "halt"}},
    ])
    _write_meta(tmp_path, {"status": "halted", "phase_handoff": {"id": "H"}})
    _write_decision(tmp_path, "H", {"action": "halt", "handoff_id": "H"})

    report = validate_run_state(tmp_path)
    # Projection cleared the active pointer but kept the history.
    assert report.projected.active_handoff_id is None
    assert "H" in report.projected.seen_handoff_ids
    # No false positive.
    assert "meta_handoff_without_event" not in _codes(report)
    # The genuine stale-pointer issue is still reported.
    assert "terminal_with_stale_handoff" in _codes(report)


def test_f2_contrast_unknown_id_does_fire(tmp_path: Path) -> None:
    """Contrast to the F2 case: same shape but meta points at an id absent
    from the event history → meta_handoff_without_event MUST fire.
    """
    _write_events(tmp_path, [
        _handoff_event("H", seq=1),
        {"seq": 2, "ts": "t", "kind": "phase_handoff.decided", "phase": None,
         "payload": {"action": "halt"}},
    ])
    _write_meta(tmp_path, {"status": "halted", "phase_handoff": {"id": "OTHER"}})
    report = validate_run_state(tmp_path)
    assert "meta_handoff_without_event" in _codes(report)
    assert not report.ok
