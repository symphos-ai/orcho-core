from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pipeline.project.correction_followup import compose_correction_context
from sdk.errors import LaunchError
from sdk.run_control.launch import (
    CorrectionFollowupLaunchRequest,
    launch_correction_followup,
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
