# SPDX-License-Identifier: Apache-2.0
"""Repair-subject proof guard for the review-retry resume path (T2).

Before ``repair_changes`` re-runs after a ``review_changes`` rejection, the
rejected diff subject must be present in the repair cwd. These tests pin
every branch of :func:`pipeline.project.retry_subject.ensure_repair_subject_proven`:

* a dirty retained worktree proves the subject (uncommitted changes);
* a committed diff (HEAD moved off the recorded base) proves it too;
* a clean HEAD sitting on the recorded base aborts with the required text;
* a repair cwd that does not match the recorded retained path aborts;
* isolation-off runs are judged purely on dirty/HEAD-shift in the cwd.

Plus the read-only / re-resumability invariant: the run-level guard raises
without mutating the session, so the active handoff + decision survive for a
later resume.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.project.retry_subject import (
    CLEAN_HEAD_MESSAGE,
    RepairSubjectUnproven,
    ensure_repair_subject_proven,
    guard_review_retry_subject,
)

pytestmark = pytest.mark.git_worktree


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@orcho.invalid"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Orcho Test"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True,
    )
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return _head(repo)


def _head(repo: Path) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
        check=True,
    )
    return r.stdout.strip()


def _commit(repo: Path, text: str) -> str:
    (repo / "f.txt").write_text(text, encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "change"], cwd=repo, check=True)
    return _head(repo)


def _block(path: Path, base: str, *, isolation: str = "per_run") -> dict:
    return {
        "isolation": isolation,
        "path": str(path),
        "base_ref": base,
        "source_start_head": base,
    }


# ── proven subjects ─────────────────────────────────────────────────────────


def test_dirty_worktree_proves_subject(tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    base = _init_repo(repo)
    # Uncommitted edit -> porcelain non-empty.
    (repo / "f.txt").write_text("base\nrejected change\n", encoding="utf-8")

    # Does not raise.
    ensure_repair_subject_proven(cwd=str(repo), worktree_block=_block(repo, base))


def test_committed_diff_proves_subject(tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    base = _init_repo(repo)
    # Commit the rejected change: clean tree, but HEAD has moved off base.
    new_head = _commit(repo, "base\ncommitted rejected change\n")
    assert new_head != base

    # Recorded base is the original start head; HEAD != base -> proven.
    ensure_repair_subject_proven(cwd=str(repo), worktree_block=_block(repo, base))


# ── unproven subjects ───────────────────────────────────────────────────────


def test_clean_head_on_base_aborts_with_required_message(tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    base = _init_repo(repo)
    # Clean tree, HEAD == recorded base -> the rejected diff is gone.
    with pytest.raises(RepairSubjectUnproven) as exc:
        ensure_repair_subject_proven(
            cwd=str(repo), worktree_block=_block(repo, base),
        )
    assert str(exc.value) == CLEAN_HEAD_MESSAGE


def test_cwd_not_matching_recorded_path_aborts(tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    base = _init_repo(repo)
    # Even with a dirty tree, a cwd that is not the recorded retained path is
    # the wrong subject and must abort before the dirty check.
    (repo / "f.txt").write_text("base\ndirty\n", encoding="utf-8")
    other = tmp_path / "elsewhere" / "checkout"
    with pytest.raises(RepairSubjectUnproven) as exc:
        ensure_repair_subject_proven(
            cwd=str(repo), worktree_block=_block(other, base),
        )
    assert "does not match the retained" in str(exc.value)
    assert str(other) in str(exc.value)


# ── isolation off ───────────────────────────────────────────────────────────


def test_isolation_off_dirty_proves_subject(tmp_path: Path) -> None:
    repo = tmp_path / "src"
    base = _init_repo(repo)
    (repo / "f.txt").write_text("base\ndirty in place\n", encoding="utf-8")
    # isolation off: no retained-path match, only dirty/HEAD-shift in cwd.
    ensure_repair_subject_proven(
        cwd=str(repo), worktree_block=_block(repo, base, isolation="off"),
    )


def test_isolation_off_clean_head_aborts(tmp_path: Path) -> None:
    repo = tmp_path / "src"
    base = _init_repo(repo)
    with pytest.raises(RepairSubjectUnproven) as exc:
        ensure_repair_subject_proven(
            cwd=str(repo), worktree_block=_block(repo, base, isolation="off"),
        )
    assert str(exc.value) == CLEAN_HEAD_MESSAGE


def test_no_worktree_block_uses_cwd_dirty_check(tmp_path: Path) -> None:
    repo = tmp_path / "src"
    _init_repo(repo)
    (repo / "f.txt").write_text("base\nuntracked dirty\n", encoding="utf-8")
    # No recorded block at all -> off-equivalent dirty check on cwd.
    ensure_repair_subject_proven(cwd=str(repo), worktree_block=None)


# ── read-only / re-resumability ─────────────────────────────────────────────


def test_guard_is_read_only_so_run_stays_resumable(tmp_path: Path) -> None:
    """A clean-HEAD abort leaves the active handoff + decision intact.

    The run-level adapter resolves cwd/worktree from the run and raises
    without touching the session, so meta.phase_handoff (and the recorded
    decision) survive and the run can be decided/resumed again after the
    retained worktree diff is restored.
    """
    repo = tmp_path / "wt"
    base = _init_repo(repo)
    active_handoff = {
        "id": "review_changes:repair_round:2",
        "phase": "review_changes",
        "available_actions": ["continue", "retry_feedback", "halt"],
    }
    run = SimpleNamespace(
        state=SimpleNamespace(
            extras={"git_cwd": str(repo)},
            project_dir=str(repo),
        ),
        session={
            "status": "awaiting_phase_handoff",
            "phase_handoff": dict(active_handoff),
            "worktree": _block(repo, base),
        },
    )

    with pytest.raises(RepairSubjectUnproven) as exc:
        guard_review_retry_subject(run)
    assert str(exc.value) == CLEAN_HEAD_MESSAGE

    # Read-only: the active handoff payload and status are untouched, so the
    # run is still decidable / resumable.
    assert run.session["phase_handoff"] == active_handoff
    assert run.session["status"] == "awaiting_phase_handoff"


def test_guard_passes_for_dirty_run_via_adapter(tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    base = _init_repo(repo)
    (repo / "f.txt").write_text("base\nrejected\n", encoding="utf-8")
    run = SimpleNamespace(
        state=SimpleNamespace(extras={"git_cwd": str(repo)}, project_dir=str(repo)),
        session={"worktree": _block(repo, base)},
    )
    # Does not raise.
    guard_review_retry_subject(run)
