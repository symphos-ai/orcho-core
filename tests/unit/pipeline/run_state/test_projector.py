"""Unit tests for the run-state projector."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.run_state import (
    RunStateSnapshot,
    RunStatus,
    project_events,
    project_run_dir,
)


def _line(seq: int, kind: str, *, phase: str | None = None, **payload: object) -> str:
    return json.dumps(
        {"seq": seq, "ts": "t", "kind": kind, "phase": phase, "payload": payload}
    )


def test_project_events_folds_stream() -> None:
    snap = project_events(
        [
            {"seq": 1, "kind": "run.start", "payload": {"task": "t", "run_kind": "single_project"}},
            {"seq": 2, "kind": "phase.start", "payload": {"title": "PLAN"}},
            {"seq": 3, "kind": "phase.end", "payload": {"title": "PLAN", "outcome": "ok"}},
            {"seq": 4, "kind": "phase.handoff_requested", "payload": {"handoff_id": "h1", "phase": "validate_plan"}},
        ]
    )
    assert snap.status is RunStatus.AWAITING_PHASE_HANDOFF
    assert snap.completed_phases == ("PLAN",)
    assert snap.seen_handoff_ids == ("h1",)
    assert snap.seq == 4


def test_project_run_dir_missing_events_returns_initial(tmp_path: Path) -> None:
    snap = project_run_dir(tmp_path)
    assert snap == RunStateSnapshot.initial()


def test_project_run_dir_tolerates_partial_last_line(tmp_path: Path) -> None:
    (tmp_path / "events.jsonl").write_text(
        _line(1, "run.start", task="t", run_kind="single_project") + "\n"
        + _line(2, "phase.start", title="PLAN") + "\n"
        + '{"seq": 3, "ts": "t", "kind": "phase.end", "payl',  # partial last line
        encoding="utf-8",
    )
    snap = project_run_dir(tmp_path)
    # The partial last line is skipped by read_all; the first two apply.
    assert snap.status is RunStatus.RUNNING
    assert snap.active_phase == "PLAN"
    assert snap.seq == 2


def test_project_run_dir_ignores_meta_json(tmp_path: Path) -> None:
    # meta.json claims a status that contradicts the event stream.
    (tmp_path / "meta.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(
        _line(1, "run.start", task="t", run_kind="single_project") + "\n"
        + _line(2, "phase.handoff_requested",
                handoff_id="hX", phase="validate_plan") + "\n",
        encoding="utf-8",
    )
    snap = project_run_dir(tmp_path)
    # Projection follows events (awaiting handoff), NOT meta.json (done).
    assert snap.status is RunStatus.AWAITING_PHASE_HANDOFF
    assert snap.active_handoff_id == "hX"
    assert snap.seen_handoff_ids == ("hX",)
