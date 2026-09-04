"""Project isolation setup wires plugin-declared worktree bootstrap."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agents.runtimes import MockAgentProvider
from pipeline.engine.pre_run_dirty import PreRunDirtyIntake
from pipeline.engine.worktree import WorktreeConfigError
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


def _iso(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


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


def test_bootstrap_terminal_renders_step_and_total_elapsed(
    tmp_path: Path, capsys,
) -> None:
    session = {}
    worktree_ctx = SimpleNamespace(is_isolated=True, path=tmp_path)
    clock = Mock(side_effect=[10.0, 11.25, 12.5])
    record = {"index": 1, "action": "run", "status": "ok"}

    def bootstrap(*args, on_step, **kwargs):
        on_step("start", 1, "run", {"run": ["composer", "install"]})
        on_step("complete", 1, "run", record)
        return {"status": "ok", "steps": [record]}

    with patch("pipeline.project.isolation_setup.time.monotonic", clock), patch(
        "pipeline.engine.worktree_bootstrap.run_worktree_bootstrap", bootstrap,
    ):
        _apply_worktree_bootstrap(
            config=[{"run": ["composer", "install"]}],
            session=session,
            output_dir=None,
            git_root=tmp_path,
            worktree_ctx=worktree_ctx,
            presentation=PresentationPolicy.TERMINAL,
    )

    output = capsys.readouterr().out
    assert "[SETUP] Worktree bootstrap" in output or "▶ setup" in output
    assert "composer install" in output
    assert "done (1.25s)" in output
    assert "Worktree bootstrap complete (2.50s)" in output


def test_bootstrap_silent_is_quiet_even_when_steps_report(
    tmp_path: Path, capsys,
) -> None:
    """SILENT still receives the step callback (it feeds the startup watchdog)
    but must not render anything for it."""
    session = {}
    worktree_ctx = SimpleNamespace(is_isolated=True, path=tmp_path)
    record = {"index": 1, "action": "copy", "status": "ok"}
    seen: dict[str, object] = {}

    def bootstrap(*args, on_step, **kwargs):
        seen.update(kwargs)
        on_step("start", 1, "copy", {"copy": "libs"})
        on_step("complete", 1, "copy", record)
        return {"status": "ok", "steps": [record]}

    with patch(
        "pipeline.engine.worktree_bootstrap.run_worktree_bootstrap", bootstrap,
    ):
        _apply_worktree_bootstrap(
            config=[{"copy": "libs"}],
            session=session,
            output_dir=None,
            git_root=tmp_path,
            worktree_ctx=worktree_ctx,
            presentation=PresentationPolicy.SILENT,
        )

    assert seen == {"source_root": tmp_path, "worktree_path": tmp_path}
    assert session["worktree_bootstrap"]["status"] == "ok"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_bootstrap_longer_than_the_startup_budget_is_not_retro_halted(
    tmp_path: Path,
) -> None:
    """A successful bootstrap emits no event and writes no output.log, so the
    startup watchdog's ambient progress check cannot see it. The bootstrap
    path must report its own progress: after a bootstrap that outlives the
    budget, the next checkpoint keeps the run alive and the durable window in
    ``startup_command.json`` is refreshed, while the watchdog stays armed for
    a later hang before the first phase."""
    from pipeline.project.startup_watchdog import startup_watchdog_scope

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = {"status": "running", "phases": {}}
    worktree_ctx = SimpleNamespace(is_isolated=True, path=tmp_path)
    step_beats: list[str] = []

    def bootstrap(*args, on_step, **kwargs):
        for index in (1, 2):
            on_step("start", index, "run", {"run": ["npm", "ci"]})
            time.sleep(0.02)
            on_step("complete", index, "run", {"index": index, "action": "run", "status": "ok"})
            step_beats.append(json.loads((run_dir / "startup_command.json").read_text())["armed_at"])
        return {"status": "ok", "steps": []}

    with startup_watchdog_scope(run_dir) as watchdog, patch(
        "pipeline.engine.worktree_bootstrap.run_worktree_bootstrap", bootstrap,
    ):
        watchdog.budget_s = 0.01
        watchdog.arm()
        armed_at = json.loads((run_dir / "startup_command.json").read_text())["armed_at"]
        _apply_worktree_bootstrap(
            config=[{"run": ["npm", "ci"]}],
            session=session,
            output_dir=run_dir,
            git_root=tmp_path,
            worktree_ctx=worktree_ctx,
            presentation=PresentationPolicy.SILENT,
        )
        assert watchdog.checkpoint(session) is False
        assert watchdog.armed is True and watchdog.disarmed is False
        refreshed = json.loads((run_dir / "startup_command.json").read_text())
        assert (
            _iso(refreshed["armed_at"]) > _iso(step_beats[-1]) > _iso(step_beats[0]) > _iso(armed_at)
        )
        assert refreshed["budget_s"] == 0.01

        time.sleep(0.02)
        assert watchdog.checkpoint(session) is True

    assert session["worktree_bootstrap"]["status"] == "ok"
    assert session["halt_reason"] == "startup_stalled"


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


def test_pre_run_dirty_seed_failed_terminal_prints_actionable_message(
    tmp_path: Path, capsys,
) -> None:
    """A seed failure must reach the terminal, not only ``meta.json``.

    Both pre-run halts land before the first phase starts, so nothing
    downstream renders them. Silence here is what made a failed seed look
    like Orcho exiting cleanly to a bare shell prompt.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = {"status": "running"}
    include_intake = PreRunDirtyIntake(
        action="include",
        status="seed_pending",
        dirty=True,
        changed_paths=("src/app.py",),
        selected_untracked_paths=("docs/research/",),
    )
    seed_failed_intake = include_intake.with_status(
        "seed_failed",
        error="untracked source no longer exists: docs/research/notes.md",
    )
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
                presentation=PresentationPolicy.TERMINAL,
            ),
        )

    captured = capsys.readouterr()
    assert captured.out == ""
    # Names the action, the failing path, and the next operator move.
    assert "'include'" in captured.err
    assert "docs/research/notes.md" in captured.err
    assert "docs/research/" in captured.err
    assert "src/app.py" in captured.err
    assert "'exclude'" in captured.err
    assert "checkout was not modified" in captured.err
    assert result.halted is True
    assert session["status"] == "halted"
    assert session["halt_reason"] == "pre_run_dirty_seed_failed"


