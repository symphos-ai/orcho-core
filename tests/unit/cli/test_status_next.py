"""``orcho status`` ends with a ``Next:`` block derived from ``run_diagnosis``.

Unit tests drive :func:`cli._status_next.next_step_lines` with hand-built
``RunDiagnosis`` values, one per core condition. Integration tests run
``cmd_status`` against a runspace: a real producer-parked delivery gate (git
worktree + ``meta.commit_delivery``), a diagnosis that raises, a plain
``done`` run, and a monkeypatched ``delivery_inconsistent`` verdict.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import orcho
from cli._status_next import diagnosis_unavailable_line, next_step_lines
from core.io.ansi import strip_ansi
from pipeline.engine.commit_delivery import resolve_commit_delivery
from sdk.run_control.diagnosis import (
    CONDITION_ACTIVE,
    CONDITION_BLOCKED_WORKTREE,
    CONDITION_CLOSED_BY_FOLLOWUP,
    CONDITION_CORRECTION_FOLLOWUP_REQUIRED,
    CONDITION_DELIVERY_INCONSISTENT,
    CONDITION_NEEDS_DECISION,
    CONDITION_NEEDS_DELIVERY_DECISION,
    CONDITION_RECOVER_VIA_SOURCE_RUN,
    CONDITION_RESUME_INERT_TERMINAL,
    CONDITION_STALLED,
    CONDITION_SUPERSEDED_BY_CHILD,
)
from sdk.run_control.types import RunDiagnosis

RUN = "20260908_131908_4064f0"


def _diag(condition: str, **fields) -> RunDiagnosis:
    fields.setdefault("run_id", RUN)
    fields.setdefault("reason", f"reason for {condition}")
    return RunDiagnosis(condition=condition, **fields)


# ── next_step_lines: one case per condition ──────────────────────────────────


def test_active_has_no_next_step() -> None:
    assert next_step_lines(_diag(CONDITION_ACTIVE, status="running")) == []


def test_needs_delivery_decision_names_decide_and_the_sdk_actions() -> None:
    lines = next_step_lines(_diag(
        CONDITION_NEEDS_DELIVERY_DECISION,
        available_actions=("approve", "apply", "skip", "halt"),
    ))
    assert lines == [
        f"decide the parked delivery gate — orcho delivery decide {RUN} <action>",
        "available actions: approve, apply, skip, halt",
        f"details: orcho delivery gate {RUN}",
    ]


def test_needs_delivery_decision_with_no_actions_prints_a_dash() -> None:
    lines = next_step_lines(_diag(CONDITION_NEEDS_DELIVERY_DECISION))
    assert lines[1] == "available actions: -"


def test_correction_followup_required_repeats_core_reason() -> None:
    reason = "correction gate dead-ended; start an ordinary follow-up run"
    assert next_step_lines(
        _diag(CONDITION_CORRECTION_FOLLOWUP_REQUIRED, reason=reason),
    ) == [reason]


def test_delivery_inconsistent_extracts_the_backticked_command() -> None:
    reason = (
        "delivery commit abc123def456 exists for this run but the run records "
        f"no matching delivery (x); record it with `orcho reconcile-delivery {RUN} "
        "--commit abc123def456 --apply` after verifying it"
    )
    assert next_step_lines(_diag(CONDITION_DELIVERY_INCONSISTENT, reason=reason)) == [
        "record the existing delivery commit — "
        f"orcho reconcile-delivery {RUN} --commit abc123def456 --apply",
    ]


def test_delivery_inconsistent_without_backticks_uses_whole_reason() -> None:
    lines = next_step_lines(_diag(CONDITION_DELIVERY_INCONSISTENT, reason="plain"))
    assert lines == ["record the existing delivery commit — plain"]


def test_needs_decision_names_handoff_actions_and_resume() -> None:
    lines = next_step_lines(_diag(
        CONDITION_NEEDS_DECISION,
        handoff_id="review_changes:repair_round:2",
        available_actions=("approve", "reject"),
    ))
    assert lines == [
        "decide the pending phase handoff review_changes:repair_round:2 "
        f"(actions: approve, reject) then orcho run --resume {RUN}",
    ]


def test_stalled_points_at_repair_state() -> None:
    assert next_step_lines(_diag(CONDITION_STALLED, status="running")) == [
        f"orcho repair-state {RUN}",
    ]


def test_resume_inert_terminal_is_inspect_only() -> None:
    lines = next_step_lines(_diag(
        CONDITION_RESUME_INERT_TERMINAL, status="done", recommended_run_id=RUN,
    ))
    assert lines == [f"inspect only — orcho evidence {RUN}"]
    assert "--resume" not in " ".join(lines)


def test_closed_by_followup_is_inspect_only_and_names_the_child() -> None:
    lines = next_step_lines(_diag(
        CONDITION_CLOSED_BY_FOLLOWUP, status="halted", recommended_run_id="child_1",
    ))
    assert lines == [f"inspect only — orcho evidence {RUN} (superseded by child_1)"]
    assert "--resume" not in " ".join(lines)


def test_superseded_by_child_resumes_the_child() -> None:
    lines = next_step_lines(_diag(
        CONDITION_SUPERSEDED_BY_CHILD, recommended_run_id="child_1",
    ))
    assert lines == ["orcho run --resume child_1"]


def test_recover_via_source_run_resume_source() -> None:
    lines = next_step_lines(_diag(
        CONDITION_RECOVER_VIA_SOURCE_RUN,
        recommended_next_action="resume_source_run",
        recommended_run_id="source_1",
    ))
    assert lines == ["orcho run --resume source_1"]


def test_recover_via_source_run_plan_artifact_uses_from_run_plan() -> None:
    lines = next_step_lines(_diag(
        CONDITION_RECOVER_VIA_SOURCE_RUN,
        recommended_next_action="plan_artifact_continuation",
        recommended_run_id="source_1",
    ))
    assert lines == ["orcho run --from-run-plan source_1 --project <dir>"]


def test_recover_via_source_run_other_action_falls_back_to_reason() -> None:
    lines = next_step_lines(_diag(
        CONDITION_RECOVER_VIA_SOURCE_RUN,
        recommended_next_action="stop_unknown",
        reason="missing facts",
    ))
    assert lines == ["missing facts"]


def test_blocked_worktree_repeats_core_reason() -> None:
    assert next_step_lines(
        _diag(CONDITION_BLOCKED_WORKTREE, reason="worktree is busy"),
    ) == ["worktree is busy"]


@pytest.mark.parametrize("status", ["halted", "failed", "interrupted"])
def test_residual_resumable_stop_resumes_this_run(status: str) -> None:
    lines = next_step_lines(_diag(status, status=status))
    assert lines == [f"orcho run --resume {RUN}"]


def test_interrupted_before_checkpoint_continues_from_run_plan() -> None:
    # Core rules out a plain resume for a phase interrupted before a
    # resumable checkpoint and recommends the plan-artifact continuation.
    lines = next_step_lines(_diag(
        "interrupted",
        status="interrupted",
        recommended_next_action="plan_artifact_continuation",
        recommended_run_id=RUN,
    ))
    assert lines == [f"orcho run --from-run-plan {RUN} --project <dir>"]
    assert not any("--resume" in line for line in lines)


def test_unknown_condition_falls_back_to_reason() -> None:
    lines = next_step_lines(_diag("something_new", status="odd", reason="core says so"))
    assert lines == ["core says so"]


def test_diagnosis_unavailable_line() -> None:
    assert diagnosis_unavailable_line("RuntimeError: boom") == (
        "(diagnosis unavailable: RuntimeError: boom)"
    )


# ── cmd_status integration ───────────────────────────────────────────────────


def _args(**kwargs) -> SimpleNamespace:
    return SimpleNamespace(
        run_id=kwargs.pop("run_id", None),
        workspace=kwargs.pop("workspace", None),
        verbose=kwargs.pop("verbose", False),
        **kwargs,
    )


@pytest.fixture
def runs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    rd = tmp_path / "runs"
    rd.mkdir()
    monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))
    return rd


def _write_meta(runs_dir: Path, run_id: str, meta: dict) -> Path:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return run_dir


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _park_delivery_gate(tmp_path: Path, runs_dir: Path, run_id: str) -> None:
    """A producer-parked deferred delivery gate (ADR 0175 addendum shape)."""
    from core.io.git_helpers import create_worktree

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@orcho.invalid")
    _git(repo, "config", "user.name", "Orcho Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")

    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    result = create_worktree(
        repo=repo,
        base_ref=_git(repo, "rev-parse", "HEAD"),
        target_path=run_dir / "checkout",
        branch_name=f"orcho/run/{run_id}",
    )
    assert result.ok, result.error
    wt = run_dir / "checkout"
    (wt / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=wt,
        run_dir=run_dir,
        run_id=run_id,
        session={
            "status": "done",
            "phases": {"final_acceptance": {"verdict": "APPROVED", "short_summary": "feat: x"}},
        },
        commit_config={"enabled": True, "auto_in_ci": "approve", "add_untracked": True},
        no_interactive=True,
        decision_mode="defer",
    )
    ctx = decision.to_dict()
    ctx["decided_at"] = "2026-07-29T09:31:22+00:00"
    _write_meta(runs_dir, run_id, {
        "task": "ship it",
        "project": str(repo),
        "profile": "small_task",
        "timestamp": "2026-09-08T13:19:08",
        "status": "halted",
        "halt_reason": "commit_delivery_pending",
        "phases": {"implement": [{}]},
        "commit_delivery": ctx,
    })


def test_status_on_parked_gate_names_decide_and_sdk_actions(
    tmp_path: Path, runs_dir: Path, capsys,
) -> None:
    _park_delivery_gate(tmp_path, runs_dir, RUN)

    assert orcho.cmd_status(_args(run_id=RUN)) == 0

    out = strip_ansi(capsys.readouterr().out)
    tail = out[out.index("Next:"):]
    assert f"Next: decide the parked delivery gate — orcho delivery decide {RUN} <action>" in tail
    assert "available actions: approve, apply, skip, halt" in tail
    assert f"orcho delivery gate {RUN}" in tail
    assert "--resume" not in tail


def test_status_degrades_when_diagnosis_raises(runs_dir: Path, capsys, monkeypatch) -> None:
    _write_meta(runs_dir, RUN, {
        "task": "t", "project": "/p", "profile": "small_task",
        "timestamp": "2026-09-08T13:19:08", "status": "halted", "phases": {},
    })

    def _boom(*_a, **_k):
        raise RuntimeError("no verdict today")

    monkeypatch.setattr("sdk.run_control.run_diagnosis", _boom)

    assert orcho.cmd_status(_args(run_id=RUN)) == 0

    captured = capsys.readouterr()
    out = strip_ansi(captured.out)
    assert "Next: (diagnosis unavailable: RuntimeError: no verdict today)" in out
    assert "Traceback" not in captured.err
    assert "Traceback" not in out
    assert f"Run:     {RUN}" in out


def test_status_on_done_run_never_suggests_resume(runs_dir: Path, capsys) -> None:
    _write_meta(runs_dir, RUN, {
        "task": "t", "project": "/p", "profile": "small_task",
        "timestamp": "2026-09-08T13:19:08", "status": "done",
        "phases": {"implement": [{}]},
    })

    assert orcho.cmd_status(_args(run_id=RUN)) == 0

    out = strip_ansi(capsys.readouterr().out)
    assert "--resume" not in out
    assert f"Next: inspect only — orcho evidence {RUN}" in out


def test_status_delivery_inconsistent_names_reconcile_command(
    runs_dir: Path, capsys, monkeypatch,
) -> None:
    _write_meta(runs_dir, RUN, {
        "task": "t", "project": "/p", "profile": "small_task",
        "timestamp": "2026-09-08T13:19:08", "status": "failed", "phases": {},
    })
    sha = "0123456789ab"

    def _inconsistent(run_id, **_k):
        return RunDiagnosis(
            run_id=run_id,
            condition=CONDITION_DELIVERY_INCONSISTENT,
            reason=(
                f"delivery commit {sha} exists for this run but the run records no "
                f"matching delivery (legacy_commit); record it with `orcho "
                f"reconcile-delivery {run_id} --commit {sha} --apply` after verifying "
                "it — do not resume or decide delivery first"
            ),
            status="failed",
            recommended_next_action="reconcile_delivery",
            recommended_run_id=run_id,
        )

    monkeypatch.setattr("sdk.run_control.run_diagnosis", _inconsistent)

    assert orcho.cmd_status(_args(run_id=RUN)) == 0

    out = strip_ansi(capsys.readouterr().out)
    assert f"orcho reconcile-delivery {RUN} --commit {sha} --apply" in out
    assert "Next: record the existing delivery commit" in out
    assert "--resume" not in out


def test_status_calls_run_diagnosis_exactly_once(runs_dir: Path, capsys, monkeypatch) -> None:
    _write_meta(runs_dir, RUN, {
        "task": "t", "project": "/p", "profile": "small_task",
        "timestamp": "2026-09-08T13:19:08", "status": "running", "phases": {},
    })
    calls: list[str] = []

    def _stalled(run_id, **_k):
        calls.append(run_id)
        return RunDiagnosis(
            run_id=run_id, condition=CONDITION_STALLED,
            reason="pid 1 is no longer alive", status="running",
        )

    monkeypatch.setattr("sdk.run_control.run_diagnosis", _stalled)

    assert orcho.cmd_status(_args(run_id=RUN)) == 0

    out = strip_ansi(capsys.readouterr().out)
    assert calls == [RUN]
    assert "Stalled: pid 1 is no longer alive" in out
    assert f"Next: orcho repair-state {RUN}" in out


def test_status_next_block_precedes_detailed_meta(runs_dir: Path, capsys) -> None:
    _write_meta(runs_dir, RUN, {
        "task": "t", "project": "/p", "profile": "small_task",
        "timestamp": "2026-09-08T13:19:08", "status": "done",
        "phases": {"implement": [{}]},
    })

    assert orcho.cmd_status(_args(run_id=RUN, verbose=True)) == 0

    out = strip_ansi(capsys.readouterr().out)
    assert out.index("Paths:") < out.index("Next:") < out.index("Detailed Meta:")
