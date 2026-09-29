"""A path the run deleted is delivered as a deletion, however it was removed.

The run owns its checkout, so it may delete a tracked file with ``git rm``
(deletion already staged) as readily as with a plain ``rm`` (deletion only in
the working tree). Delivery stages run-owned paths by name; both removal modes
must reach the delivery commit on every commit site: the run branch in a
per-run worktree, the canonical checkout after patch transport, and an
in-place run committing its own checkout.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.io.git_helpers import create_worktree
from pipeline.engine.commit_delivery import (
    apply_commit_delivery,
    resolve_commit_delivery,
)

_GONE = "gone.txt"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@orcho.invalid")
    _git(repo, "config", "user.name", "Orcho Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    (repo / _GONE).write_text("obsolete\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")


def _run_checkout(repo: Path, run_dir: Path, *, isolated: bool) -> Path:
    if not isolated:
        return repo
    result = create_worktree(
        repo=repo,
        base_ref=_git(repo, "rev-parse", "HEAD"),
        target_path=run_dir / "checkout",
        branch_name="orcho/run/r1",
    )
    assert result.ok, result.error
    return run_dir / "checkout"


def _remove(checkout: Path, removal: str) -> None:
    if removal == "git_rm":
        _git(checkout, "rm", "-q", _GONE)
    else:
        (checkout / _GONE).unlink()


# (label, isolated per-run worktree, branch_policy) — one row per commit site.
_COMMIT_SITES = [
    ("run_branch", True, "worktree_branch"),
    ("transported_checkout", True, "bypass"),
    ("in_place_checkout", False, "bypass"),
]


@pytest.mark.parametrize("removal", ["git_rm", "plain_rm"])
@pytest.mark.parametrize(
    ("isolated", "branch_policy"),
    [row[1:] for row in _COMMIT_SITES],
    ids=[row[0] for row in _COMMIT_SITES],
)
def test_run_deleted_path_reaches_the_delivery_commit(
    tmp_path: Path, isolated: bool, branch_policy: str, removal: str,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    checkout = _run_checkout(repo, run_dir, isolated=isolated)
    (checkout / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    _remove(checkout, removal)

    commit_config: dict = {
        "enabled": True,
        "auto_in_ci": "approve",
        "add_untracked": True,
        "branch_policy": branch_policy,
    }
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=checkout,
        run_dir=run_dir,
        run_id="r1",
        session={
            "phases": {
                "final_acceptance": {
                    "verdict": "APPROVED",
                    "short_summary": "feat: drop obsolete file",
                },
            },
        },
        commit_config=commit_config,
        no_interactive=True,
        baseline_ref="HEAD",
    )
    delivered = apply_commit_delivery(
        decision, run_dir=run_dir, commit_config=commit_config,
    )

    assert delivered.status == "committed", delivered.error
    delivered_ref = delivered.delivery_branch or "HEAD"
    delivered_files = _git(repo, "ls-tree", "--name-only", delivered_ref)
    assert delivered_files.splitlines() == ["app.txt"]
    assert _git(repo, "show", f"{delivered_ref}:app.txt") == "base\nrun"