def _retained_followup_decision(parent_worktree: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        blocked=False,
        effective_parent_worktree=parent_worktree,
        diff_source="worktree",
        mode_label="reuse retained parent worktree",
        to_dict=lambda: {
            "mode_label": "reuse retained parent worktree",
            "blocked": False,
            "reason": None,
            "diff_source": "worktree",
        },
    )


def _correction_followup_setup_kwargs(
    *, session: dict, output_dir: Path, git_root: Path,
    parent_worktree: dict[str, str],
) -> dict:
    kwargs = _setup_isolation_kwargs(
        session=session,
        output_dir=output_dir,
        git_root=git_root,
        presentation=PresentationPolicy.SILENT,
    )
    kwargs.update(
        followup_parent_worktree=parent_worktree,
        resume_mode="followup",
        followup_parent_run_id="parent-run",
        v2_profile=SimpleNamespace(
            name="correction", worktree_isolation=None, sandbox=None,
        ),
    )
    return kwargs


def test_correction_followup_publishes_exact_retained_worktree(
    tmp_path: Path,
) -> None:
    parent_path = tmp_path / "retained"
    parent_path.mkdir()
    output_dir = tmp_path / "child-run"
    output_dir.mkdir()
    parent_worktree = {"path": str(parent_path / ".")}
    worktree_ctx = SimpleNamespace(
        is_isolated=True,
        degraded_reason=None,
        path=parent_path,
        to_dict=lambda: {"path": str(parent_path), "isolation": "per_run"},
    )
    session: dict = {}

    with patch(
        "pipeline.project.followup_worktree.classify_followup_worktree",
        return_value=_retained_followup_decision(parent_worktree),
    ), patch(
        "pipeline.engine.worktree.resolve_worktree_for_run",
        return_value=worktree_ctx,
    ):
        result = setup_isolation(
            **_correction_followup_setup_kwargs(
                session=session,
                output_dir=output_dir,
                git_root=tmp_path,
                parent_worktree=parent_worktree,
            ),
        )

    assert result.worktree_ctx is worktree_ctx
    assert result.git_cwd == str(parent_path)
    assert result.halted is False
    assert result.worktree_ctx.path.resolve() == parent_path.resolve()
    assert session["worktree"]["path"] == str(parent_path)
    assert session["worktree"]["followup_continuity"]["diff_source"] == "worktree"


def test_correction_followup_rejects_substituted_retained_worktree(
    tmp_path: Path,
) -> None:
    parent_path = tmp_path / "retained"
    substituted_path = tmp_path / "substituted"
    parent_path.mkdir()
    substituted_path.mkdir()
    output_dir = tmp_path / "child-run"
    output_dir.mkdir()
    parent_worktree = {"path": str(parent_path)}
    worktree_ctx = SimpleNamespace(
        is_isolated=True,
        degraded_reason=None,
        path=substituted_path,
        to_dict=lambda: {"path": str(substituted_path), "isolation": "per_run"},
    )

    with patch(
        "pipeline.project.followup_worktree.classify_followup_worktree",
        return_value=_retained_followup_decision(parent_worktree),
    ), patch(
        "pipeline.engine.worktree.resolve_worktree_for_run",
        return_value=worktree_ctx,
    ), pytest.raises(
        WorktreeConfigError,
        match="must reuse the exact retained parent worktree",
    ):
        setup_isolation(
            **_correction_followup_setup_kwargs(
                session={},
                output_dir=output_dir,
                git_root=tmp_path,
                parent_worktree=parent_worktree,
            ),
        )


