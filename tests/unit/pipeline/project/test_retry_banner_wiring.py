"""Post-retry outcome classification + pre/post banner wiring (T3 / F1).

``_classify_retry_outcome`` picks the post-banner variant from the resume
outcome + the run's event stream. ``_begin_retry_banner`` /
``_finish_retry_banner`` (used by ``apply_phase_handoff_resume_with_banners``)
emit the operator banners on BOTH the interactive in-process path and the
checkpoint/preflight resume path — driven by the persisted decision
artifact, not the live prompt — so a checkpoint resume with an already
recorded ``retry_feedback`` decision gets the same banners.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.control.handoff_banners import RetryOutcome
from pipeline.project.handoff import (
    PhaseHandoffResumeOutcome,
    _begin_retry_banner,
    _classify_retry_outcome,
    _finish_retry_banner,
)


@pytest.fixture(autouse=True)
def _live_output_mode():
    """Pin the full multi-line banner shape.

    ``summary`` (the default run-output mode) collapses the retry banners
    emitted by ``_begin_retry_banner`` / ``_finish_retry_banner`` to a
    two-line presenter card; this file asserts the full banner, so force
    ``live`` and restore.
    """
    from core.observability import logging as _logging

    _before = _logging.get_output_mode()
    _logging._output_mode = "live"
    try:
        yield
    finally:
        _logging._output_mode = _before


def _run_with_events(tmp_path: Path, kinds: list[str]) -> SimpleNamespace:
    out = tmp_path / "run"
    out.mkdir()
    lines = [
        json.dumps({"seq": i + 1, "ts": "t", "kind": k, "phase": None,
                    "payload": {}})
        for i, k in enumerate(kinds)
    ]
    (out / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return SimpleNamespace(output_dir=out)


def test_rejected_again_dominates(tmp_path) -> None:
    run = _run_with_events(tmp_path, ["phase.start"])
    assert _classify_retry_outcome(run, since=0, paused=True) is (
        RetryOutcome.REJECTED_AGAIN
    )


def test_provider_fallback_detected_when_not_paused(tmp_path) -> None:
    run = _run_with_events(
        tmp_path, ["phase.start", "phase.provider_session_fallback"],
    )
    assert _classify_retry_outcome(run, since=1, paused=False) is (
        RetryOutcome.PROVIDER_FALLBACK
    )


def test_fallback_before_since_is_ignored(tmp_path) -> None:
    # The fallback event predates the retry window (index 0 < since=1).
    run = _run_with_events(
        tmp_path, ["phase.provider_session_fallback", "phase.start"],
    )
    assert _classify_retry_outcome(run, since=1, paused=False) is (
        RetryOutcome.APPROVED
    )


def test_approved_when_no_fallback(tmp_path) -> None:
    run = _run_with_events(tmp_path, ["phase.start", "phase.end"])
    assert _classify_retry_outcome(run, since=0, paused=False) is (
        RetryOutcome.APPROVED
    )


def test_no_output_dir_degrades_to_approved() -> None:
    run = SimpleNamespace(output_dir=None)
    assert _classify_retry_outcome(run, since=0, paused=False) is (
        RetryOutcome.APPROVED
    )


# ── pre/post banners driven by the persisted decision artifact (F1) ────


def _make_resume_run(
    tmp_path: Path, *, action: str, phase: str = "review_changes",
    round_n: int = 1, loop_max: int = 1,
    worktree: dict | None = None,
) -> SimpleNamespace:
    """Run stub whose decision artifact is recorded via the SDK, mirroring
    a checkpoint/preflight resume (decision already on disk, no live prompt)."""
    from sdk.phase_handoff import phase_handoff_decide

    runs = tmp_path / "runs"
    rd = runs / "20260101_000000"
    rd.mkdir(parents=True)
    handoff_id = f"{phase}:repair_round:{round_n}"
    payload = {
        "id": handoff_id, "phase": phase,
        "type": "human_feedback_on_reject", "trigger": "rejected",
        "verdict": "REJECTED", "approved": False,
        "round_extras_key": "repair_round", "round": round_n,
        "loop_max_rounds": loop_max,
        "available_actions": ["continue", "retry_feedback", "halt"],
        "artifacts": {}, "last_output": "crit",
    }
    meta = {"status": "awaiting_phase_handoff", "phase_handoff": payload,
            "task": "t", "project": "/p"}
    (rd / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    feedback = "please tighten the retry path" if action in (
        "retry_feedback", "continue_with_waiver") else None
    phase_handoff_decide(
        rd.name, handoff_id, action, feedback=feedback, runs_dir=runs, cwd=None,
    )
    session = {"phase_handoff": payload}
    if worktree is not None:
        session["worktree"] = worktree
    return SimpleNamespace(
        session=session, output_dir=rd,
        session_ts=rd.name, _ckpt=None,
    )


def test_begin_retry_banner_prints_for_retry_feedback(tmp_path, capsys) -> None:
    run = _make_resume_run(tmp_path, action="retry_feedback", phase="review_changes")
    ctx = _begin_retry_banner(run)
    out = capsys.readouterr().out
    assert ctx is not None
    assert "retry_feedback" in out
    assert "repair_changes → review_changes retry" in out
    assert "please tighten the retry path" in out


def test_begin_retry_banner_threads_retained_worktree_path(tmp_path, capsys) -> None:
    run = _make_resume_run(
        tmp_path,
        action="retry_feedback",
        phase="review_changes",
        worktree={"isolation": "per_run", "path": "/wt/wt_orig/checkout"},
    )
    _begin_retry_banner(run)
    out = capsys.readouterr().out
    # Path threaded from session['worktree']['path'] as the retained subject.
    assert "worktree        : retained retry subject /wt/wt_orig/checkout" in out
    # The provider-session line is still present and independent.
    assert "provider session:" in out


def test_begin_retry_banner_in_place_when_isolation_off(tmp_path, capsys) -> None:
    run = _make_resume_run(
        tmp_path,
        action="retry_feedback",
        phase="review_changes",
        worktree={"isolation": "off", "path": "/repo/src"},
    )
    _begin_retry_banner(run)
    out = capsys.readouterr().out
    assert "worktree        : in-place checkout /repo/src" in out


def test_begin_retry_banner_falls_back_to_git_cwd(tmp_path, capsys) -> None:
    # No worktree block recorded -> fall back to run.git_cwd (in-place).
    run = _make_resume_run(tmp_path, action="retry_feedback", phase="review_changes")
    run.git_cwd = "/repo/src"
    _begin_retry_banner(run)
    out = capsys.readouterr().out
    assert "worktree        : in-place checkout /repo/src" in out


def test_begin_retry_banner_plan_kind(tmp_path, capsys) -> None:
    run = _make_resume_run(tmp_path, action="retry_feedback", phase="validate_plan")
    _begin_retry_banner(run)
    assert "plan → validate_plan retry" in capsys.readouterr().out


def test_begin_retry_banner_none_for_continue(tmp_path, capsys) -> None:
    run = _make_resume_run(tmp_path, action="continue")
    assert _begin_retry_banner(run) is None
    assert "retry_feedback" not in capsys.readouterr().out


def test_finish_retry_banner_rejected_again(tmp_path, capsys) -> None:
    run = SimpleNamespace(session_ts="r", output_dir=tmp_path)
    outcome = PhaseHandoffResumeOutcome(
        profile=None, completed_phases=frozenset(), paused=True,
    )
    _finish_retry_banner(run, ("h", "review_changes", 0), outcome)
    assert "rejected_again" in capsys.readouterr().out


def test_finish_retry_banner_noop_without_context(capsys) -> None:
    run = SimpleNamespace(session_ts="r", output_dir=None)
    outcome = PhaseHandoffResumeOutcome(
        profile=None, completed_phases=frozenset(), paused=False,
    )
    _finish_retry_banner(run, None, outcome)
    assert capsys.readouterr().out == ""
