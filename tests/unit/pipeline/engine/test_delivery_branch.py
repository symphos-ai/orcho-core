"""ADR 0119 delivery-branch resolver tests on real git commits.

Exercises :mod:`pipeline.engine.delivery_branch` against real repositories and
worktrees: default-branch detection, the full ``branch_policy`` × isolation
table, the ``worktree_branch`` publish (rebase-onto-fresh-default, conflict and
offline degrade), and the PR-intent shape. Marked ``git_worktree`` (real
worktrees / subprocesses) and ``serial`` (shared git state).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pipeline.engine.delivery_branch import (
    DeliveryPrIntent,
    checkout_delivery_branch,
    detect_default_branch,
    normalize_branch_policy,
    publish_delivery_branch,
    resolve_delivery_branch,
)

pytestmark = [pytest.mark.git_worktree, pytest.mark.serial]


# --- git fixtures --------------------------------------------------------


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _git_out(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _identity(path: Path) -> None:
    _git(path, "config", "user.email", "orcho@example.test")
    _git(path, "config", "user.name", "Orcho Test")


def _commit_file(path: Path, name: str, content: str, message: str) -> None:
    (path / name).write_text(content, encoding="utf-8")
    _git(path, "add", name)
    _git(path, "commit", "-qm", message)


def _make_origin_clone(tmp_path: Path, *, seed_file: str = "seed.txt") -> tuple[Path, Path, Path]:
    """Return ``(origin, seed, canonical)`` — a bare origin on ``main``, its seed
    working repo, and a fresh clone whose ``origin/HEAD`` points at ``main``."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _identity(seed)
    _commit_file(seed, seed_file, "base\n", "initial")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")

    canonical = tmp_path / "canonical"
    _git(tmp_path, "clone", "-q", str(origin), str(canonical))
    _identity(canonical)
    return origin, seed, canonical


def _add_run_worktree(canonical: Path, tmp_path: Path, run_id: str, base: str) -> Path:
    run_wt = tmp_path / f"run_{run_id}"
    _git(canonical, "worktree", "add", "-q", "-b", f"orcho/run/{run_id}", str(run_wt), base)
    return run_wt


# --- default-branch detection --------------------------------------------


def test_detect_default_branch_from_origin_head(tmp_path: Path) -> None:
    _, _, canonical = _make_origin_clone(tmp_path)
    assert detect_default_branch(canonical) == "main"