def test_correction_followup_rejects_unreadable_retained_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_path = tmp_path / "retained"
    parent_path.mkdir()
    output_dir = tmp_path / "child-run"
    output_dir.mkdir()
    parent_worktree = {"path": str(parent_path)}
    worktree_ctx = SimpleNamespace(
        is_isolated=True,
        degraded_reason=None,
        path=parent_path,
        to_dict=lambda: {"path": str(parent_path), "isolation": "per_run"},
    )

    def _unreadable_resolve(*_args, **_kwargs):
        raise OSError("unreadable worktree")

    monkeypatch.setattr(
        "pipeline.project.isolation_setup.Path.resolve", _unreadable_resolve,
    )
    with patch(
        "pipeline.project.followup_worktree.classify_followup_worktree",
        return_value=_retained_followup_decision(parent_worktree),
    ), patch(
        "pipeline.engine.worktree.resolve_worktree_for_run",
        return_value=worktree_ctx,
    ), pytest.raises(
        WorktreeConfigError,
        match="must reuse the exact retained parent worktree",
    ):
        setup_isolation(
            **_correction_followup_setup_kwargs(
                session={},
                output_dir=output_dir,
                git_root=tmp_path,
                parent_worktree=parent_worktree,
            ),
        )


# ── a bootstrap halt must leave the failing step's output in the run dir ────


def _bootstrap_failure(**overrides) -> WorktreeBootstrapError:
    failure = {
        "index": 2, "action": "run", "status": "failed", "reason": "exit_code",
        "cmd": ["npx", "nuxt", "prepare"], "cwd": "/wt", "exit_code": 1,
        "stdout_tail": "preparing app", "stderr_tail": "ENOENT: nuxt.config",
    }
    failure.update(overrides)
    return WorktreeBootstrapError(
        "worktree_bootstrap run step 2 failed with exit code 1", failure=failure,
    )


def test_bootstrap_failure_persists_step_output_into_the_run_dir(
    tmp_path: Path,
) -> None:
    """The reported gap: runner.log carried one exit-code line and output.log
    was empty, so the halt was undiagnosable once the process was gone."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session: dict = {"status": "running"}
    worktree_ctx = SimpleNamespace(is_isolated=True, path=tmp_path)

    with patch(
        "pipeline.engine.worktree_bootstrap.run_worktree_bootstrap",
        side_effect=_bootstrap_failure(),
    ), pytest.raises(WorktreeBootstrapError):
        _apply_worktree_bootstrap(
            config=[{"run": ["npx", "nuxt", "prepare"]}],
            session=session,
            output_dir=run_dir,
            git_root=tmp_path,
            worktree_ctx=worktree_ctx,
            presentation=PresentationPolicy.SILENT,
        )

    evidence = run_dir / "worktree_bootstrap" / "step-0002-run.json"
    assert evidence.is_file()
    record = json.loads(evidence.read_text(encoding="utf-8"))
    assert record["exit_code"] == 1
    assert record["cmd"] == ["npx", "nuxt", "prepare"]
    assert record["stdout_tail"] == "preparing app"
    assert record["stderr_tail"] == "ENOENT: nuxt.config"
    assert record["error"] == "worktree_bootstrap run step 2 failed with exit code 1"

    # The durable session/meta record carries it too, next to the halt reason.
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["halt_reason"] == "worktree_bootstrap_failed"
    assert meta["worktree_bootstrap"]["failed_step"]["stderr_tail"] == (
        "ENOENT: nuxt.config"
    )


def test_bootstrap_failure_terminal_quotes_the_step_output(
    tmp_path: Path, capsys,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session: dict = {"status": "running"}
    worktree_ctx = SimpleNamespace(is_isolated=True, path=tmp_path)

    with patch(
        "pipeline.engine.worktree_bootstrap.run_worktree_bootstrap",
        side_effect=_bootstrap_failure(),
    ), pytest.raises(SystemExit):
        _apply_worktree_bootstrap(
            config=[{"run": ["npx", "nuxt", "prepare"]}],
            session=session,
            output_dir=run_dir,
            git_root=tmp_path,
            worktree_ctx=worktree_ctx,
            presentation=PresentationPolicy.TERMINAL,
        )

    err = capsys.readouterr().err
    assert "step 2 (run)" in err
    assert "preparing app" in err
    assert "ENOENT: nuxt.config" in err


def test_bootstrap_failure_without_output_dir_still_records_the_step(
    tmp_path: Path, capsys,
) -> None:
    """No run dir is not a reason to lose the diagnosis."""
    session: dict = {"status": "running"}
    worktree_ctx = SimpleNamespace(is_isolated=True, path=tmp_path)

    with patch(
        "pipeline.engine.worktree_bootstrap.run_worktree_bootstrap",
        side_effect=_bootstrap_failure(),
    ), pytest.raises(SystemExit):
        _apply_worktree_bootstrap(
            config=[{"run": ["npx", "nuxt", "prepare"]}],
            session=session,
            output_dir=None,
            git_root=tmp_path,
            worktree_ctx=worktree_ctx,
            presentation=PresentationPolicy.TERMINAL,
        )

    assert session["worktree_bootstrap"]["failed_step"]["exit_code"] == 1
    assert "ENOENT: nuxt.config" in capsys.readouterr().err
