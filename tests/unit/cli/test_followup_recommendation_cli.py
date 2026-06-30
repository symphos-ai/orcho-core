"""Non-interactive follow-up lineage recommendation (T5).

On the non-interactive path the CLI must NOT silently switch the run id to
an in-progress follow-up child; it prints the recommended command instead.
"""

from __future__ import annotations

from pipeline.control.resume_context import ActiveFollowupChild
from pipeline.project.cli import _print_active_followup_recommendation


def _child(**kw) -> ActiveFollowupChild:
    base = dict(
        child_run_id="20260102_000000",
        child_status="interrupted",
        parent_run_id="20260101_000000",
        active_handoff_id="review_changes:repair_round:2",
    )
    base.update(kw)
    return ActiveFollowupChild(**base)


def test_prints_recommended_command(capsys) -> None:
    _print_active_followup_recommendation(
        _child(), parent_run_id="20260101_000000",
    )
    err = capsys.readouterr().err
    assert "in-progress follow-up 20260102_000000" in err
    assert "interrupted" in err
    assert "review_changes:repair_round:2" in err
    # The recommended copy-paste command resumes the child, not the parent.
    assert "orcho run --resume 20260102_000000" in err


def test_no_handoff_hint_when_absent(capsys) -> None:
    _print_active_followup_recommendation(
        _child(active_handoff_id=None), parent_run_id="20260101_000000",
    )
    err = capsys.readouterr().err
    assert "active handoff" not in err
    assert "orcho run --resume 20260102_000000" in err
