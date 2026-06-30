"""Pre-run dirty intake engine tests (ADR 0044)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from core.io.git_helpers import create_worktree
from pipeline.engine import pre_run_dirty
from pipeline.engine.pre_run_dirty import (
    PreRunDirtyIntake,
    apply_pre_run_dirty_seed,
    resolve_pre_run_dirty_intake,
)


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@orcho.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Orcho Test"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=repo,
        check=True,
    )
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return _head(repo)


def _head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _new_worktree(repo: Path, run_dir: Path, *, run_id: str = "r1") -> Path:
    result = create_worktree(
        repo=repo,
        base_ref=_head(repo),
        target_path=run_dir / "checkout",
        branch_name=f"orcho/run/{run_id}",
    )
    assert result.ok, result.error
    return run_dir / "checkout"


def test_clean_checkout_is_noop(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    intake = resolve_pre_run_dirty_intake(
        project_dir=repo,
        run_dir=tmp_path / "run",
        run_id="r1",
        pre_run_config={"enabled": True},
        worktree_config={"enabled": True, "isolation": "per_run"},
        profile_isolation=None,
        resume_from=None,
        no_interactive=True,
    )

    assert intake.action == "none"
    assert intake.status == "clean"


def test_noninteractive_default_halts_on_dirty_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "app.txt").write_text("dirty\n", encoding="utf-8")

    intake = resolve_pre_run_dirty_intake(
        project_dir=repo,
        run_dir=tmp_path / "run",
        run_id="r1",
        pre_run_config={
            "enabled": True,
            "non_interactive_default": "halt",
        },
        worktree_config={"enabled": True, "isolation": "per_run"},
        profile_isolation=None,
        resume_from=None,
        no_interactive=True,
    )

    assert intake.action == "halt"
    assert intake.status == "halted"
    assert intake.changed_paths == ("app.txt",)


def test_include_seeds_tracked_diff_without_touching_source(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "app.txt").write_text("dirty\n", encoding="utf-8")
    run_dir = tmp_path / "run"

    intake = resolve_pre_run_dirty_intake(
        project_dir=repo,
        run_dir=run_dir,
        run_id="r1",
        pre_run_config={
            "enabled": True,
            "non_interactive_default": "include",
            "include_untracked": "none",
        },
        worktree_config={"enabled": True, "isolation": "per_run"},
        profile_isolation=None,
        resume_from=None,
        no_interactive=True,
    )
    worktree = _new_worktree(repo, run_dir)

    seeded = apply_pre_run_dirty_seed(
        intake,
        project_dir=repo,
        worktree_path=worktree,
    )

    assert seeded.status == "seeded"
    assert seeded.seed_tree_sha
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "dirty\n"
    assert (repo / "app.txt").read_text(encoding="utf-8") == "dirty\n"
    assert (run_dir / "pre_run_dirty" / "seed.patch").exists()


def test_include_can_seed_untracked_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "notes.txt").write_text("new\n", encoding="utf-8")
    run_dir = tmp_path / "run"

    intake = resolve_pre_run_dirty_intake(
        project_dir=repo,
        run_dir=run_dir,
        run_id="r1",
        pre_run_config={
            "enabled": True,
            "non_interactive_default": "include",
            "include_untracked": "all",
        },
        worktree_config={"enabled": True, "isolation": "per_run"},
        profile_isolation=None,
        resume_from=None,
        no_interactive=True,
    )
    worktree = _new_worktree(repo, run_dir)

    seeded = apply_pre_run_dirty_seed(
        intake,
        project_dir=repo,
        worktree_path=worktree,
    )

    assert seeded.status == "seeded"
    assert seeded.selected_untracked_paths == ("notes.txt",)
    assert (worktree / "notes.txt").read_text(encoding="utf-8") == "new\n"


def test_interactive_prompt_can_include_untracked_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "app.txt").write_text("dirty\n", encoding="utf-8")
    (repo / "notes.txt").write_text("new\n", encoding="utf-8")
    answers = iter(["", "y"])

    monkeypatch.setattr(pre_run_dirty, "stdio_interactive", lambda: True)
    intake = resolve_pre_run_dirty_intake(
        project_dir=repo,
        run_dir=tmp_path / "run",
        run_id="r1",
        pre_run_config={
            "enabled": True,
            "interactive_default": "include",
            "include_untracked": "prompt",
        },
        worktree_config={"enabled": True, "isolation": "per_run"},
        profile_isolation=None,
        resume_from=None,
        no_interactive=False,
        input_fn=lambda _prompt: next(answers),
        output_fn=lambda _line: None,
    )

    assert intake.status == "seed_pending"
    assert intake.selected_untracked_paths == ("notes.txt",)


def test_seed_rejects_unsafe_untracked_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    intake = PreRunDirtyIntake(
        action="include",
        status="seed_pending",
        dirty=True,
        selected_untracked_paths=("../escape.txt",),
    )

    seeded = apply_pre_run_dirty_seed(
        intake,
        project_dir=repo,
        worktree_path=worktree,
    )

    assert seeded.status == "seed_failed"
    assert seeded.error == "unsafe untracked path '../escape.txt'"


def test_seed_rejects_untracked_destination_collision(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    (repo / "notes.txt").write_text("source\n", encoding="utf-8")
    (worktree / "notes.txt").write_text("existing\n", encoding="utf-8")
    intake = PreRunDirtyIntake(
        action="include",
        status="seed_pending",
        dirty=True,
        selected_untracked_paths=("notes.txt",),
    )

    seeded = apply_pre_run_dirty_seed(
        intake,
        project_dir=repo,
        worktree_path=worktree,
    )

    assert seeded.status == "seed_failed"
    assert seeded.error == "seed destination already exists: notes.txt"
    assert (worktree / "notes.txt").read_text(encoding="utf-8") == "existing\n"


def test_exclude_leaves_new_worktree_at_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "app.txt").write_text("dirty\n", encoding="utf-8")
    run_dir = tmp_path / "run"

    intake = resolve_pre_run_dirty_intake(
        project_dir=repo,
        run_dir=run_dir,
        run_id="r1",
        pre_run_config={
            "enabled": True,
            "non_interactive_default": "exclude",
        },
        worktree_config={"enabled": True, "isolation": "per_run"},
        profile_isolation=None,
        resume_from=None,
        no_interactive=True,
    )
    worktree = _new_worktree(repo, run_dir)

    assert intake.status == "excluded"
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "base\n"


def test_commit_action_advances_head_and_cleans_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    old_head = _init_repo(repo)
    (repo / "app.txt").write_text("dirty\n", encoding="utf-8")

    intake = resolve_pre_run_dirty_intake(
        project_dir=repo,
        run_dir=tmp_path / "run",
        run_id="r1",
        pre_run_config={
            "enabled": True,
            "non_interactive_default": "commit",
        },
        worktree_config={"enabled": True, "isolation": "per_run"},
        profile_isolation=None,
        resume_from=None,
        no_interactive=True,
    )

    assert intake.status == "committed"
    assert intake.commit_sha
    assert intake.commit_sha != old_head
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout.strip() == ""


def test_commit_failure_unstages_checkout_without_discarding_work(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "app.txt").write_text("dirty\n", encoding="utf-8")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    intake = resolve_pre_run_dirty_intake(
        project_dir=repo,
        run_dir=tmp_path / "run",
        run_id="r1",
        pre_run_config={
            "enabled": True,
            "non_interactive_default": "commit",
        },
        worktree_config={"enabled": True, "isolation": "per_run"},
        profile_isolation=None,
        resume_from=None,
        no_interactive=True,
    )

    cached = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )

    assert intake.status == "commit_failed"
    assert cached.stdout.strip() == ""
    assert status.stdout.splitlines() == [" M app.txt"]
    assert (repo / "app.txt").read_text(encoding="utf-8") == "dirty\n"