def test_detect_default_branch_falls_back_to_master(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _identity(repo)
    _commit_file(repo, "a.txt", "x\n", "init")
    # No origin/HEAD: detection walks main -> master and finds master.
    assert detect_default_branch(repo) == "master"


# --- bypass / named ------------------------------------------------------


def test_bypass_commits_in_place_with_no_branch_or_pr_intent(tmp_path: Path) -> None:
    _, _, canonical = _make_origin_clone(tmp_path)
    outcome = resolve_delivery_branch(
        source_path=canonical,
        project_path=canonical,
        run_id="20260702_1",
        base_ref="HEAD",
        branch_policy="bypass",
    )
    assert outcome.policy == "bypass"
    assert outcome.plan == "commit_in_place"
    assert outcome.commits_into_checkout is True
    assert outcome.delivery_branch is None
    assert outcome.pr_intent is None


def test_named_commits_onto_named_branch(tmp_path: Path) -> None:
    _, _, canonical = _make_origin_clone(tmp_path)
    outcome = resolve_delivery_branch(
        source_path=canonical,
        project_path=canonical,
        run_id="20260702_2",
        base_ref="HEAD",
        branch_policy="named",
        named_branch="release/v9",
        release_summary="Ship the delivery branch policy",
    )
    assert outcome.policy == "named"
    assert outcome.plan == "commit_on_branch"
    assert outcome.commit_branch == "release/v9"
    assert outcome.delivery_branch == "release/v9"
    assert outcome.pr_intent is not None
    assert outcome.pr_intent.branch == "release/v9"
    assert outcome.pr_intent.base == "main"


def test_named_without_target_degrades_to_protect_default(tmp_path: Path) -> None:
    _, _, canonical = _make_origin_clone(tmp_path)
    outcome = resolve_delivery_branch(
        source_path=canonical,
        project_path=canonical,
        run_id="20260702_3",
        base_ref="HEAD",
        branch_policy="named",
        named_branch="",
    )
    assert outcome.policy == "protect_default"
    assert outcome.plan == "commit_on_branch"
    assert outcome.commit_branch is not None
    assert outcome.commit_branch.startswith("orcho/deliver/")
    assert any("requires a target branch" in n for n in outcome.notices)


# --- in-place table (isolation off) --------------------------------------


def test_in_place_head_on_default_protects_with_fresh_branch(tmp_path: Path) -> None:
    _, _, canonical = _make_origin_clone(tmp_path)
    # source == project == on default branch -> must not commit onto default.
    outcome = resolve_delivery_branch(
        source_path=canonical,
        project_path=canonical,
        run_id="20260702_4",
        base_ref="HEAD",
        branch_policy="worktree_branch",
    )
    assert outcome.policy == "protect_default"
    assert outcome.plan == "commit_on_branch"
    assert outcome.commit_branch is not None
    assert outcome.commit_branch.startswith("orcho/deliver/")
    # worktree_branch had no run branch to publish -> it degraded.
    assert any("degraded to protect_default" in n for n in outcome.notices)
    # The canonical checkout is still on the default branch (nothing committed).
    assert _git_out(canonical, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    # checkout_delivery_branch (the commit site's thin call) creates + switches.
    err = checkout_delivery_branch(canonical, outcome.commit_branch)
    assert err is None
    assert _git_out(canonical, "rev-parse", "--abbrev-ref", "HEAD") == outcome.commit_branch


def test_in_place_on_feature_branch_commits_in_place(tmp_path: Path) -> None:
    _, _, canonical = _make_origin_clone(tmp_path)
    _git(canonical, "checkout", "-q", "-b", "feature/x")
    outcome = resolve_delivery_branch(
        source_path=canonical,
        project_path=canonical,
        run_id="20260702_5",
        base_ref="HEAD",
        branch_policy="worktree_branch",
        release_summary="Feature work",
    )
    assert outcome.policy == "protect_default"
    assert outcome.plan == "commit_in_place"
    assert outcome.commit_branch is None
    assert outcome.delivery_branch == "feature/x"
    assert outcome.pr_intent is not None
    assert outcome.pr_intent.branch == "feature/x"
    assert outcome.pr_intent.base == "main"


# --- base_ref anchoring (ADR 0119: create off base_ref, not default) -----


def test_checkout_delivery_branch_anchors_to_base_ref_not_default(
    tmp_path: Path,
) -> None:
    # ADR 0119: a protect_default / named branch is created off the run's
    # ``base_ref`` baseline, NOT the local default branch. When the default
    # branch advances past the baseline between resolve and approve (or base_ref
    # is a bare commit / seed ref that is not a local head), the delivery branch
    # must still anchor to base_ref — otherwise its patch base and PR range
    # include commits that were never part of the run.
    _, _, canonical = _make_origin_clone(tmp_path)
    base_ref = _git_out(canonical, "rev-parse", "HEAD")
    # Default branch advances past the run baseline.
    _commit_file(canonical, "drift.txt", "drift\n", "post-baseline commit on main")
    advanced = _git_out(canonical, "rev-parse", "HEAD")
    assert advanced != base_ref

    # base_ref here is a bare commit SHA (not refs/heads/<name>): the resolver
    # must still create the branch off it, not fall back to the current HEAD.
    err = checkout_delivery_branch(
        canonical, "orcho/deliver/anchor", base_ref=base_ref,
    )
    assert err is None
    # The delivery branch tip is exactly base_ref, not the advanced default tip.
    assert _git_out(canonical, "rev-parse", "HEAD") == base_ref
    # The post-baseline commit's file never leaks onto the delivery branch.
    assert not (canonical / "drift.txt").exists()


# --- worktree_branch publish (isolation per_run) -------------------------


def test_worktree_branch_publishes_rebased_branch_without_touching_canonical(
    tmp_path: Path,
) -> None:
    _, _, canonical = _make_origin_clone(tmp_path)
    base = _git_out(canonical, "rev-parse", "HEAD")
    run_wt = _add_run_worktree(canonical, tmp_path, "run6", base)
    _commit_file(run_wt, "feature.txt", "run change\n", "run commit")
    run_tip = _git_out(run_wt, "rev-parse", "HEAD")

    canonical_head_before = _git_out(canonical, "rev-parse", "HEAD")

    outcome = resolve_delivery_branch(
        source_path=run_wt,
        project_path=canonical,
        run_id="run6",
        base_ref=base,
        branch_policy="worktree_branch",
        release_summary="Publish the run branch",
    )
    assert outcome.plan == "publish"
    # Resolution is pure — nothing published yet.
    assert outcome.published is False
    outcome = publish_delivery_branch(
        source_path=run_wt, project_path=canonical, outcome=outcome,
    )

    assert outcome.policy == "worktree_branch"
    assert outcome.plan == "publish"
    assert outcome.commits_into_checkout is False
    assert outcome.published is True
    assert outcome.rebased is True
    assert outcome.delivery_branch is not None
    assert outcome.delivery_branch.startswith("orcho/deliver/run6-")

    # Canonical checkout untouched: still on main at the same commit, clean tree.
    assert _git_out(canonical, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git_out(canonical, "rev-parse", "HEAD") == canonical_head_before
    assert _git_out(canonical, "status", "--porcelain") == ""

    # The delivery branch exists in the shared object store and carries the run
    # commit. Base was already fresh, so the tip is unchanged (nothing to move).
    assert _git_out(canonical, "rev-parse", "--verify", outcome.delivery_branch)
    assert _git_out(canonical, "rev-parse", outcome.delivery_branch) == run_tip


def test_protect_default_per_run_commits_on_delivery_branch(tmp_path: Path) -> None:
    """protect_default (per_run) is NOT a pure publish: it commits onto a fresh
    delivery branch in the target repo, so it resolves to ``commit_on_branch``
    (commit_sha populated downstream), unlike ``worktree_branch`` which only
    publishes the run branch."""
    _, _, canonical = _make_origin_clone(tmp_path)
    base = _git_out(canonical, "rev-parse", "HEAD")
    run_wt = _add_run_worktree(canonical, tmp_path, "run7", base)
    _commit_file(run_wt, "feature.txt", "run change\n", "run commit")

    outcome = resolve_delivery_branch(
        source_path=run_wt,
        project_path=canonical,
        run_id="run7",
        base_ref=base,
        branch_policy="protect_default",
    )
    assert outcome.policy == "protect_default"
    assert outcome.plan == "commit_on_branch"
    assert outcome.commits_into_checkout is True
    assert outcome.commit_branch is not None
    assert outcome.commit_branch.startswith("orcho/deliver/run7-")
    assert outcome.delivery_branch == outcome.commit_branch
    assert outcome.pr_intent is not None
    # publish is a no-op for a non-publish plan (defensive).
    unchanged = publish_delivery_branch(
        source_path=run_wt, project_path=canonical, outcome=outcome,
    )
    assert unchanged.published is False
    assert unchanged.plan == "commit_on_branch"


def test_worktree_branch_rebases_onto_fresh_default(tmp_path: Path) -> None:
    _, seed, canonical = _make_origin_clone(tmp_path)
    base = _git_out(canonical, "rev-parse", "HEAD")
    run_wt = _add_run_worktree(canonical, tmp_path, "run8", base)
    _commit_file(run_wt, "feature.txt", "run change\n", "run commit")

    # origin/main advances after the run forked off ``base``.
    _commit_file(seed, "upstream.txt", "moved\n", "upstream commit")
    _git(seed, "push", "-q", "origin", "main")
    new_main = _git_out(seed, "rev-parse", "HEAD")

    outcome = resolve_delivery_branch(
        source_path=run_wt,
        project_path=canonical,
        run_id="run8",
        base_ref=base,
        branch_policy="worktree_branch",
    )
    outcome = publish_delivery_branch(
        source_path=run_wt, project_path=canonical, outcome=outcome,
    )
    assert outcome.rebased is True
    assert outcome.delivery_branch is not None
    # The freshly-fetched upstream commit is now an ancestor of the delivery
    # branch: the run commit was replayed on top of it.
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", new_main, outcome.delivery_branch],
        cwd=canonical,
        check=True,
        timeout=30,
    )


def test_worktree_branch_rebase_conflict_publishes_unrebased_with_warning(
    tmp_path: Path,
) -> None:
    _, seed, canonical = _make_origin_clone(tmp_path, seed_file="conflict.txt")
    base = _git_out(canonical, "rev-parse", "HEAD")
    run_wt = _add_run_worktree(canonical, tmp_path, "run9", base)
    # Run and upstream edit the same file differently -> rebase conflict.
    _commit_file(run_wt, "conflict.txt", "run edit\n", "run commit")
    run_tip = _git_out(run_wt, "rev-parse", "HEAD")

    _commit_file(seed, "conflict.txt", "upstream edit\n", "upstream commit")
    _git(seed, "push", "-q", "origin", "main")
    new_main = _git_out(seed, "rev-parse", "HEAD")

    outcome = resolve_delivery_branch(
        source_path=run_wt,
        project_path=canonical,
        run_id="run9",
        base_ref=base,
        branch_policy="worktree_branch",
    )
    outcome = publish_delivery_branch(
        source_path=run_wt, project_path=canonical, outcome=outcome,
    )
    assert outcome.published is True
    assert outcome.rebased is False
    assert any("conflict.txt" in w for w in outcome.warnings)
    # Published un-rebased: tip is still the original run commit, and the moved
    # upstream is NOT an ancestor.
    assert _git_out(canonical, "rev-parse", outcome.delivery_branch) == run_tip
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", new_main, outcome.delivery_branch],
        cwd=canonical,
        capture_output=True,
        timeout=30,
    )
    assert ancestor.returncode != 0


def test_worktree_branch_offline_degrades_to_local_branch(tmp_path: Path) -> None:
    # A repo with no configured remote: publish must still produce a local
    # delivery branch and emit a "push when a remote is available" notice.
    repo = tmp_path / "local"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _identity(repo)
    _commit_file(repo, "a.txt", "base\n", "init")
    base = _git_out(repo, "rev-parse", "HEAD")
    run_wt = _add_run_worktree(repo, tmp_path, "run10", base)
    _commit_file(run_wt, "feature.txt", "run change\n", "run commit")

    outcome = resolve_delivery_branch(
        source_path=run_wt,
        project_path=repo,
        run_id="run10",
        base_ref=base,
        branch_policy="worktree_branch",
    )
    outcome = publish_delivery_branch(
        source_path=run_wt, project_path=repo, outcome=outcome,
    )
    assert outcome.published is True
    assert outcome.delivery_branch is not None
    assert _git_out(repo, "rev-parse", "--verify", outcome.delivery_branch)
    assert any("remote is available" in n for n in outcome.notices)


# --- pr_intent shape -----------------------------------------------------


def test_pr_intent_shape_is_provider_neutral(tmp_path: Path) -> None:
    _, _, canonical = _make_origin_clone(tmp_path)
    base = _git_out(canonical, "rev-parse", "HEAD")
    run_wt = _add_run_worktree(canonical, tmp_path, "run11", base)
    _commit_file(run_wt, "feature.txt", "run change\n", "run commit")

    outcome = resolve_delivery_branch(
        source_path=run_wt,
        project_path=canonical,
        run_id="run11",
        base_ref=base,
        branch_policy="worktree_branch",
        release_summary="Add branch policy\n\nlonger body ignored",
    )
    intent = outcome.pr_intent
    assert isinstance(intent, DeliveryPrIntent)
    assert intent.branch == outcome.delivery_branch
    assert intent.base == "main"
    # Title lifted from the first line of the release summary.
    assert intent.title == "Add branch policy"
    # Provider-neutral: plain git, never gh/glab.
    assert intent.suggested_command.startswith("git push")
    assert "gh " not in intent.suggested_command
    assert "glab" not in intent.suggested_command
    assert set(intent.to_dict()) == {"branch", "base", "title", "suggested_command"}


def test_normalize_branch_policy_defaults_and_validates() -> None:
    assert normalize_branch_policy(None) == "worktree_branch"
    assert normalize_branch_policy("") == "worktree_branch"
    assert normalize_branch_policy("nonsense") == "worktree_branch"
    for policy in ("worktree_branch", "protect_default", "named", "bypass"):
        assert normalize_branch_policy(policy) == policy


# --- degrade / fallback branches -----------------------------------------


def test_detect_default_branch_last_resort_returns_main(tmp_path: Path) -> None:
    # No origin/HEAD and neither a main nor master head -> last-resort "main".
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    _identity(repo)
    _commit_file(repo, "a.txt", "x\n", "init")
    assert detect_default_branch(repo) == "main"


def test_checkout_delivery_branch_falls_back_to_head_when_base_unresolvable(
    tmp_path: Path,
) -> None:
    # An unresolvable base_ref must not abort: the branch is still created, off
    # the current HEAD, so delivery degrades rather than failing hard.
    _, _, canonical = _make_origin_clone(tmp_path)
    head = _git_out(canonical, "rev-parse", "HEAD")
    err = checkout_delivery_branch(
        canonical, "orcho/deliver/fallback", base_ref="0000000000nonexistent",
    )
    assert err is None
    assert _git_out(canonical, "rev-parse", "--abbrev-ref", "HEAD") == "orcho/deliver/fallback"
    assert _git_out(canonical, "rev-parse", "HEAD") == head  # off current HEAD


def test_slugify_truncates_long_summary_and_current_branch_detached(
    tmp_path: Path,
) -> None:
    from pipeline.engine.delivery_branch import (
        _SLUG_MAX_LEN,
        _current_branch,
        _slugify,
    )

    slug = _slugify("word " * 100)
    assert len(slug) <= _SLUG_MAX_LEN
    assert not slug.endswith("-")

    # Detached HEAD -> _current_branch is None (drives the in-place resolution
    # branch that treats a detached checkout as non-default).
    _, _, canonical = _make_origin_clone(tmp_path)
    _git(canonical, "checkout", "-q", "--detach", "HEAD")
    assert _current_branch(canonical) is None
