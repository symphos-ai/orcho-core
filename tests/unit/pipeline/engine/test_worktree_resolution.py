"""WorktreeContext + resolve_worktree_for_run + teardown (ADR 0033).

Domain-level tests for the orcho-managed worktree lifecycle helper.
Lower-level git CLI interactions are pinned in
:mod:`tests.unit.core.io.test_git_helpers_worktree`; this file
focuses on the resolution policy (mode precedence, degraded paths,
config validation) and on the wire shape (``to_dict`` projection).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pipeline.engine.worktree import (
    WorktreeConfigError,
    get_active_worktree_checkout,
    reset_active_worktree_checkout,
    resolve_worktree_for_run,
    set_active_worktree_checkout,
    teardown_worktree,
)


def _init_repo(repo: Path) -> str:
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
    (repo / "f.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _checkout_path(run_dir: Path, run_id: str) -> Path:
    root = run_dir.parent.parent if run_dir.parent.name == "runs" else run_dir.parent
    return root / "worktrees" / f"wt_{run_id}" / "checkout"


# ── Branch / run_id decoupling (cross worktree collision fix) ───────────────


class TestBranchRunIdDecoupling:
    """``branch_run_id`` makes the orcho branch unique without moving the
    checkout path. Cross children keep a stable ``run_id`` = alias (so the
    ``wt_<alias>`` path contract holds for finalization/delivery) but need a
    per-cross-run branch — a stable ``orcho/run/<alias>`` collides on the
    second cross run's ``git worktree add -b`` and silently degrades to
    in-place.
    """

    def test_branch_run_id_overrides_branch_but_not_path(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "runs" / "20260529_010101"
        run_dir.mkdir(parents=True)

        ctx = resolve_worktree_for_run(
            run_id="web",                       # alias — drives wt_<alias> path
            project_dir=repo,
            run_dir=run_dir,
            branch_run_id="20260529_010101__web",  # per-cross-run branch
        )

        assert ctx.is_isolated
        # Path identity stays alias-keyed (the finalization/delivery contract).
        assert ctx.path == _checkout_path(run_dir, "web")
        # Branch is the unique per-cross-run namespace, NOT orcho/run/web.
        assert ctx.branch == "orcho/run/20260529_010101__web"

    def test_second_cross_run_same_alias_does_not_collide(self, tmp_path: Path) -> None:
        """Reproduces the bug: two cross runs, same alias ``run_id`` against
        the SAME source repo. With a per-cross-run ``branch_run_id`` both
        resolve to isolated worktrees on distinct branches — the second does
        NOT degrade to in-place (which is what a stable branch caused)."""
        repo = tmp_path / "src"
        _init_repo(repo)

        # Mirror the production child run_dir shape: runs/<cross_ts>/<alias>.
        # ``parent.name != "runs"`` → the worktree root is per-cross-run
        # (runs/<cross_ts>/worktrees), so target_path differs between runs and
        # the second run does NOT hit reuse-detection — it actually creates.
        run_dir_a = tmp_path / "runs" / "cross_A" / "web"
        run_dir_a.mkdir(parents=True)
        ctx_a = resolve_worktree_for_run(
            run_id="web", project_dir=repo, run_dir=run_dir_a,
            branch_run_id="cross_A__web",
        )

        run_dir_b = tmp_path / "runs" / "cross_B" / "web"
        run_dir_b.mkdir(parents=True)
        ctx_b = resolve_worktree_for_run(
            run_id="web", project_dir=repo, run_dir=run_dir_b,
            branch_run_id="cross_B__web",
        )

        # Both isolated — the second did not silently fall back to source.
        assert ctx_a.is_isolated and ctx_b.is_isolated
        assert ctx_a.degraded_reason is None
        assert ctx_b.degraded_reason is None, (
            f"second cross run degraded to in-place: {ctx_b.degraded_reason}"
        )
        assert ctx_a.branch != ctx_b.branch
        assert ctx_a.path != ctx_b.path

    def test_stable_branch_reproduces_the_collision(self, tmp_path: Path) -> None:
        """Guard the regression mechanism: WITHOUT the per-cross-run branch
        (branch_run_id unset → branch = run_id = alias), the second run on the
        same repo collides on ``git worktree add -b`` and degrades to off —
        the exact silent isolation-loss this fix prevents."""
        repo = tmp_path / "src"
        _init_repo(repo)

        run_dir_a = tmp_path / "runs" / "cross_A" / "web"
        run_dir_a.mkdir(parents=True)
        ctx_a = resolve_worktree_for_run(
            run_id="web", project_dir=repo, run_dir=run_dir_a,
        )
        assert ctx_a.is_isolated  # first run creates orcho/run/web

        run_dir_b = tmp_path / "runs" / "cross_B" / "web"
        run_dir_b.mkdir(parents=True)
        ctx_b = resolve_worktree_for_run(
            run_id="web", project_dir=repo, run_dir=run_dir_b,
        )
        # Stable branch collides → degraded to in-place (the bug).
        assert not ctx_b.is_isolated
        assert ctx_b.degraded_reason is not None


# ── Mode resolution ────────────────────────────────────────────────────────


class TestModeResolution:
    def test_default_mode_is_per_run(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        ctx = resolve_worktree_for_run(
            run_id="20260522_001",
            project_dir=repo,
            run_dir=run_dir,
        )

        assert ctx.mode == "per_run"
        assert ctx.path == _checkout_path(run_dir, "20260522_001")
        assert ctx.branch == "orcho/run/20260522_001"
        assert ctx.is_isolated

    def test_enabled_false_short_circuits_to_off(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        ctx = resolve_worktree_for_run(
            run_id="20260522_002",
            project_dir=repo,
            run_dir=run_dir,
            worktree_config={"enabled": False},
        )

        assert ctx.mode == "off"
        assert ctx.path == repo
        assert ctx.branch is None
        assert not ctx.is_isolated
        # No checkout dir was created.
        assert not _checkout_path(run_dir, "20260522_002").exists()

    def test_explicit_off_in_config(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        ctx = resolve_worktree_for_run(
            run_id="20260522_003",
            project_dir=repo,
            run_dir=run_dir,
            worktree_config={"isolation": "off"},
        )

        assert ctx.mode == "off"
        assert ctx.path == repo

    def test_profile_isolation_overrides_config(self, tmp_path: Path) -> None:
        """Profile-level worktree_isolation takes precedence over the
        global config — a profile that explicitly opts into isolation
        cannot be silently switched off by a global ``isolation=off``."""
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        ctx = resolve_worktree_for_run(
            run_id="20260522_004",
            project_dir=repo,
            run_dir=run_dir,
            worktree_config={"isolation": "off"},
            profile_isolation="per_run",
        )

        assert ctx.mode == "per_run"

    def test_per_phase_rejected_at_runtime(self, tmp_path: Path) -> None:
        """``per_phase`` is schema-valid (so DAG-2 profiles can declare
        intent) but v1 runtime rejects with a clear error."""
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        with pytest.raises(WorktreeConfigError, match="per_phase"):
            resolve_worktree_for_run(
                run_id="20260522_005",
                project_dir=repo,
                run_dir=run_dir,
                profile_isolation="per_phase",
            )

    def test_unknown_isolation_rejected(self, tmp_path: Path) -> None:
        """Typos surface loudly — silently downgrading ``per_runn`` to
        ``off`` would hide bugs in profile JSON / config.local."""
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        with pytest.raises(WorktreeConfigError, match="one of"):
            resolve_worktree_for_run(
                run_id="20260522_006",
                project_dir=repo,
                run_dir=run_dir,
                worktree_config={"isolation": "per_runn"},
            )


# ── Degraded paths ─────────────────────────────────────────────────────────


class TestDegradedPaths:
    def test_non_git_project_with_isolation_raises(self, tmp_path: Path) -> None:
        """Non-git ``project_dir`` with isolation requested is a hard
        misconfiguration — silently degrading to off would let later
        phases edit the user's tree while review gates diff a non-repo
        and pass an unreviewed change. Surface it as a config error."""
        non_git = tmp_path / "not_a_repo"
        non_git.mkdir()
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        with pytest.raises(WorktreeConfigError, match="not a git repository"):
            resolve_worktree_for_run(
                run_id="20260522_007",
                project_dir=non_git,
                run_dir=run_dir,
            )

    def test_non_git_project_with_isolation_off_returns_off(
        self, tmp_path: Path,
    ) -> None:
        """When the operator explicitly opts out of isolation
        (``worktree.enabled=false``), a non-git project_dir is fine —
        the off-mode short-circuit fires before the git-repo check."""
        non_git = tmp_path / "not_a_repo"
        non_git.mkdir()
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        ctx = resolve_worktree_for_run(
            run_id="20260522_007b",
            project_dir=non_git,
            run_dir=run_dir,
            worktree_config={"enabled": False},
        )

        assert ctx.mode == "off"
        assert ctx.path == non_git

    def test_git_failure_falls_back_to_off_with_reason(
        self, tmp_path: Path,
    ) -> None:
        """Pre-existing rogue dir at target_path must degrade gracefully.

        The resolver detects the directory exists but lacks a .git gitlink,
        so it is not a valid orcho worktree. It falls back to mode='off'
        with a descriptive degraded_reason rather than calling create_worktree
        (which would also fail, but the reuse-detection path catches it first).
        """
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _checkout_path(run_dir, "20260522_008").mkdir(parents=True)

        ctx = resolve_worktree_for_run(
            run_id="20260522_008",
            project_dir=repo,
            run_dir=run_dir,
        )

        assert ctx.mode == "off"
        assert ctx.degraded_reason is not None
        assert "not a registered orcho worktree" in ctx.degraded_reason


# ── Retention ──────────────────────────────────────────────────────────────


class TestRetention:
    def test_default_retention_days_populates_timestamp(
        self, tmp_path: Path,
    ) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        ctx = resolve_worktree_for_run(
            run_id="20260522_009",
            project_dir=repo,
            run_dir=run_dir,
        )

        assert ctx.retention_until is not None
        # ISO8601, contains a date.
        assert "T" in ctx.retention_until
        # Year extracted from ISO timestamp; just sanity-check that
        # it's not 1970 or some default.
        assert ctx.retention_until.startswith("20")

    def test_zero_retention_disables_timestamp(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        ctx = resolve_worktree_for_run(
            run_id="20260522_010",
            project_dir=repo,
            run_dir=run_dir,
            worktree_config={"retention_days": 0},
        )
        assert ctx.retention_until is None

    def test_negative_retention_disables_timestamp(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        ctx = resolve_worktree_for_run(
            run_id="20260522_011",
            project_dir=repo,
            run_dir=run_dir,
            worktree_config={"retention_days": -5},
        )
        assert ctx.retention_until is None

    def test_non_integer_retention_raises(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        with pytest.raises(WorktreeConfigError, match="retention_days"):
            resolve_worktree_for_run(
                run_id="20260522_012",
                project_dir=repo,
                run_dir=run_dir,
                worktree_config={"retention_days": "seven"},
            )


# ── Source repo preservation ───────────────────────────────────────────────


class TestSourceRepoPreservation:
    def test_isolated_run_does_not_touch_user_dirt(self, tmp_path: Path) -> None:
        """The load-bearing isolation guarantee from the GWT-1 ADR:
        creating an orcho worktree must not affect uncommitted work
        in the user's source checkout."""
        repo = tmp_path / "src"
        _init_repo(repo)
        # Stage some user-owned dirt.
        (repo / "user_wip.txt").write_text("WIP\n", encoding="utf-8")
        (repo / "f.txt").write_text("modified by user\n", encoding="utf-8")
        before_wip = (repo / "user_wip.txt").read_text(encoding="utf-8")
        before_f = (repo / "f.txt").read_text(encoding="utf-8")

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        ctx = resolve_worktree_for_run(
            run_id="20260522_013",
            project_dir=repo,
            run_dir=run_dir,
        )
        assert ctx.mode == "per_run"

        # User's source checkout is byte-identical after worktree
        # creation — every file the user was editing stays as-is.
        assert (repo / "user_wip.txt").read_text(encoding="utf-8") == before_wip
        assert (repo / "f.txt").read_text(encoding="utf-8") == before_f

        # The orcho worktree starts from base HEAD, NOT from the
        # user's uncommitted state — the worktree's f.txt is the
        # committed content, not the modified one.
        worktree_f = (ctx.path / "f.txt").read_text(encoding="utf-8")
        assert worktree_f == "a\n", (
            f"orcho worktree should start at base HEAD, not "
            f"user's uncommitted state; got {worktree_f!r}"
        )


