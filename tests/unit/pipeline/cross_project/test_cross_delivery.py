"""Cross-level commit delivery tests (ADR cross-delivery + CFA pause, Phase B).

Proves the P0 fix: an APPROVED (or operator-overridden) cross run
transports each alias's worktree diff into the project checkout and
commits it. Exercises the outcome matrix, the override path, idempotent
resume, alias-scoped artifact dirs, the phase-scoped evidence location,
and the finalizer status mapping + event ordering.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.io.git_helpers import create_worktree
from pipeline.cross_project.cross_delivery import run_cross_delivery

pytestmark = [pytest.mark.cross_project, pytest.mark.git_worktree]


# ── git fixtures ──────────────────────────────────────────────────────


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
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return _head(repo)


def _head(repo: Path) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
        text=True, check=True,
    )
    return r.stdout.strip()


def _git_log_subjects(repo: Path) -> list[str]:
    r = subprocess.run(
        ["git", "log", "--format=%s"], cwd=repo, capture_output=True,
        text=True, check=True,
    )
    return [line for line in r.stdout.splitlines() if line.strip()]


def _worktree(repo: Path, run_dir: Path, alias: str) -> Path:
    result = create_worktree(
        repo=repo,
        base_ref=_head(repo),
        target_path=run_dir / "worktrees" / f"wt_{alias}" / "checkout",
        branch_name=f"orcho/run/{alias}",
    )
    assert result.ok, result.error
    return run_dir / "worktrees" / f"wt_{alias}" / "checkout"


def _child_session(
    *, worktree: Path | None, seed: str, verdict: str = "APPROVED",
    summary: str = "fix: deliver change",
) -> dict:
    """Build a child (per-alias) session as stored under cross
    ``session["phases"]["projects"][alias]``."""
    child: dict = {
        "pre_run_dirty": {"seed_tree_sha": seed},
        "phases": {
            "final_acceptance": {"verdict": verdict, "short_summary": summary},
        },
    }
    if worktree is not None:
        child["worktree"] = {"path": str(worktree), "base_ref": seed}
    return child


def _app_cfg(
    enabled: bool = True,
    action: str = "approve",
    branch_policy: str | None = "bypass",
) -> SimpleNamespace:
    # These legacy cross tests assert the "commit the alias diff onto each
    # project checkout" contract, which under ADR 0119 is the ``bypass`` policy —
    # opt into it explicitly. A ``None`` policy omits the key entirely so the
    # commit site resolves the ``worktree_branch`` default (default-branch
    # protection).
    commit: dict = {
        "enabled": enabled,
        "auto_in_ci": action,
        "add_untracked": True,
    }
    if branch_policy is not None:
        commit["branch_policy"] = branch_policy
    return SimpleNamespace(commit=commit)


def _make_alias(
    tmp_path: Path, cross_run_dir: Path, alias: str, *, change: bool = True,
    verdict: str = "APPROVED",
) -> tuple[Path, dict]:
    """Create a project repo + worktree (optionally with a change) and
    return ``(project_path, child_session)``."""
    repo = tmp_path / alias
    seed = _init_repo(repo)
    wt = _worktree(repo, cross_run_dir, alias)
    if change:
        (wt / "app.txt").write_text(f"base\n{alias}-change\n", encoding="utf-8")
    child = _child_session(worktree=wt, seed=seed, verdict=verdict)
    return repo, child


# ── outcome matrix ────────────────────────────────────────────────────


def test_per_alias_clean_targets_all_commit(tmp_path: Path) -> None:
    """The headline P0 fix: APPROVED cross → real commits in BOTH
    project checkouts; SHAs recorded in the phase-scoped evidence."""
    cross_run_dir = tmp_path / "run"
    cross_run_dir.mkdir()
    api_repo, api_child = _make_alias(tmp_path, cross_run_dir, "api")
    web_repo, web_child = _make_alias(tmp_path, cross_run_dir, "web")
    session = {"phases": {"projects": {"api": api_child, "web": web_child}}}

    result = run_cross_delivery(
        session=session,
        projects={"api": api_repo, "web": web_repo},
        app_cfg=_app_cfg(),
        cross_run_dir=cross_run_dir,
        terminal=False,
    )

    assert result.overall == "ok"
    # Both project checkouts gained a delivery commit.
    assert any("deliver" in s for s in _git_log_subjects(api_repo))
    assert any("deliver" in s for s in _git_log_subjects(web_repo))
    # The fix landed in the source checkout.
    assert "api-change" in (api_repo / "app.txt").read_text()
    assert "web-change" in (web_repo / "app.txt").read_text()
    # Phase-scoped evidence carries per-alias SHAs.
    ev = session["phases"]["cross_delivery"]
    assert ev["overall"] == "ok"
    assert ev["per_alias"]["api"]["status"] == "committed"
    assert ev["per_alias"]["api"]["commit_sha"]
    assert ev["per_alias"]["web"]["status"] == "committed"


def test_missing_branch_policy_protects_project_default_branch(
    tmp_path: Path,
) -> None:
    """ADR 0119 invariant for cross: a missing ``commit.branch_policy`` resolves
    to ``worktree_branch`` (never a hidden ``bypass``), so an APPROVED cross run
    publishes a delivery branch instead of auto-committing onto each project's
    default branch."""
    cross_run_dir = tmp_path / "run"
    cross_run_dir.mkdir()
    api_repo, api_child = _make_alias(tmp_path, cross_run_dir, "api")
    old_head = _head(api_repo)
    session = {"phases": {"projects": {"api": api_child}}}

    result = run_cross_delivery(
        session=session,
        projects={"api": api_repo},
        app_cfg=_app_cfg(branch_policy=None),
        cross_run_dir=cross_run_dir,
        terminal=False,
    )

    assert result.overall == "ok"
    rec = result.per_alias[0]
    assert rec.status == "committed"
    # Invariant: the default branch never moved and no commit_sha was produced —
    # the deliverable is a published branch, not a commit onto ``main``.
    assert _head(api_repo) == old_head
    assert _git_log_subjects(api_repo) == ["init"]
    assert rec.commit_sha is None
    # The run's work is published as an ``orcho/deliver/…`` branch.
    branches = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        cwd=api_repo, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert any(b.startswith("orcho/deliver/") for b in branches)


def test_outcome_matrix_classifies_no_diff_as_success(tmp_path: Path) -> None:
    cross_run_dir = tmp_path / "run"
    cross_run_dir.mkdir()
    repo, child = _make_alias(tmp_path, cross_run_dir, "api", change=False)
    session = {"phases": {"projects": {"api": child}}}

    result = run_cross_delivery(
        session=session, projects={"api": repo}, app_cfg=_app_cfg(),
        cross_run_dir=cross_run_dir, terminal=False,
    )

    assert result.overall == "ok"
    assert result.per_alias[0].status == "no_diff"


def test_outcome_matrix_classifies_applied_uncommitted_as_success(
    tmp_path: Path,
) -> None:
    cross_run_dir = tmp_path / "run"
    cross_run_dir.mkdir()
    repo, child = _make_alias(tmp_path, cross_run_dir, "api")
    session = {"phases": {"projects": {"api": child}}}

    result = run_cross_delivery(
        session=session, projects={"api": repo},
        app_cfg=_app_cfg(action="apply"),
        cross_run_dir=cross_run_dir, terminal=False,
    )

    assert result.overall == "ok"
    assert result.per_alias[0].status == "applied_uncommitted"
    # apply = transported, NOT committed.
    assert _git_log_subjects(repo) == ["init"]
    assert "api-change" in (repo / "app.txt").read_text()


def test_no_worktree_alias_is_noop_success(tmp_path: Path) -> None:
    """A plan-only / non-isolated child (no worktree) has nothing to
    deliver — success-like, never demotes the run."""
    cross_run_dir = tmp_path / "run"
    cross_run_dir.mkdir()
    repo = tmp_path / "api"
    seed = _init_repo(repo)
    child = _child_session(worktree=None, seed=seed)
    session = {"phases": {"projects": {"api": child}}}

    result = run_cross_delivery(
        session=session, projects={"api": repo}, app_cfg=_app_cfg(),
        cross_run_dir=cross_run_dir, terminal=False,
    )

    assert result.overall == "ok"
    assert result.per_alias[0].status == "no_diff"


def test_per_alias_target_dirty_recorded_continues(tmp_path: Path) -> None:
    """One alias's project checkout is dirty → target_dirty (recorded,
    failure-like); the other still commits → overall partial."""
    cross_run_dir = tmp_path / "run"
    cross_run_dir.mkdir()
    api_repo, api_child = _make_alias(tmp_path, cross_run_dir, "api")
    web_repo, web_child = _make_alias(tmp_path, cross_run_dir, "web")
    # Make the api project checkout dirty so delivery pauses (headless →
    # records target_dirty + continues to web).
    (api_repo / "untracked_local.txt").write_text("dirty\n", encoding="utf-8")
    session = {"phases": {"projects": {"api": api_child, "web": web_child}}}

    result = run_cross_delivery(
        session=session, projects={"api": api_repo, "web": web_repo},
        app_cfg=_app_cfg(), cross_run_dir=cross_run_dir, terminal=False,
    )

    assert result.overall == "partial"
    by_alias = {r.alias: r for r in result.per_alias}
    assert by_alias["api"].status == "target_dirty"
    assert by_alias["web"].status == "committed"
    # web still shipped despite the dirty sibling — no global rollback.
    assert any("deliver" in s for s in _git_log_subjects(web_repo))


def test_all_aliases_fail_yields_failed(tmp_path: Path) -> None:
    cross_run_dir = tmp_path / "run"
    cross_run_dir.mkdir()
    api_repo, api_child = _make_alias(tmp_path, cross_run_dir, "api")
    web_repo, web_child = _make_alias(tmp_path, cross_run_dir, "web")
    (api_repo / "x.txt").write_text("dirty\n", encoding="utf-8")
    (web_repo / "y.txt").write_text("dirty\n", encoding="utf-8")
    session = {"phases": {"projects": {"api": api_child, "web": web_child}}}

    result = run_cross_delivery(
        session=session, projects={"api": api_repo, "web": web_repo},
        app_cfg=_app_cfg(), cross_run_dir=cross_run_dir, terminal=False,
    )

    assert result.overall == "failed"
    assert all(r.status == "target_dirty" for r in result.per_alias)


def test_delivery_disabled_by_config_is_success(tmp_path: Path) -> None:
    """commit.enabled=False → operator turned delivery off → success-like
    with the ``disabled_by_config`` contract set."""
    cross_run_dir = tmp_path / "run"
    cross_run_dir.mkdir()
    repo, child = _make_alias(tmp_path, cross_run_dir, "api")
    session = {"phases": {"projects": {"api": child}}}

    result = run_cross_delivery(
        session=session, projects={"api": repo},
        app_cfg=_app_cfg(enabled=False),
        cross_run_dir=cross_run_dir, terminal=False,
    )

    assert result.overall == "disabled"
    assert result.disabled_by_config is True
    # Nothing committed.
    assert _git_log_subjects(repo) == ["init"]


# ── override path (invariant 6) ───────────────────────────────────────


def test_override_delivers_despite_rejected_child_final_acceptance(
    tmp_path: Path,
) -> None:
    """CFA override-continue: child verdict != APPROVED, but the operator
    chose to ship → delivery still runs, and the original verdict is
    preserved in ``release_override``."""
    cross_run_dir = tmp_path / "run"
    cross_run_dir.mkdir()
    repo, child = _make_alias(
        tmp_path, cross_run_dir, "api", verdict="REJECTED",
    )
    session = {"phases": {"projects": {"api": child}}}

    result = run_cross_delivery(
        session=session, projects={"api": repo}, app_cfg=_app_cfg(),
        cross_run_dir=cross_run_dir, terminal=False, override=True,
    )

    assert result.overall == "ok"
    rec = result.per_alias[0]
    assert rec.status == "committed"
    assert rec.release_override is not None
    assert rec.release_override["original_verdict"] == "REJECTED"
    assert rec.release_override["effective_verdict"] == "APPROVED_FOR_DELIVERY"
    # The persisted child final_acceptance verdict is NOT rewritten.
    assert child["phases"]["final_acceptance"]["verdict"] == "REJECTED"


def test_rejected_child_without_override_does_not_deliver(
    tmp_path: Path,
) -> None:
    """No override + child not APPROVED → mono gate blocks delivery
    (not_applicable, failure-like)."""
    cross_run_dir = tmp_path / "run"
    cross_run_dir.mkdir()
    repo, child = _make_alias(
        tmp_path, cross_run_dir, "api", verdict="REJECTED",
    )
    session = {"phases": {"projects": {"api": child}}}

    result = run_cross_delivery(
        session=session, projects={"api": repo}, app_cfg=_app_cfg(),
        cross_run_dir=cross_run_dir, terminal=False, override=False,
    )

    assert result.overall == "failed"
    assert result.per_alias[0].status == "not_applicable"
    assert _git_log_subjects(repo) == ["init"]


# ── alias-scoped run_dir + idempotent resume ──────────────────────────


def test_delivery_uses_alias_scoped_run_dir(tmp_path: Path) -> None:
    """Audit artifacts land under ``<cross_run_dir>/<alias>/commit_decisions/``,
    NOT the cross parent run_dir nor a new ``projects/`` layout."""
    cross_run_dir = tmp_path / "run"
    cross_run_dir.mkdir()
    repo, child = _make_alias(tmp_path, cross_run_dir, "api")
    session = {"phases": {"projects": {"api": child}}}

    run_cross_delivery(
        session=session, projects={"api": repo}, app_cfg=_app_cfg(),
        cross_run_dir=cross_run_dir, terminal=False,
    )

    decisions = list((cross_run_dir / "api" / "commit_decisions").glob("*.json"))
    assert decisions, "per-alias decision artifact must land under <run>/<alias>/"
    assert not (cross_run_dir / "projects").exists()


def test_delivery_resume_skips_already_committed_aliases(
    tmp_path: Path,
) -> None:
    """A cross_ckpt with delivery_status marking an alias already
    delivered makes the resume skip it (idempotent, no re-commit)."""
    cross_run_dir = tmp_path / "run"
    cross_run_dir.mkdir()
    api_repo, api_child = _make_alias(tmp_path, cross_run_dir, "api")
    web_repo, web_child = _make_alias(tmp_path, cross_run_dir, "web")
    session = {"phases": {"projects": {"api": api_child, "web": web_child}}}
    cross_ckpt = {
        "delivery_status": {
            "api": {"status": "committed", "commit_sha": "deadbeef"},
        },
    }

    result = run_cross_delivery(
        session=session, projects={"api": api_repo, "web": web_repo},
        app_cfg=_app_cfg(), cross_run_dir=cross_run_dir, terminal=False,
        cross_ckpt=cross_ckpt,
    )

    assert result.overall == "ok"
    by_alias = {r.alias: r for r in result.per_alias}
    assert by_alias["api"].status == "skipped_already_delivered"
    # api was NOT re-committed (still just init).
    assert _git_log_subjects(api_repo) == ["init"]
    # web delivered fresh and the checkpoint now records it.
    assert by_alias["web"].status == "committed"
    assert cross_ckpt["delivery_status"]["web"]["status"] == "committed"


# ── events + finalizer status mapping ─────────────────────────────────


def test_cross_delivery_events_precede_run_end(tmp_path: Path) -> None:
    """``cross.delivery.*`` events fire strictly before ``run.end``
    (invariant 4) — delivery runs in app.py before finalize."""
    from pipeline.cross_project.finalization import (
        CrossFinalizationContext,
        finalize_cross_run,
    )

    cross_run_dir = tmp_path / "run"
    cross_run_dir.mkdir()
    repo, child = _make_alias(tmp_path, cross_run_dir, "api")
    session = {"phases": {"projects": {"api": child}}}

    emitted: list[str] = []

    def _capture(kind: str, **_fields):
        emitted.append(kind)

    with (
        patch("core.observability.events.emit", side_effect=_capture),
        patch("pipeline.engine.artifact_mirror.mirror_to_projects",
              return_value=[]),
    ):
        delivery = run_cross_delivery(
            session=session, projects={"api": repo}, app_cfg=_app_cfg(),
            cross_run_dir=cross_run_dir, terminal=False,
        )
        ctx = CrossFinalizationContext(
            run_dir=cross_run_dir, output_dir=False, session=session,
            projects={"api": repo}, max_rounds=1,
            cfa_result=SimpleNamespace(
                parsed=SimpleNamespace(approved=True), source="agent",
            ),
            contract_results={}, contract_check_failed=False,
            contract_check_failure_reason=None, cross_phase_usage={},
            delivery_result=delivery,
        )
        finalize_cross_run(ctx)

    assert "cross.delivery.started" in emitted
    assert "cross.delivery.completed" in emitted
    assert "run.end" in emitted
    assert emitted.index("cross.delivery.completed") < emitted.index("run.end")


def _finalize_with_delivery(tmp_path: Path, overall: str):
    from pipeline.cross_project.cross_delivery import CrossDeliveryResult
    from pipeline.cross_project.finalization import (
        CrossFinalizationContext,
        finalize_cross_run,
    )

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = {"phases": {}}
    ctx = CrossFinalizationContext(
        run_dir=run_dir, output_dir=False, session=session,
        projects={"api": tmp_path / "api"}, max_rounds=1,
        cfa_result=SimpleNamespace(
            parsed=SimpleNamespace(approved=True), source="agent",
        ),
        contract_results={}, contract_check_failed=False,
        contract_check_failure_reason=None, cross_phase_usage={},
        delivery_result=CrossDeliveryResult(overall=overall),
    )
    with (
        patch("core.observability.events.emit"),
        patch("pipeline.engine.artifact_mirror.mirror_to_projects",
              return_value=[]),
    ):
        return finalize_cross_run(ctx)


def test_finalizer_maps_ok_to_done(tmp_path: Path) -> None:
    result = _finalize_with_delivery(tmp_path, "ok")
    assert result.status == "done"
    assert result.halt_reason is None


def test_finalizer_maps_partial_to_cross_delivery_partial(tmp_path: Path) -> None:
    result = _finalize_with_delivery(tmp_path, "partial")
    assert result.status == "failed"
    assert result.halt_reason == "cross_delivery_partial"


def test_finalizer_maps_failed_to_cross_delivery_failed(tmp_path: Path) -> None:
    result = _finalize_with_delivery(tmp_path, "failed")
    assert result.status == "failed"
    assert result.halt_reason == "cross_delivery_failed"


def test_finalizer_maps_halted(tmp_path: Path) -> None:
    result = _finalize_with_delivery(tmp_path, "halted")
    assert result.status == "halted"
    assert result.halt_reason == "phase_handoff_halt"


def test_finalizer_maps_disabled_to_done(tmp_path: Path) -> None:
    result = _finalize_with_delivery(tmp_path, "disabled")
    assert result.status == "done"
    assert result.halt_reason is None
