"""Injected operator action + defer-parking for resolve_commit_delivery (ADR 0100).

These exercise the two non-interactive hooks added for the out-of-band delivery
decision surface: ``decision_action`` (replace the config default with an
operator choice while keeping the hard guards) and ``decision_mode='defer'``
(park the decision as a ``pending`` gate instead of auto-resolving it).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from core.io.git_helpers import create_worktree
from pipeline.engine.commit_delivery import resolve_commit_delivery


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@orcho.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Orcho Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _worktree(repo: Path, run_dir: Path) -> Path:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    result = create_worktree(
        repo=repo,
        base_ref=head,
        target_path=run_dir / "checkout",
        branch_name="orcho/run/r1",
    )
    assert result.ok, result.error
    return run_dir / "checkout"


def _session(verdict: str = "APPROVED") -> dict:
    return {
        "status": "done",
        "phases": {
            "final_acceptance": {"verdict": verdict, "short_summary": "feat: x"},
        },
    }


_CFG = {"enabled": True, "auto_in_ci": "approve", "add_untracked": True}


def _dirty_worktree(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    wt = _worktree(repo, run_dir)
    (wt / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    return repo, run_dir, wt


def test_decision_action_overrides_config_default(tmp_path: Path) -> None:
    repo, run_dir, wt = _dirty_worktree(tmp_path)
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=wt,
        run_dir=run_dir,
        run_id="r1",
        session=_session(),
        commit_config=_CFG,  # auto default is approve
        no_interactive=True,
        decision_action="skip",  # operator overrides
    )
    assert decision.status == "pending"
    assert decision.action == "skip"


def test_decision_action_fix_is_honored(tmp_path: Path) -> None:
    repo, run_dir, wt = _dirty_worktree(tmp_path)
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=wt,
        run_dir=run_dir,
        run_id="r1",
        session=_session(),
        commit_config=_CFG,
        no_interactive=True,
        decision_action="fix",
    )
    assert decision.status == "pending"
    assert decision.action == "fix"


def test_rejected_release_still_blocks_injected_approve(tmp_path: Path) -> None:
    repo, run_dir, wt = _dirty_worktree(tmp_path)
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=wt,
        run_dir=run_dir,
        run_id="r1",
        session=_session(verdict="REJECTED"),
        commit_config=_CFG,
        no_interactive=True,
        decision_action="approve",
    )
    # Hard guard preserved: a rejected release cannot be shipped non-interactively.
    assert decision.status == "not_applicable"
    assert decision.error == "release verdict is not APPROVED"


def test_rejected_release_allows_injected_skip(tmp_path: Path) -> None:
    repo, run_dir, wt = _dirty_worktree(tmp_path)
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=wt,
        run_dir=run_dir,
        run_id="r1",
        session=_session(verdict="REJECTED"),
        commit_config=_CFG,
        no_interactive=True,
        decision_action="skip",
    )
    # fix / skip / halt do not ship the diff, so they remain expressible.
    assert decision.status == "pending"
    assert decision.action == "skip"


def test_defer_parks_decision_with_context(tmp_path: Path) -> None:
    repo, run_dir, wt = _dirty_worktree(tmp_path)
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=wt,
        run_dir=run_dir,
        run_id="r1",
        session=_session(),
        commit_config=_CFG,
        no_interactive=True,
        decision_mode="defer",
    )
    assert decision.status == "pending"
    assert decision.action == "none"  # undecided gate
    assert decision.changed_paths == ("app.txt",)
    assert decision.baseline_ref
    persisted = decision.to_dict()
    assert persisted["status"] == "pending"
    assert persisted["source_path"] == str(wt)


def test_defer_parks_rejected_release_as_correction(tmp_path: Path) -> None:
    repo, run_dir, wt = _dirty_worktree(tmp_path)
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=wt,
        run_dir=run_dir,
        run_id="r1",
        session=_session(verdict="REJECTED"),
        commit_config=_CFG,
        no_interactive=True,
        decision_mode="defer",
    )
    assert decision.status == "pending"
    assert decision.release_verdict == "REJECTED"


def test_auto_default_unchanged_without_injection(tmp_path: Path) -> None:
    repo, run_dir, wt = _dirty_worktree(tmp_path)
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=wt,
        run_dir=run_dir,
        run_id="r1",
        session=_session(),
        commit_config=_CFG,
        no_interactive=True,
    )
    # Byte-identical legacy behavior: action resolves to the auto default.
    assert decision.status == "pending"
    assert decision.action == "approve"
