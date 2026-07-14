"""Project isolation setup wires plugin-declared worktree bootstrap."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents.runtimes import MockAgentProvider
from pipeline.engine.pre_run_dirty import PreRunDirtyIntake
from pipeline.engine.worktree_bootstrap import WorktreeBootstrapError
from pipeline.plugins import PluginConfig
from pipeline.project.app import run_project_pipeline
from pipeline.project.isolation_setup import (
    _apply_worktree_bootstrap,
    setup_isolation,
)
from pipeline.project.types import PresentationPolicy, ProjectRunRequest
from pipeline.runtime import PhaseStep, Profile
from pipeline.runtime.profile import ExecutionPolicy


def _setup_isolation_kwargs(
    *, session: dict, output_dir: Path, git_root: Path, presentation,
) -> dict:
    """Minimal kwargs that drive ``setup_isolation`` to a pre-run-dirty halt."""
    return {
        "session": session,
        "output_dir": output_dir,
        "session_ts": "run1",
        "git_root": git_root,
        "followup_parent_worktree": None,
        "worktree_config_override": {"enabled": True, "isolation": "per_run"},
        "v2_profile": SimpleNamespace(worktree_isolation=None, sandbox=None),
        "resume_mode": None,
        "resume_from": None,
        "no_interactive": True,
        "parent_run_id": None,
        "project_alias": None,
        "followup_parent_run_id": None,
        "followup_parent_run_dir": None,
        "worktree_bootstrap_config": None,
        "presentation": presentation,
    }


def _init_repo_with_ignored_libs(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@orcho.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Orcho Test"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=path,
        check=True,
    )
    (path / ".gitignore").write_text("libs/\n", encoding="utf-8")
    (path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (path / "libs").mkdir()
    (path / "libs" / "native.dll").write_bytes(b"dll")
    subprocess.run(["git", "add", ".gitignore", "app.py"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def test_plugin_bootstrap_copies_ignored_libs_into_isolated_worktree(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "project"
    _init_repo_with_ignored_libs(project)
    run_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs" / "run1"
    plugin = PluginConfig(
        name="Native Project",
        worktree_bootstrap=[{"copy": "libs"}],
    )

    monkeypatch.setenv("ORCHO_RUN_ID", "run1")
    with patch("pipeline.project.session_run.load_plugin", return_value=plugin):
        session = run_project_pipeline(
            ProjectRunRequest(
                task="touch nothing",
                project_dir=str(project),
                output_dir=run_dir,
                profile_name="isolated-bootstrap-test",
                profile_obj=Profile(
                    name="isolated-bootstrap-test",
                    steps=(PhaseStep(
                        "implement",
                        execution_policy=ExecutionPolicy(
                            mode="linear",
                            session_continuity="same_zone_continue",
                        ),
                    ),),
                    worktree_isolation="per_run",
                ),
                provider=MockAgentProvider(latency=0.0),
                presentation=PresentationPolicy.SILENT,
                no_interactive=True,
            ),
        ).session

    checkout = Path(session["worktree"]["path"])
    assert (checkout / "libs" / "native.dll").read_bytes() == b"dll"
    assert session["worktree_bootstrap"]["status"] == "ok"
    assert session["worktree_bootstrap"]["steps"][0]["action"] == "copy"


def test_bootstrap_failure_silent_persists_session_then_reraises(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = {"phase_handoff": {"pending": "decision"}, "status": "running"}
    worktree_ctx = SimpleNamespace(is_isolated=True, path=tmp_path)

    with patch(
        "pipeline.engine.worktree_bootstrap.run_worktree_bootstrap",
        side_effect=WorktreeBootstrapError("boom"),
    ), pytest.raises(WorktreeBootstrapError, match="boom"):
        _apply_worktree_bootstrap(
            config=[{"copy": "libs"}],
            session=session,
            output_dir=run_dir,
            git_root=tmp_path,
            worktree_ctx=worktree_ctx,
            presentation=PresentationPolicy.SILENT,
        )

    # In-memory session: failure payload + terminal halt, stale handoff gone.
    assert session["worktree_bootstrap"] == {"status": "failed", "error": "boom"}
    assert session["status"] == "halted"
    assert session["halt_reason"] == "worktree_bootstrap_failed"
    assert "phase_handoff" not in session

    # SILENT re-raises AFTER persisting: meta.json already carries the halt.
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "halted"
    assert meta["halt_reason"] == "worktree_bootstrap_failed"
    assert meta["worktree_bootstrap"] == {"status": "failed", "error": "boom"}
    assert "phase_handoff" not in meta


def test_bootstrap_failure_terminal_exits_2_with_message(
    tmp_path: Path, capsys,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = {"phase_handoff": {"pending": "decision"}, "status": "running"}
    worktree_ctx = SimpleNamespace(is_isolated=True, path=tmp_path)

    with patch(
        "pipeline.engine.worktree_bootstrap.run_worktree_bootstrap",
        side_effect=WorktreeBootstrapError("boom"),
    ), pytest.raises(SystemExit) as exc_info:
        _apply_worktree_bootstrap(
            config=[{"copy": "libs"}],
            session=session,
            output_dir=run_dir,
            git_root=tmp_path,
            worktree_ctx=worktree_ctx,
            presentation=PresentationPolicy.TERMINAL,
        )

    assert exc_info.value.code == 2
    assert "Worktree bootstrap failed: boom" in capsys.readouterr().err
    assert session["status"] == "halted"
    assert session["halt_reason"] == "worktree_bootstrap_failed"
    assert "phase_handoff" not in session


def test_pre_run_dirty_halt_silent_is_quiet_and_clears_stale_phase_handoff(
    tmp_path: Path, capsys,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = {"phase_handoff": {"pending": "decision"}, "status": "running"}
    halted_intake = PreRunDirtyIntake(
        action="halt",
        status="halted",
        dirty=True,
        reason="operator halted dirty intake",
        changed_paths=("src/app.py",),
        untracked_paths=("notes.txt",),
    )

    with patch(
        "pipeline.engine.pre_run_dirty.resolve_pre_run_dirty_intake",
        return_value=halted_intake,
    ):
        result = setup_isolation(
            **_setup_isolation_kwargs(
                session=session,
                output_dir=run_dir,
                git_root=tmp_path,
                presentation=PresentationPolicy.SILENT,
            ),
        )

    assert result.halted is True
    assert session["status"] == "halted"
    assert session["halt_reason"] == "pre_run_dirty_halt"
    assert session["pre_run_dirty"]["action"] == "halt"
    assert "phase_handoff" not in session
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_pre_run_dirty_halt_terminal_prints_actionable_message(
    tmp_path: Path, capsys,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = {"phase_handoff": {"pending": "decision"}, "status": "running"}
    halted_intake = PreRunDirtyIntake(
        action="halt",
        status="halted",
        dirty=True,
        reason="non-interactive policy selected halt",
        changed_paths=("src/app.py", "pyproject.toml"),
        untracked_paths=("notes.txt",),
    )

    with patch(
        "pipeline.engine.pre_run_dirty.resolve_pre_run_dirty_intake",
        return_value=halted_intake,
    ):
        result = setup_isolation(
            **_setup_isolation_kwargs(
                session=session,
                output_dir=run_dir,
                git_root=tmp_path,
                presentation=PresentationPolicy.TERMINAL,
            ),
        )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Dirty working tree" in captured.err
    assert "non-interactive policy selected halt" in captured.err
    assert "src/app.py" in captured.err
    assert "notes.txt" in captured.err
    assert "Commit or stash" in captured.err
    assert "--no-worktree-isolation" in captured.err
    assert result.halted is True
    assert session["status"] == "halted"
    assert session["halt_reason"] == "pre_run_dirty_halt"
    assert "phase_handoff" not in session


def test_pre_run_dirty_seed_failed_clears_stale_phase_handoff(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = {"phase_handoff": {"pending": "decision"}, "status": "running"}
    include_intake = PreRunDirtyIntake(
        action="include", status="seed_pending", dirty=True,
    )
    seed_failed_intake = include_intake.with_status("seed_failed", error="boom")
    worktree_ctx = SimpleNamespace(
        is_isolated=True, degraded_reason=None, path=tmp_path,
    )

    with patch(
        "pipeline.engine.pre_run_dirty.resolve_pre_run_dirty_intake",
        return_value=include_intake,
    ), patch(
        "pipeline.engine.worktree.resolve_worktree_for_run",
        return_value=worktree_ctx,
    ), patch(
        "pipeline.engine.pre_run_dirty.apply_pre_run_dirty_seed",
        return_value=seed_failed_intake,
    ):
        result = setup_isolation(
            **_setup_isolation_kwargs(
                session=session,
                output_dir=run_dir,
                git_root=tmp_path,
                presentation=PresentationPolicy.SILENT,
            ),
        )

    assert result.halted is True
    assert session["status"] == "halted"
    assert session["halt_reason"] == "pre_run_dirty_seed_failed"
    assert session["pre_run_dirty"]["status"] == "seed_failed"
    assert "phase_handoff" not in session
