"""Unit tests for the cross-run read-only classifier + invariants (Stage 7).

Covers :func:`pipeline.run_state.cross.classify_cross_run_state` and
:func:`pipeline.run_state.cross.validate_cross_run_state` — one case per
invariant plus tolerance cases — and the cross-safe terminal writer
:func:`pipeline.run_state.terminal.settle_cross_terminal`, including the
Stage 3b regression guard that the single-project writers still PRESERVE an
active handoff.

All artifacts are built on disk in a tmp dir; no real pipeline is run.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.run_state import (
    classify_cross_run_state,
    mark_run_failed,
    mark_run_interrupted,
    settle_cross_terminal,
    validate_cross_run_state,
)


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _meta(run_dir: Path, payload: dict) -> None:
    _write(run_dir / "meta.json", payload)


def _ckpt(run_dir: Path, payload: dict) -> None:
    _write(run_dir / "cross_checkpoint.json", payload)


def _codes(issues) -> set[str]:
    return {i.code for i in issues}


# ── classify_cross_run_state ───────────────────────────────────────────


def test_classify_folds_meta_and_checkpoint(tmp_path: Path) -> None:
    _meta(
        tmp_path,
        {"status": "awaiting_phase_handoff", "phase_handoff": {"id": "cfa:1"}},
    )
    _ckpt(
        tmp_path,
        {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "cfa",
            "phase_handoff_id": "cfa:1",
            "cfa_paused_state": {"verdict": "REJECTED"},
            "sub_status": {"web": "done"},
        },
    )
    snap = classify_cross_run_state(tmp_path)
    assert snap.meta_status == "awaiting_phase_handoff"
    assert snap.active_handoff == {"id": "cfa:1"}
    assert snap.active_handoff_id == "cfa:1"
    assert snap.checkpoint_pending is True
    assert snap.checkpoint_kind == "cfa"
    assert snap.cfa_paused_state == {"verdict": "REJECTED"}
    assert snap.sub_status == {"web": "done"}


def test_classify_reads_child_statuses(tmp_path: Path) -> None:
    _meta(tmp_path, {"status": "done"})
    _ckpt(tmp_path, {"sub_status": {"web": "done", "api": "failed"}})
    (tmp_path / "web").mkdir()
    _meta(tmp_path / "web", {"status": "done"})
    # api child dir has no meta.json → omitted from child_statuses.
    snap = classify_cross_run_state(tmp_path)
    assert snap.child_statuses == {"web": "done"}


def test_classify_reads_decisions(tmp_path: Path) -> None:
    _meta(tmp_path, {"status": "done"})
    decisions = tmp_path / "phase_handoff_decisions"
    decisions.mkdir()
    _write(decisions / "001.json", {"action": "halt", "handoff_id": "cfa:1"})
    _write(
        decisions / "000.json",
        {"action": "continue", "handoff_id": "cross_plan:1"},
    )
    snap = classify_cross_run_state(tmp_path)
    # Sorted by filename → 000 then 001.
    assert snap.decisions == (
        {"action": "continue", "handoff_id": "cross_plan:1"},
        {"action": "halt", "handoff_id": "cfa:1"},
    )


# ── tolerance ──────────────────────────────────────────────────────────


def test_no_decisions_dir_yields_empty_tuple(tmp_path: Path) -> None:
    _meta(tmp_path, {"status": "done"})
    assert classify_cross_run_state(tmp_path).decisions == ()


def test_corrupt_decision_file_is_skipped(tmp_path: Path) -> None:
    _meta(tmp_path, {"status": "done"})
    decisions = tmp_path / "phase_handoff_decisions"
    decisions.mkdir()
    (decisions / "bad.json").write_text("{not json", encoding="utf-8")
    _write(decisions / "ok.json", {"action": "halt", "handoff_id": "cfa:1"})
    snap = classify_cross_run_state(tmp_path)
    # The single bad artifact is skipped; the good one survives.
    assert snap.decisions == (
        {"action": "halt", "handoff_id": "cfa:1"},
    )


def test_no_checkpoint_file_degrades_to_empty(tmp_path: Path) -> None:
    _meta(tmp_path, {"status": "done"})
    snap = classify_cross_run_state(tmp_path)
    assert snap.checkpoint_pending is False
    assert snap.checkpoint_kind is None
    assert snap.sub_status == {}
    assert validate_cross_run_state(tmp_path) == ()


def test_corrupt_json_degrades_to_empty(tmp_path: Path) -> None:
    (tmp_path / "meta.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "cross_checkpoint.json").write_text("}}", encoding="utf-8")
    snap = classify_cross_run_state(tmp_path)
    assert snap.meta_status is None
    assert snap.active_handoff is None
    assert validate_cross_run_state(tmp_path) == ()


# ── invariant: happy path (no issues) ──────────────────────────────────


def test_clean_cross_pending_has_no_issues(tmp_path: Path) -> None:
    _meta(
        tmp_path,
        {
            "status": "awaiting_phase_handoff",
            "phase_handoff": {"id": "cross_plan:1"},
        },
    )
    _ckpt(
        tmp_path,
        {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "plan",
            "phase_handoff_id": "cross_plan:1",
        },
    )
    assert validate_cross_run_state(tmp_path) == ()


def test_clean_terminal_has_no_issues(tmp_path: Path) -> None:
    _meta(tmp_path, {"status": "done"})
    _ckpt(tmp_path, {"sub_status": {"web": "done"}})
    assert validate_cross_run_state(tmp_path) == ()


# ── invariant 1: terminal with stale handoff (all in-scope terminals) ──


def test_terminal_with_stale_handoff_done(tmp_path: Path) -> None:
    _meta(tmp_path, {"status": "done", "phase_handoff": {"id": "cfa:1"}})
    issues = validate_cross_run_state(tmp_path)
    assert "cross_terminal_with_stale_handoff" in _codes(issues)
    issue = next(
        i for i in issues if i.code == "cross_terminal_with_stale_handoff"
    )
    assert issue.severity == "warning"


def test_terminal_with_stale_handoff_failed(tmp_path: Path) -> None:
    # The load-bearing case: a FAILED cross terminal must still flag a stale
    # handoff (single-project would preserve it; cross does not).
    _meta(tmp_path, {"status": "failed", "phase_handoff": {"id": "cfa:1"}})
    assert "cross_terminal_with_stale_handoff" in _codes(
        validate_cross_run_state(tmp_path)
    )


def test_terminal_with_stale_handoff_halted_and_cancelled(
    tmp_path: Path,
) -> None:
    for status in ("halted", "cancelled"):
        run_dir = tmp_path / status
        run_dir.mkdir()
        _meta(run_dir, {"status": status, "phase_handoff": {"id": "cfa:1"}})
        assert "cross_terminal_with_stale_handoff" in _codes(
            validate_cross_run_state(run_dir)
        )


# ── invariant 2: checkpoint pending without active payload ──────────────


def test_pending_without_active_handoff_is_error(tmp_path: Path) -> None:
    _meta(tmp_path, {"status": "running"})
    _ckpt(
        tmp_path,
        {"phase_handoff_pending": True, "phase_handoff_kind": "plan"},
    )
    issues = validate_cross_run_state(tmp_path)
    issue = next(
        i
        for i in issues
        if i.code == "checkpoint_pending_without_active_handoff"
    )
    assert issue.severity == "error"


# ── invariant 3: active payload without pending marker ──────────────────


def test_active_handoff_without_pending_marker(tmp_path: Path) -> None:
    _meta(
        tmp_path,
        {
            "status": "awaiting_phase_handoff",
            "phase_handoff": {"id": "cross_plan:1"},
        },
    )
    _ckpt(tmp_path, {"phase_handoff_pending": False})
    issues = validate_cross_run_state(tmp_path)
    issue = next(
        i
        for i in issues
        if i.code == "active_handoff_without_checkpoint_pending"
    )
    assert issue.severity == "warning"


# ── invariant 4: kind/id mismatch ──────────────────────────────────────


def test_kind_id_mismatch(tmp_path: Path) -> None:
    _meta(
        tmp_path,
        {"status": "awaiting_phase_handoff", "phase_handoff": {"id": "cfa:1"}},
    )
    _ckpt(
        tmp_path,
        {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "plan",  # expects cross_plan: prefix
            "phase_handoff_id": "cfa:1",
        },
    )
    assert "checkpoint_kind_id_mismatch" in _codes(
        validate_cross_run_state(tmp_path)
    )


# ── invariant 5: incomplete project marker ─────────────────────────────


def test_project_marker_incomplete_missing_alias(tmp_path: Path) -> None:
    _meta(
        tmp_path,
        {
            "status": "awaiting_phase_handoff",
            "phase_handoff": {"id": "project:1"},
        },
    )
    _ckpt(
        tmp_path,
        {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "project",
            "phase_handoff_id": "project:1",
            "phase_handoff_child_id": "20260608_x",
            "sub_status": {"web": "running"},
            "phase_handoff_project_alias": "api",  # not in sub_status
        },
    )
    assert "project_handoff_marker_incomplete" in _codes(
        validate_cross_run_state(tmp_path)
    )


def test_project_marker_complete_is_clean(tmp_path: Path) -> None:
    _meta(
        tmp_path,
        {
            "status": "awaiting_phase_handoff",
            "phase_handoff": {"id": "project:1"},
        },
    )
    _ckpt(
        tmp_path,
        {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "project",
            "phase_handoff_id": "project:1",
            "phase_handoff_child_id": "20260608_x",
            "phase_handoff_project_alias": "web",
            "sub_status": {"web": "awaiting_phase_handoff"},
        },
    )
    assert "project_handoff_marker_incomplete" not in _codes(
        validate_cross_run_state(tmp_path)
    )


# ── invariant 6: cfa pending without paused state ──────────────────────


def test_cfa_pending_without_paused_state(tmp_path: Path) -> None:
    _meta(
        tmp_path,
        {"status": "awaiting_phase_handoff", "phase_handoff": {"id": "cfa:1"}},
    )
    _ckpt(
        tmp_path,
        {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "cfa",
            "phase_handoff_id": "cfa:1",
            # cfa_paused_state intentionally absent
        },
    )
    assert "cfa_pending_without_paused_state" in _codes(
        validate_cross_run_state(tmp_path)
    )


# ── invariant 7: pending gate and pending handoff both active ──────────


def test_pending_gate_and_handoff_active(tmp_path: Path) -> None:
    _meta(
        tmp_path,
        {
            "status": "awaiting_phase_handoff",
            "phase_handoff": {"id": "cross_plan:1"},
        },
    )
    _ckpt(
        tmp_path,
        {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "plan",
            "phase_handoff_id": "cross_plan:1",
            "pending_gate": {"gate": "manual_confirm"},
        },
    )
    assert "pending_gate_and_handoff_active" in _codes(
        validate_cross_run_state(tmp_path)
    )


# ── settle_cross_terminal: clears handoff for all in-scope terminals ────


def test_settle_cross_terminal_clears_handoff_all_terminals() -> None:
    for status in ("done", "failed", "halted", "cancelled"):
        state = {
            "status": "awaiting_phase_handoff",
            "phase_handoff": {"id": "cfa:1", "phase": "cross_final_acceptance"},
        }
        settle_cross_terminal(state, status=status, halt_reason="cross_x")
        assert state["status"] == status
        assert "phase_handoff" not in state


def test_settle_cross_terminal_done_carries_no_halt_reason() -> None:
    state = {"phase_handoff": {"id": "cfa:1"}}
    settle_cross_terminal(state, status="done", halt_reason="ignored")
    assert "halt_reason" not in state


def test_settle_cross_terminal_sets_reason_and_halted_at() -> None:
    state = {"phase_handoff": {"id": "cfa:1"}}
    settle_cross_terminal(
        state,
        status="halted",
        halt_reason="phase_handoff_halt",
        halted_at="2026-06-08T10:00:00+00:00",
    )
    assert state["halt_reason"] == "phase_handoff_halt"
    assert state["halted_at"] == "2026-06-08T10:00:00+00:00"


# ── Stage 3b regression guard: single-project writers PRESERVE handoff ──


def test_single_project_writers_preserve_handoff() -> None:
    failed = {
        "status": "running",
        "phase_handoff": {"id": "h1", "phase": "validate_plan"},
    }
    mark_run_failed(failed, halt_reason="phase_failure:RuntimeError")
    assert failed["phase_handoff"] == {"id": "h1", "phase": "validate_plan"}

    interrupted = {
        "status": "running",
        "phase_handoff": {"id": "h1", "phase": "validate_plan"},
    }
    mark_run_interrupted(interrupted, interrupted_at="2026-06-08T10:00:00")
    assert interrupted["phase_handoff"] == {"id": "h1", "phase": "validate_plan"}
