from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pipeline.project.correction_followup import compose_correction_context
from sdk.errors import LaunchError
from sdk.run_control.launch import (
    CorrectionFollowupLaunchRequest,
    FromRunPlanLaunchRequest,
    launch_correction_followup,
    launch_from_run_plan,
    resume_run,
)


def _parent(runs_dir: Path, *, halt_reason: str = "final_acceptance_rejected") -> Path:
    source = runs_dir.parent / "source"
    source.mkdir(parents=True)
    worktree = runs_dir.parent / "worktree"
    worktree.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    (worktree / "change.txt").write_text("change\n")
    parent = runs_dir / "parent"
    parent.mkdir(parents=True)
    (parent / "meta.json").write_text(json.dumps({
        "status": "halted", "halt_reason": halt_reason,
        "project": str(source), "task": "Fix the rejected change",
        "worktree": {"path": str(worktree), "isolation": "per_run"},
        "phases": {"final_acceptance": {
            "short_summary": "release rejected", "verification_gaps": [{
                "required_check": "run tests", "risk": "regression",
            }],
        }},
        "commit_delivery": {"release_blockers": [{"id": "RB1"}]},
    }), encoding="utf-8")
    return parent


def test_launch_builds_correction_argv_and_context(tmp_path: Path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    _parent(runs)
    captured: dict[str, object] = {}

    class _Popen:
        pid = 12345

    def _spawn(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _Popen()

    monkeypatch.setattr("sdk.run_control.launch._spawn_detached", _spawn)
    result = launch_correction_followup(
        CorrectionFollowupLaunchRequest(
            parent_run_id="parent", runs_dir=str(runs),
            operator_comment="Please address the failing verification.",
        ),
        run_id="child",
    )

    command = captured["cmd"]
    assert isinstance(command, list)
    assert "--resume" in command and command[command.index("--resume") + 1] == "parent"
    assert "--profile" in command and command[command.index("--profile") + 1] == "correction"
    assert "--no-interactive" in command
    context = (runs / "child" / "correction_context.md").read_text()
    assert "release rejected" in context
    assert "Operator comment" in context
    assert "Please address" in context
    assert result.run.run_id == "child"


def test_launch_uses_argv_profile_without_exporting_pipeline_env(
    tmp_path: Path, monkeypatch,
) -> None:
    runs = tmp_path / "runs"
    _parent(runs)
    captured: dict[str, object] = {}

    class _Popen:
        pid = 12345

    def _spawn(_cmd, **kwargs):
        captured.update(kwargs)
        return _Popen()

    monkeypatch.setenv("ORCHO_PIPELINE", "task")
    monkeypatch.setattr("sdk.run_control.launch._spawn_detached", _spawn)

    launch_correction_followup(
        CorrectionFollowupLaunchRequest(
            parent_run_id="parent", runs_dir=str(runs), operator_comment="Fix it.",
        ),
        run_id="child",
    )

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["ORCHO_RUN_ID"] == "child"
    assert "ORCHO_PIPELINE" not in env


def test_launch_refuses_empty_comment_before_spawn(tmp_path: Path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    _parent(runs)
    monkeypatch.setattr(
        "sdk.run_control.launch._spawn_detached",
        lambda *args, **kwargs: pytest.fail("must not spawn"),
    )

    with pytest.raises(LaunchError, match="operator_comment"):
        launch_correction_followup(
            CorrectionFollowupLaunchRequest(parent_run_id="parent", runs_dir=str(runs), operator_comment=" "),
        )


def test_launch_refuses_blocked_parent_before_spawn(tmp_path: Path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    parent = _parent(runs)
    meta = json.loads((parent / "meta.json").read_text())
    meta["worktree"]["isolation"] = "off"
    (parent / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(
        "sdk.run_control.launch._spawn_detached",
        lambda *args, **kwargs: pytest.fail("must not spawn"),
    )

    with pytest.raises(LaunchError, match="not an isolated"):
        launch_correction_followup(
            CorrectionFollowupLaunchRequest(parent_run_id="parent", runs_dir=str(runs), operator_comment="fix it"),
        )


def test_fix_parent_context_uses_delivery_gate_blockers() -> None:
    context = compose_correction_context({
        "status": "halted", "halt_reason": "commit_decision_fix",
        "commit_delivery": {"release_blockers": [{"id": "RB-fix"}]},
    })

    assert "Persisted Release Blockers" in context
    assert "RB-fix" in context


def test_followup_refuses_parent_id_before_spawn(tmp_path: Path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    _parent(runs)
    monkeypatch.setattr(
        "sdk.run_control.launch._spawn_detached",
        lambda *args, **kwargs: pytest.fail("must not spawn"),
    )

    with pytest.raises(LaunchError, match="must differ"):
        launch_correction_followup(
            CorrectionFollowupLaunchRequest("parent", "comment", runs_dir=str(runs)),
            run_id="parent",
        )


def test_from_run_plan_launch_is_fresh_and_not_a_resume(tmp_path: Path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    parent = _parent(runs, halt_reason="other")
    (parent / "parsed_plan.json").write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}

    class _Popen:
        pid = 12345

    def _spawn(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Popen()

    monkeypatch.setattr("sdk.run_control.launch._spawn_detached", _spawn)
    result = launch_from_run_plan(
        FromRunPlanLaunchRequest("parent", runs_dir=str(runs)), run_id="child",
    )

    command = captured["cmd"]
    assert isinstance(command, list)
    assert "--from-run-plan" in command
    assert command[command.index("--from-run-plan") + 1] == "parent"
    assert "--resume" not in command
    assert result.run.run_id == "child"
    assert result.run.run_dir == runs / "child"


def test_finalized_ledger_blocks_resume_before_spawn(tmp_path: Path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    run = runs / "parent"
    project = tmp_path / "project"
    project.mkdir()
    run.mkdir(parents=True)
    (run / "run_supervisor.json").write_text(json.dumps({"project_dir": str(project)}))
    (run / "meta.json").write_text(json.dumps({"task": "resume", "status": "failed"}))
    # A valid empty finalized ledger is enough: no subprocess can reopen it.
    (run / "scheduled_gate_ledger.json").write_text(json.dumps({
        "schema_version": "1", "finalized": True, "rows": [], "trail": [],
    }))
    monkeypatch.setattr(
        "sdk.run_control.launch._spawn_detached",
        lambda *args, **kwargs: pytest.fail("must not spawn"),
    )

    with pytest.raises(LaunchError, match="finalized scheduled-gate ledger"):
        resume_run("parent", runs_dir=str(runs))
