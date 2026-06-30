"""Worktree primitives in :mod:`core.io.git_helpers` (GWT-1 / ADR 0033).

Covers ``create_worktree``, ``remove_worktree``,
``worktree_diff_against_base``, and ``apply_patch_to_checkout``
against real on-disk git repos (tmp_path-scoped). Each test
exercises one of the three contract guarantees:

* never raises on expected git-side failure — surfaces via
  ``GitOpResult.ok=False`` instead;
* preserves user-owned dirt in the source repo (the load-bearing
  GWT-1 isolation guarantee);
* idempotency / no-op on safe re-runs.

These are filesystem tests (real ``git`` subprocess), not mocks —
the underlying contract is "we call git correctly", which only
real git can validate.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from core.io.git_helpers import (
    GitOpResult,
    apply_patch_to_checkout,
    create_worktree,
    git_head,
    remove_worktree,
    worktree_diff_against_base,
)


def _init_repo(repo: Path, *, with_commit: bool = True) -> None:
    """Initialise a fresh git repo at ``repo`` with a single
    commit (when ``with_commit=True``) and conservative
    user identity to keep CI environments happy."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@orcho.invalid"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Orcho Test"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=repo, check=True,
    )
    if with_commit:
        (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "init"], cwd=repo, check=True,
        )


def _head_sha(repo: Path) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


# ── create_worktree ─────────────────────────────────────────────────────────