# ── Follow-up attachment ───────────────────────────────────────────────────


class TestFollowupAttachment:
    def test_followup_reuses_parent_worktree_and_updates_manifest(
        self, tmp_path: Path,
    ) -> None:
        repo = tmp_path / "src"
        base_ref = _init_repo(repo)
        runs_dir = tmp_path / "runspace" / "runs"
        parent_run_dir = runs_dir / "20260522_020"
        child_run_dir = runs_dir / "20260522_021"
        parent_run_dir.mkdir(parents=True)
        child_run_dir.mkdir(parents=True)

        parent = resolve_worktree_for_run(
            run_id="20260522_020",
            project_dir=repo,
            run_dir=parent_run_dir,
        )
        parent_meta = parent.to_dict()
        child = resolve_worktree_for_run(
            run_id="20260522_021",
            project_dir=repo,
            run_dir=child_run_dir,
            followup_parent_worktree=parent_meta,
            followup_parent_run_id="20260522_020",
        )

        assert child.path == parent.path
        assert child.worktree_id == "wt_20260522_020"
        assert child.root_run_id == "20260522_020"
        assert child.branch == "orcho/run/20260522_020"
        assert child.base_ref == base_ref
        assert not _checkout_path(child_run_dir, "20260522_021").exists()

        manifest_path = parent.path.parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["worktree_id"] == "wt_20260522_020"
        assert manifest["kind"] == "primary"
        assert manifest["root_run_id"] == "20260522_020"
        assert manifest["attached_run_ids"] == [
            "20260522_020",
            "20260522_021",
        ]
        assert manifest["source_repo_path"] == str(repo)
        assert manifest["source_start_head"] == base_ref

    def test_followup_with_missing_parent_worktree_is_rejected(
        self, tmp_path: Path,
    ) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "runspace" / "runs" / "20260522_022"
        run_dir.mkdir(parents=True)

        with pytest.raises(WorktreeConfigError, match="parent worktree"):
            resolve_worktree_for_run(
                run_id="20260522_022",
                project_dir=repo,
                run_dir=run_dir,
                followup_parent_worktree={
                    "path": str(tmp_path / "missing" / "checkout"),
                },
                followup_parent_run_id="20260522_020",
            )


