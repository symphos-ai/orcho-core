"""CLI wiring for the checkpoint-resume handoff preflight (T2).

Exercises the thin ``_handle_checkpoint_resume_preflight`` helper that the
project CLI calls before ``run_pipeline`` on a CHECKPOINT resume:

* non-interactive → prints a copy-pasteable hint and exits 4 without
  recording a decision (no mutation), instead of letting the resume trip
  ``load_handoff_decision_validated`` with a RuntimeError/traceback;
* interactive → shows the menu, records the decision via the SDK, and
  returns so the same command continues the resume;
* a run with a decision already recorded is a no-op (resume proceeds).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.project.cli import _handle_checkpoint_resume_preflight

_HANDOFF_ID = "validate_plan:plan_round:2"


def _make_run(tmp_path: Path, *, status: str = "interrupted") -> tuple[Path, Path]:
    runs = tmp_path / "runs"
    run_dir = runs / "20260101_000000"
    run_dir.mkdir(parents=True)
    meta = {
        "status": status,
        "phase_handoff": {
            "id": _HANDOFF_ID,
            "phase": "validate_plan",
            "type": "human_feedback_on_reject",
            "trigger": "rejected",
            "verdict": "REJECTED",
            "approved": False,
            "round_extras_key": "plan_round",
            "round": 2,
            "loop_max_rounds": 2,
            "available_actions": ["continue", "retry_feedback", "halt"],
            "artifacts": {},
            "last_output": "crit",
        },
        "task": "t",
        "project": "/p",
    }
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return runs, run_dir


def _meta(run_dir: Path) -> dict:
    return json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))


def test_noninteractive_prints_hint_and_exits_4_no_mutation(
    tmp_path, capsys,
) -> None:
    _runs, run_dir = _make_run(tmp_path, status="interrupted")
    before = (run_dir / "meta.json").read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        _handle_checkpoint_resume_preflight(
            run_id=run_dir.name,
            run_dir=run_dir,
            meta=_meta(run_dir),
            no_interactive=True,
        )
    assert exc.value.code == 4

    err = capsys.readouterr().err
    assert _HANDOFF_ID in err
    assert "available_actions" in err
    assert "phase_handoff_decide" in err

    # No decision recorded, meta untouched.
    assert not (run_dir / "phase_handoff_decisions").exists()
    assert (run_dir / "meta.json").read_text(encoding="utf-8") == before


def test_interactive_records_decision_and_continues(
    tmp_path, monkeypatch,
) -> None:
    runs, run_dir = _make_run(tmp_path, status="interrupted")

    from pipeline.control import handoff_prompt as _hp

    # Force the TTY gate so the interactive path is exercised under pytest
    # (whose stdin/stdout are not real TTYs).
    monkeypatch.setattr(
        _hp, "should_prompt_for_phase_handoff", lambda **_k: True,
    )
    monkeypatch.setattr(
        _hp, "prompt_phase_handoff_action",
        lambda *_a, **_k: _hp.HandoffDecisionInput(
            action="continue", feedback=None, note="cli preflight test",
        ),
    )

    # Returns (no SystemExit) → the same command will continue the resume.
    _handle_checkpoint_resume_preflight(
        run_id=run_dir.name,
        run_dir=run_dir,
        meta=_meta(run_dir),
        no_interactive=False,
    )

    decisions = list((run_dir / "phase_handoff_decisions").glob("*.json"))
    assert len(decisions) == 1
    recorded = json.loads(decisions[0].read_text(encoding="utf-8"))
    assert recorded["action"] == "continue"
    assert recorded["handoff_id"] == _HANDOFF_ID


def test_aborted_prompt_exits_4_no_mutation(tmp_path, monkeypatch) -> None:
    _runs, run_dir = _make_run(tmp_path, status="interrupted")

    from pipeline.control import handoff_prompt as _hp

    monkeypatch.setattr(
        _hp, "should_prompt_for_phase_handoff", lambda **_k: True,
    )
    monkeypatch.setattr(
        _hp, "prompt_phase_handoff_action",
        lambda *_a, **_k: _hp.HANDOFF_PROMPT_ABORTED,
    )

    with pytest.raises(SystemExit) as exc:
        _handle_checkpoint_resume_preflight(
            run_id=run_dir.name,
            run_dir=run_dir,
            meta=_meta(run_dir),
            no_interactive=False,
        )
    assert exc.value.code == 4
    assert not (run_dir / "phase_handoff_decisions").exists()


def test_non_tty_without_flag_prints_hint_no_mutation(
    tmp_path, monkeypatch,
) -> None:
    """F1: a piped / CI run (non-TTY stdin/stdout) WITHOUT
    ``--no-interactive`` must take the hint path and never mutate the run —
    the same gate a freshly fired handoff uses, not the bare flag."""
    import io
    import sys

    _runs, run_dir = _make_run(tmp_path, status="interrupted")
    before = (run_dir / "meta.json").read_text(encoding="utf-8")

    # Non-TTY streams (io.StringIO.isatty() is False); flag NOT set.
    monkeypatch.setattr(sys, "stdin", io.StringIO("1\n"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    from pipeline.control import handoff_prompt as _hp
    prompted: list[int] = []
    monkeypatch.setattr(
        _hp, "prompt_phase_handoff_action",
        lambda *_a, **_k: prompted.append(1),
    )

    with pytest.raises(SystemExit) as exc:
        _handle_checkpoint_resume_preflight(
            run_id=run_dir.name,
            run_dir=run_dir,
            meta=_meta(run_dir),
            no_interactive=False,
        )
    assert exc.value.code == 4
    # Prompt never invoked → no decision recorded → run unmutated.
    assert prompted == []
    assert not (run_dir / "phase_handoff_decisions").exists()
    assert (run_dir / "meta.json").read_text(encoding="utf-8") == before


def test_no_preflight_when_decision_exists(tmp_path) -> None:
    from sdk.phase_handoff import phase_handoff_decide

    runs, run_dir = _make_run(tmp_path, status="interrupted")
    phase_handoff_decide(
        run_dir.name, _HANDOFF_ID, "continue", runs_dir=runs, cwd=None,
    )

    # No SystemExit, no prompt — resume proceeds normally.
    _handle_checkpoint_resume_preflight(
        run_id=run_dir.name,
        run_dir=run_dir,
        meta=_meta(run_dir),
        no_interactive=True,
    )


def test_no_preflight_for_terminal_run(tmp_path) -> None:
    _runs, run_dir = _make_run(tmp_path, status="done")
    _handle_checkpoint_resume_preflight(
        run_id=run_dir.name,
        run_dir=run_dir,
        meta=_meta(run_dir),
        no_interactive=True,
    )