class TestCreateWorktree:
    def test_creates_worktree_on_new_branch(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        target = tmp_path / "out" / "checkout"

        result = create_worktree(
            repo=repo,
            base_ref=_head_sha(repo),
            target_path=target,
            branch_name="orcho/run/test1",
        )

        assert result.ok, result.error
        assert result.path == target
        assert result.branch == "orcho/run/test1"
        # The worktree exists on disk and has the README from base.
        assert (target / "README.md").read_text(encoding="utf-8") == "# fixture\n"
        # ``.git`` is a file (worktree gitlink), not a dir.
        gitlink = target / ".git"
        assert gitlink.exists()
        assert gitlink.is_file(), "worktree .git must be a gitlink file"

    def test_detached_head_when_no_branch_name(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        target = tmp_path / "out" / "detached"

        result = create_worktree(
            repo=repo,
            base_ref=_head_sha(repo),
            target_path=target,
        )

        assert result.ok, result.error
        assert result.branch is None
        # HEAD is detached at the base ref.
        head = subprocess.run(
            ["git", "rev-parse", "--symbolic-full-name", "HEAD"],
            cwd=target, capture_output=True, text=True, check=True,
        )
        assert head.stdout.strip() == "HEAD", (
            f"expected detached HEAD, got {head.stdout.strip()!r}"
        )

    def test_rejects_existing_target(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        target = tmp_path / "occupied"
        target.mkdir()

        result = create_worktree(
            repo=repo,
            base_ref="HEAD",
            target_path=target,
            branch_name="orcho/run/test2",
        )

        assert result.ok is False
        assert "already exists" in (result.error or "")

    def test_unknown_base_ref_returns_error(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        target = tmp_path / "wt"

        result = create_worktree(
            repo=repo,
            base_ref="deadbeef" * 5,  # nonexistent sha
            target_path=target,
            branch_name="orcho/run/bogus",
        )

        assert result.ok is False
        assert result.error  # git's stderr propagated, content varies by version

    def test_does_not_mutate_source_repo(self, tmp_path: Path) -> None:
        """Load-bearing isolation guarantee: creating a worktree must
        not touch the source checkout (no new files, no commits, no
        index changes). Pin this explicitly — the whole GWT-1 thesis
        rests on it."""
        repo = tmp_path / "src"
        _init_repo(repo)
        # Pre-existing user-owned dirt in source — must survive.
        (repo / "user_dirty.txt").write_text("user work\n", encoding="utf-8")
        files_before = sorted(p.name for p in repo.iterdir())
        head_before = _head_sha(repo)

        target = tmp_path / "iso"
        result = create_worktree(
            repo=repo,
            base_ref="HEAD",
            target_path=target,
            branch_name="orcho/run/iso",
        )
        assert result.ok, result.error

        files_after = sorted(p.name for p in repo.iterdir())
        head_after = _head_sha(repo)
        assert files_after == files_before, (
            f"worktree creation mutated source repo files: "
            f"{files_before} → {files_after}"
        )
        assert head_after == head_before
        # The dirty file is still untracked in the source repo.
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        assert "user_dirty.txt" in status.stdout


# ── remove_worktree ─────────────────────────────────────────────────────────


class TestRemoveWorktree:
    def test_removes_clean_worktree(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        target = tmp_path / "to_remove"
        create_worktree(
            repo=repo,
            base_ref="HEAD",
            target_path=target,
            branch_name="orcho/run/rm1",
        )

        result = remove_worktree(target, repo=repo)
        assert result.ok, result.error
        assert not target.exists()

    def test_force_removes_dirty_worktree(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        target = tmp_path / "dirty_wt"
        create_worktree(
            repo=repo,
            base_ref="HEAD",
            target_path=target,
            branch_name="orcho/run/dirty",
        )
        (target / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

        result = remove_worktree(target, repo=repo, force=True)
        assert result.ok, result.error

    def test_absent_target_is_idempotent_success(self, tmp_path: Path) -> None:
        """Teardown after the worktree directory was already wiped
        (manual rmtree, crash, double-teardown) must not raise — the
        bookkeeping inside the source repo gets cleaned up by the
        next ``git worktree prune`` regardless."""
        result = remove_worktree(tmp_path / "never_existed")
        assert result.ok is True


# ── worktree_diff_against_base ─────────────────────────────────────────────


class TestWorktreeDiffAgainstBase:
    def test_returns_no_diff_marker_when_clean(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        target = tmp_path / "wt"
        create_worktree(
            repo=repo,
            base_ref="HEAD",
            target_path=target,
            branch_name="orcho/run/clean",
        )
        assert worktree_diff_against_base(target) == "(no diff)"

    def test_surfaces_uncommitted_changes(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        target = tmp_path / "wt"
        create_worktree(
            repo=repo,
            base_ref="HEAD",
            target_path=target,
            branch_name="orcho/run/diff",
        )
        (target / "README.md").write_text(
            "# fixture\n\nadded line\n", encoding="utf-8",
        )

        diff = worktree_diff_against_base(target)
        assert "(no diff)" not in diff
        assert "+added line" in diff or "added line" in diff

    def test_unavailable_marker_on_non_git_path(self, tmp_path: Path) -> None:
        """Non-git path must collapse to ``(diff unavailable)`` rather
        than raising — the consumer (evidence collector, sync-back)
        treats this as "no diff to apply" and moves on."""
        non_git = tmp_path / "not_a_repo"
        non_git.mkdir()
        out = worktree_diff_against_base(non_git)
        assert out == "(diff unavailable)"


# ── apply_patch_to_checkout ────────────────────────────────────────────────


class TestApplyPatchToCheckout:
    def _make_patch_pair(
        self, tmp_path: Path,
    ) -> tuple[Path, Path, str]:
        """Build a (source-with-edit, target-clean, patch_text) trio.

        Source has README.md modified; target is a fresh worktree
        from the same base. The diff between source and base is the
        patch we'll apply to target — should land cleanly."""
        repo = tmp_path / "src"
        _init_repo(repo)

        # Make a change in source and capture diff vs HEAD.
        (repo / "README.md").write_text(
            "# fixture\n\nadded line\n", encoding="utf-8",
        )
        diff = worktree_diff_against_base(repo)

        # Fresh worktree from same base to apply onto.
        target = tmp_path / "apply_target"
        create_worktree(
            repo=repo,
            base_ref="HEAD",
            target_path=target,
            branch_name="orcho/run/apply",
        )
        return repo, target, diff

    def test_apply_clean_patch_succeeds(self, tmp_path: Path) -> None:
        _repo, target, patch = self._make_patch_pair(tmp_path)
        result = apply_patch_to_checkout(target, patch)
        assert result.ok, result.error
        applied = (target / "README.md").read_text(encoding="utf-8")
        assert "added line" in applied

    def test_check_only_does_not_mutate(self, tmp_path: Path) -> None:
        _repo, target, patch = self._make_patch_pair(tmp_path)
        original = (target / "README.md").read_text(encoding="utf-8")
        result = apply_patch_to_checkout(target, patch, check_only=True)
        assert result.ok, result.error
        # File content unchanged after --check.
        assert (target / "README.md").read_text(encoding="utf-8") == original

    def test_empty_patch_is_noop_success(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        result = apply_patch_to_checkout(repo, "")
        assert result.ok is True

    def test_malformed_patch_returns_error(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        result = apply_patch_to_checkout(repo, "this is not a unified diff\n")
        assert result.ok is False
        assert result.error  # git stderr, content varies

    def test_conflicting_patch_returns_error_without_partial_apply(
        self, tmp_path: Path,
    ) -> None:
        """A patch that doesn't apply cleanly (base file changed since
        diff was taken) must fail atomically — no partial application.
        ``git apply`` is all-or-nothing by default; pin that
        behaviour."""
        repo = tmp_path / "src"
        _init_repo(repo)

        # Capture a patch based on README.md == "# fixture\n".
        (repo / "README.md").write_text(
            "# fixture\n\nadded line\n", encoding="utf-8",
        )
        patch = worktree_diff_against_base(repo)

        # Now divergently edit base so the patch can no longer apply.
        target = tmp_path / "diverged"
        create_worktree(
            repo=repo,
            base_ref="HEAD",
            target_path=target,
            branch_name="orcho/run/div",
        )
        (target / "README.md").write_text(
            "# different content entirely\n", encoding="utf-8",
        )
        snapshot_before = (target / "README.md").read_text(encoding="utf-8")

        result = apply_patch_to_checkout(target, patch)
        assert result.ok is False
        # Atomic failure: file content unchanged.
        assert (target / "README.md").read_text(encoding="utf-8") == snapshot_before


# ── git_head ───────────────────────────────────────────────────────────────


class TestGitHead:
    def test_returns_head_sha_in_real_repo(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        assert git_head(repo) == _head_sha(repo)

    def test_returns_none_outside_git_repo(self, tmp_path: Path) -> None:
        non_git = tmp_path / "not_a_repo"
        non_git.mkdir()
        assert git_head(non_git) is None


# ── never-raises discipline ────────────────────────────────────────────────


class TestNeverRaisesOnExpectedFailure:
    """All four primitives MUST return ``GitOpResult(ok=False, error=...)``
    on expected git failure rather than raising. Pinned because the
    pipeline engine branches on ``.ok`` and does not wrap calls in
    try/except — a regression to raising would break the lifecycle."""

    def test_create_worktree_does_not_raise_on_bogus_repo(
        self, tmp_path: Path,
    ) -> None:
        result = create_worktree(
            repo=tmp_path / "not_a_repo",
            base_ref="HEAD",
            target_path=tmp_path / "out",
            branch_name="orcho/run/x",
        )
        assert isinstance(result, GitOpResult)
        assert result.ok is False

    def test_remove_worktree_does_not_raise_on_bogus_target(
        self, tmp_path: Path,
    ) -> None:
        result = remove_worktree(
            tmp_path / "absent", repo=tmp_path / "also_absent",
        )
        # Absent target with absent source repo → idempotent ok=True.
        assert isinstance(result, GitOpResult)

    def test_apply_patch_does_not_raise_on_bogus_path(
        self, tmp_path: Path,
    ) -> None:
        result = apply_patch_to_checkout(
            tmp_path / "missing", "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n",
        )
        assert isinstance(result, GitOpResult)