# ── Wire shape ─────────────────────────────────────────────────────────────


class TestToDictWireShape:
    def test_per_run_serialises_full_payload(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        ctx = resolve_worktree_for_run(
            run_id="20260522_014",
            project_dir=repo,
            run_dir=run_dir,
        )

        out = ctx.to_dict()
        assert out["isolation"] == "per_run"
        assert out["path"].endswith("/worktrees/wt_20260522_014/checkout")
        assert out["base_ref"]  # truthy sha
        assert out["branch_ref"] == "orcho/run/20260522_014"
        assert "retention_until" in out
        # Degraded reason absent on happy path.
        assert "degraded_reason" not in out

    def test_reuse_existing_worktree_heals_missing_manifest(
        self, tmp_path: Path,
    ) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        first = resolve_worktree_for_run(
            run_id="20260522_014b",
            project_dir=repo,
            run_dir=run_dir,
        )
        manifest_path = first.path.parent / "manifest.json"
        manifest_path.unlink()

        second = resolve_worktree_for_run(
            run_id="20260522_014b",
            project_dir=repo,
            run_dir=run_dir,
        )

        assert second.path == first.path
        assert manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["worktree_id"] == "wt_20260522_014b"
        assert manifest["attached_run_ids"] == ["20260522_014b"]

    def test_off_mode_serialises_minimal_payload(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        ctx = resolve_worktree_for_run(
            run_id="20260522_015",
            project_dir=repo,
            run_dir=run_dir,
            worktree_config={"isolation": "off"},
        )

        out = ctx.to_dict()
        assert out["isolation"] == "off"
        assert out["branch_ref"] is None
        assert "retention_until" not in out

    def test_degraded_mode_surfaces_reason(self, tmp_path: Path) -> None:
        """A rogue dir at the target worktree path triggers the
        non-fatal degraded branch (git worktree add would also fail,
        but the reuse-detection path catches it first). Wire shape
        must include the descriptive ``degraded_reason``.
        """
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _checkout_path(run_dir, "20260522_016").mkdir(parents=True)

        ctx = resolve_worktree_for_run(
            run_id="20260522_016",
            project_dir=repo,
            run_dir=run_dir,
        )

        out = ctx.to_dict()
        assert out["isolation"] == "off"
        assert "degraded_reason" in out


# ── Teardown ───────────────────────────────────────────────────────────────


class TestTeardown:
    def test_teardown_removes_isolated_worktree(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        ctx = resolve_worktree_for_run(
            run_id="20260522_017",
            project_dir=repo,
            run_dir=run_dir,
        )
        assert ctx.path.exists()

        result = teardown_worktree(ctx)
        assert result.ok, result.error
        assert not ctx.path.exists()

    def test_teardown_retain_keeps_worktree(self, tmp_path: Path) -> None:
        """``retain=True`` is the run-end default: the worktree stays
        on disk for inspection until ``orcho gc`` (PR4) sweeps past
        its retention TTL."""
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        ctx = resolve_worktree_for_run(
            run_id="20260522_018",
            project_dir=repo,
            run_dir=run_dir,
        )

        result = teardown_worktree(ctx, retain=True)
        assert result.ok, result.error
        # Still on disk.
        assert ctx.path.exists()
        # The result carries a retention note for callers that log.
        assert "retained" in (result.error or "")

    def test_teardown_off_mode_is_noop(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        _init_repo(repo)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        ctx = resolve_worktree_for_run(
            run_id="20260522_019",
            project_dir=repo,
            run_dir=run_dir,
            worktree_config={"enabled": False},
        )

        # off-mode teardown: just succeed, don't touch anything.
        result = teardown_worktree(ctx)
        assert result.ok
        # User's checkout still on disk (sanity guard).
        assert repo.exists()


# ── F2 regression: ContextVar-based active-checkout registry ─────────────────
# Tests that the ContextVar API is correct and that a directory named
# "checkout" does NOT get treated as an orcho-managed worktree unless the
# orchestrator explicitly registers it.


class TestActiveCheckoutContextVar:
    def test_default_is_none(self) -> None:
        # Without any orchestrator setup the ContextVar returns None.
        # A fresh ContextVar starts at its default, but tests share the
        # main thread — use a token-reset to be safe.
        token = set_active_worktree_checkout(None)
        try:
            assert get_active_worktree_checkout() is None
        finally:
            reset_active_worktree_checkout(token)

    def test_set_and_get_round_trip(self) -> None:
        token = set_active_worktree_checkout("/run/20260522/checkout")
        try:
            assert get_active_worktree_checkout() == "/run/20260522/checkout"
        finally:
            reset_active_worktree_checkout(token)

    def test_reset_restores_previous_value(self) -> None:
        outer = set_active_worktree_checkout("/run/outer/checkout")
        inner = set_active_worktree_checkout("/run/inner/checkout")
        reset_active_worktree_checkout(inner)
        assert get_active_worktree_checkout() == "/run/outer/checkout"
        reset_active_worktree_checkout(outer)

    def test_cwd_named_checkout_without_registration_is_not_active(self) -> None:
        """F2 regression: a real project at /some/path/checkout must not
        be treated as an orcho-managed worktree unless the ContextVar was
        set. This is the basename-collision guard that the old heuristic
        failed to prevent.
        """
        # No ContextVar set (or explicitly set to None).
        token = set_active_worktree_checkout(None)
        try:
            active = get_active_worktree_checkout()
            cwd = "/real/project/checkout"
            # Guard relaxation requires: active is not None AND cwd == active.
            in_worktree = active is not None and cwd == active
            assert not in_worktree, (
                "A cwd named 'checkout' should NOT be treated as a worktree "
                "without an explicit ContextVar registration."
            )
        finally:
            reset_active_worktree_checkout(token)
