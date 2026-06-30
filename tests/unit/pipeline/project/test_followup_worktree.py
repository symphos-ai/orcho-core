"""Follow-up worktree continuity is diff- and plan-aware.

A follow-up run continues its parent's change session. Classification is
three-way, evaluated in order:

1. **Undelivered diff** — reuse the parent physical worktree only when
   that *isolated* worktree actually carries an undelivered diff (``git
   status --porcelain`` non-empty). When the parent's only undelivered
   diff survives as a ``diff.patch`` artifact — the physical worktree
   being clean, absent, or non-isolated — the run blocks *before* any
   write phase rather than silently starting on a clean HEAD and dropping
   the change.
2. **Plan-artifact continuation** — no undelivered diff, but the parent
   produced a durable plan artifact -> a *fresh* worktree is built from
   that plan (``diff_source='plan_artifact'``).
3. **Nothing to continue** — no undelivered diff and no plan artifact ->
   the run blocks with an operator message naming what is missing.

A ``worktree_isolation=off`` parent ran in-place on the shared source
checkout, so its dirtiness is NOT an undelivered isolated diff and must
not trigger reuse or an artifact block.

These tests pin the classification directly (``classify_followup_worktree``)
and through the pipeline, asserting the decision persisted into the
session worktree block (``worktree.followup_continuity``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agents.runtimes import MockAgentProvider
from core.io.git_helpers import has_uncommitted
from pipeline.engine.worktree import WorktreeConfigError
from pipeline.project.app import run_project_pipeline
from pipeline.project.followup_worktree import (
    FollowupPlanContinuationError,
    classify_followup_worktree,
    resolve_followup_plan_promotion,
)
from pipeline.project.types import PresentationPolicy, ProjectRunRequest


def _init_repo(path: Path) -> None:
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
    (path / "app.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _request(
    *,
    task: str,
    project: Path,
    run_dir: Path,
    profile_name: str = "task",
    followup_parent_run_id: str | None = None,
    followup_parent_run_dir: Path | None = None,
) -> ProjectRunRequest:
    return ProjectRunRequest(
        task=task,
        project_dir=str(project),
        output_dir=run_dir,
        profile_name=profile_name,
        provider=MockAgentProvider(latency=0.0),
        presentation=PresentationPolicy.SILENT,
        no_interactive=True,
        resume_mode="followup" if followup_parent_run_id else None,
        followup_parent_run_id=followup_parent_run_id,
        followup_parent_run_dir=(
            str(followup_parent_run_dir)
            if followup_parent_run_dir is not None
            else None
        ),
    )


def _write_parent_meta(parent_run_dir: Path, worktree: dict | None) -> None:
    """Persist a parent ``meta.json`` (optionally carrying a worktree block)."""
    parent_run_dir.mkdir(parents=True, exist_ok=True)
    meta: dict = {"id": parent_run_dir.name}
    if worktree is not None:
        meta["worktree"] = worktree
    (parent_run_dir / "meta.json").write_text(
        json.dumps(meta) + "\n", encoding="utf-8",
    )


def test_followup_run_reuses_parent_physical_worktree(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    parent_run_dir = runs_dir / "20260526_parent"
    child_run_dir = runs_dir / "20260526_child"

    monkeypatch.setenv("ORCHO_RUN_ID", "20260526_parent")
    parent = run_project_pipeline(
        _request(
            task="soft delete users",
            project=project,
            run_dir=parent_run_dir,
            profile_name="feature",
        ),
    ).session

    # The parent run finished its lifecycle, so finalization ran
    # teardown_worktree(retain=True) and the registered isolated worktree
    # must still be on disk before we deposit the dirty marker the reuse
    # path depends on. Confirm the retain invariant deterministically here
    # rather than discovering a missing worktree as a confusing write error.
    parent_worktree_path = Path(parent["worktree"]["path"])
    assert parent["status"] == "done", (
        "parent run must reach a finalized 'done' state so finalization runs "
        "teardown_worktree(retain=True) and retains the worktree"
    )
    assert parent_worktree_path.exists(), (
        f"parent worktree {parent_worktree_path} must be retained on disk "
        "(finalization retain=True) before writing the dirty marker"
    )
    assert (parent_worktree_path / ".git").is_file(), (
        f"parent worktree {parent_worktree_path} must be a registered "
        "isolated checkout with a .git gitlink file (retain=True)"
    )
    assert parent_worktree_path != Path(str(project)), (
        "parent worktree must be an isolated per-run checkout, not the "
        "source project tree"
    )

    # Guarantee an undelivered diff in the parent physical worktree so the
    # follow-up reuse path is exercised deterministically (not relying on
    # whatever the mock provider happened to leave behind).
    (parent_worktree_path / "undelivered.txt").write_text(
        "uncommitted follow-up continuity marker\n", encoding="utf-8",
    )
    assert has_uncommitted(str(parent_worktree_path))

    monkeypatch.setenv("ORCHO_RUN_ID", "20260526_child")
    child = run_project_pipeline(
        _request(
            task="add hard delete users",
            project=project,
            run_dir=child_run_dir,
            profile_name="feature",
            followup_parent_run_id="20260526_parent",
            followup_parent_run_dir=parent_run_dir,
        ),
    ).session

    parent_worktree = parent["worktree"]
    child_worktree = child["worktree"]
    assert child_worktree["path"] == parent_worktree["path"]
    assert child_worktree["worktree_id"] == parent_worktree["worktree_id"]
    assert child_worktree["root_run_id"] == "20260526_parent"
    assert not (child_run_dir / "checkout").exists()
    # The reused worktree really carries the uncommitted change.
    assert (parent_worktree_path / "undelivered.txt").exists()

    manifest = json.loads(
        Path(parent_worktree["manifest_path"]).read_text(encoding="utf-8"),
    )
    assert manifest["attached_run_ids"] == [
        "20260526_parent",
        "20260526_child",
    ]

    # Persisted classification: dirty parent worktree -> reuse.
    continuity = child_worktree["followup_continuity"]
    assert continuity["diff_source"] == "worktree"
    assert continuity["blocked"] is False
    assert continuity["mode_label"] == f"reused parent {child_worktree['path']}"


def test_followup_reuses_dirty_parent_worktree_one_line_diff(
    tmp_path: Path, monkeypatch,
) -> None:
    """A single uncommitted line in the parent worktree triggers reuse."""
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    parent_run_dir = runs_dir / "20260527_parent"
    child_run_dir = runs_dir / "20260527_child"

    monkeypatch.setenv("ORCHO_RUN_ID", "20260527_parent")
    parent = run_project_pipeline(
        _request(
            task="phase one",
            project=project,
            run_dir=parent_run_dir,
            profile_name="feature",
        ),
    ).session

    parent_worktree_path = Path(parent["worktree"]["path"])
    # Modify a tracked file by one line so the porcelain status is non-empty
    # purely from an in-tree edit.
    (parent_worktree_path / "app.txt").write_text(
        "base\nfollow-up edit\n", encoding="utf-8",
    )
    assert has_uncommitted(str(parent_worktree_path))

    monkeypatch.setenv("ORCHO_RUN_ID", "20260527_child")
    child = run_project_pipeline(
        _request(
            task="phase two",
            project=project,
            run_dir=child_run_dir,
            profile_name="feature",
            followup_parent_run_id="20260527_parent",
            followup_parent_run_dir=parent_run_dir,
        ),
    ).session

    assert child["worktree"]["path"] == parent["worktree"]["path"]
    assert "follow-up edit" in (parent_worktree_path / "app.txt").read_text(
        encoding="utf-8",
    )
    continuity = child["worktree"]["followup_continuity"]
    assert continuity["diff_source"] == "worktree"
    assert continuity["mode_label"] == (
        f"reused parent {child['worktree']['path']}"
    )


def test_followup_artifact_only_diff_blocks_before_write(
    tmp_path: Path, monkeypatch,
) -> None:
    """Undelivered diff present only as diff.patch -> block, not clean HEAD."""
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    parent_run_dir = runs_dir / "20260528_parent"
    child_run_dir = runs_dir / "20260528_child"
    # Parent worktree is clean/absent (no worktree metadata), but an
    # undelivered diff survives only as a non-empty diff.patch artifact.
    _write_parent_meta(parent_run_dir, worktree=None)
    (parent_run_dir / "diff.patch").write_text(
        "diff --git a/app.txt b/app.txt\n"
        "--- a/app.txt\n+++ b/app.txt\n"
        "@@ -1 +1,2 @@\n base\n+artifact-only change\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("ORCHO_RUN_ID", "20260528_child")
    with pytest.raises(WorktreeConfigError, match="diff.patch artifact"):
        run_project_pipeline(
            _request(
                task="follow up on artifact diff",
                project=project,
                run_dir=child_run_dir,
                followup_parent_run_id="20260528_parent",
                followup_parent_run_dir=parent_run_dir,
            ),
        )

    # Blocked strictly before any write phase: no child checkout materialised
    # and the source project tree is untouched.
    assert not (child_run_dir / "checkout").exists()
    assert not has_uncommitted(str(project))

    # The blocked classification is persisted to the child meta.json even
    # though the run stopped before a worktree resolved.
    child_meta = json.loads(
        (child_run_dir / "meta.json").read_text(encoding="utf-8"),
    )
    continuity = child_meta["worktree"]["followup_continuity"]
    assert continuity["blocked"] is True
    assert continuity["diff_source"] == "artifact"
    assert continuity["mode_label"] == "blocked: parent diff/worktree unavailable"
    assert "diff.patch" in continuity["reason"]


def test_followup_blocks_when_parent_has_no_diff_or_plan(
    tmp_path: Path, monkeypatch,
) -> None:
    """No undelivered diff and no plan artifact -> block, not clean HEAD.

    With no plan signal wired through the pipeline (the parent meta carries
    no ``plan_source``), a follow-up from a parent that left neither an
    undelivered diff nor a plan artifact has nothing to continue from and
    blocks with an operator message rather than silently starting fresh.
    """
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    parent_run_dir = runs_dir / "20260529_parent"
    child_run_dir = runs_dir / "20260529_child"
    # Parent has neither a dirty worktree, a diff.patch artifact, nor a plan.
    _write_parent_meta(parent_run_dir, worktree=None)

    monkeypatch.setenv("ORCHO_RUN_ID", "20260529_child")
    with pytest.raises(WorktreeConfigError, match="nothing to continue"):
        run_project_pipeline(
            _request(
                task="follow up on a clean parent",
                project=project,
                run_dir=child_run_dir,
                followup_parent_run_id="20260529_parent",
                followup_parent_run_dir=parent_run_dir,
            ),
        )

    # Blocked strictly before any write phase: no child checkout materialised.
    assert not (child_run_dir / "checkout").exists()
    child_meta = json.loads(
        (child_run_dir / "meta.json").read_text(encoding="utf-8"),
    )
    continuity = child_meta["worktree"]["followup_continuity"]
    assert continuity["blocked"] is True
    assert continuity["diff_source"] == "none"
    assert continuity["mode_label"] == (
        "blocked: no parent diff or plan to continue"
    )


def test_followup_without_parent_worktree_metadata_is_rejected(
    tmp_path: Path, monkeypatch,
) -> None:
    """Rejection now requires an undelivered diff.

    Without parent worktree metadata the run previously always failed.
    Continuity is now diff-aware: the run only blocks when an undelivered
    diff exists. Here a non-empty ``diff.patch`` keeps the block in force
    (artifact-only diff cannot be transferred to a clean HEAD this run).
    """
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    parent_run_dir = runs_dir / "20260526_parent"
    child_run_dir = runs_dir / "20260526_child"
    _write_parent_meta(parent_run_dir, worktree=None)
    (parent_run_dir / "diff.patch").write_text(
        "diff --git a/app.txt b/app.txt\n"
        "--- a/app.txt\n+++ b/app.txt\n"
        "@@ -1 +1,2 @@\n base\n+undelivered\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("ORCHO_RUN_ID", "20260526_child")
    with pytest.raises(WorktreeConfigError, match="parent worktree"):
        run_project_pipeline(
            _request(
                task="add hard delete users",
                project=project,
                run_dir=child_run_dir,
                followup_parent_run_id="20260526_parent",
                followup_parent_run_dir=parent_run_dir,
            ),
        )


# ── Focused classify_followup_worktree unit tests ──────────────────────────
# These pin the three-way classification directly, without driving the whole
# pipeline, so the plan-artifact branch (only wired into the pipeline by a
# later subtask) is still covered here via the explicit plan signal.


def test_classify_plan_artifact_continuation_when_no_diff(tmp_path: Path) -> None:
    """Persisted plan + no worktree metadata + no diff -> plan continuation."""
    parent_run_dir = tmp_path / "parent"
    parent_run_dir.mkdir()

    decision = classify_followup_worktree(
        parent_run_dir=parent_run_dir,
        followup_parent_worktree=None,
        parent_has_persisted_plan=True,
    )

    assert decision.blocked is False
    assert decision.effective_parent_worktree is None
    assert decision.diff_source == "plan_artifact"
    assert "plan artifact" in decision.mode_label


def test_classify_reuses_dirty_isolated_parent_worktree(tmp_path: Path) -> None:
    """A dirty *isolated* parent worktree is reused (diff_source='worktree')."""
    project = tmp_path / "api"
    _init_repo(project)
    # One uncommitted in-tree edit makes the worktree dirty.
    (project / "app.txt").write_text("base\nedit\n", encoding="utf-8")
    assert has_uncommitted(str(project))
    parent_worktree = {"path": str(project), "isolation": "per_run"}

    decision = classify_followup_worktree(
        parent_run_dir=None,
        followup_parent_worktree=parent_worktree,
    )

    assert decision.diff_source == "worktree"
    assert decision.blocked is False
    assert decision.effective_parent_worktree == parent_worktree
    assert decision.mode_label == f"reused parent {project}"


def test_classify_artifact_only_diff_blocks(tmp_path: Path) -> None:
    """A diff.patch artifact with no dirty worktree -> artifact block."""
    parent_run_dir = tmp_path / "parent"
    parent_run_dir.mkdir()
    (parent_run_dir / "diff.patch").write_text(
        "diff --git a/app.txt b/app.txt\n"
        "--- a/app.txt\n+++ b/app.txt\n"
        "@@ -1 +1,2 @@\n base\n+artifact-only change\n",
        encoding="utf-8",
    )

    decision = classify_followup_worktree(
        parent_run_dir=parent_run_dir,
        followup_parent_worktree=None,
        # Even with a plan, an undelivered artifact diff takes precedence and
        # blocks (strict continuity is evaluated first).
        parent_has_persisted_plan=True,
    )

    assert decision.diff_source == "artifact"
    assert decision.blocked is True
    assert "diff.patch" in (decision.block_message or "")


def test_classify_off_isolation_dirty_parent_with_plan_is_plan_artifact(
    tmp_path: Path,
) -> None:
    """worktree_isolation=off + dirty source checkout + plan -> plan continuation.

    The off-isolation parent ran in-place on the shared source checkout, so
    its dirtiness must NOT be read as an undelivered diff (no reuse, no
    artifact block); the persisted plan drives a fresh worktree instead.
    """
    project = tmp_path / "api"
    _init_repo(project)
    (project / "app.txt").write_text("base\ndirty source\n", encoding="utf-8")
    assert has_uncommitted(str(project))

    decision = classify_followup_worktree(
        parent_run_dir=tmp_path / "parent",
        followup_parent_worktree={"path": str(project), "isolation": "off"},
        parent_has_persisted_plan=True,
    )

    assert decision.diff_source == "plan_artifact"
    assert decision.blocked is False
    assert decision.effective_parent_worktree is None


def test_classify_off_isolation_dirty_parent_without_plan_blocks(
    tmp_path: Path,
) -> None:
    """Off-isolation dirty parent with no plan blocks — never reuse/artifact."""
    project = tmp_path / "api"
    _init_repo(project)
    (project / "app.txt").write_text("base\ndirty source\n", encoding="utf-8")
    assert has_uncommitted(str(project))

    decision = classify_followup_worktree(
        parent_run_dir=tmp_path / "parent",
        followup_parent_worktree={"path": str(project), "isolation": "off"},
        parent_has_persisted_plan=False,
    )

    assert decision.diff_source == "none"
    assert decision.blocked is True
    assert decision.effective_parent_worktree is None


def test_classify_blocks_when_no_diff_or_plan(tmp_path: Path) -> None:
    """No diff and no plan -> block with a clear, legacy-free message."""
    parent_run_dir = tmp_path / "parent"
    parent_run_dir.mkdir()

    decision = classify_followup_worktree(
        parent_run_dir=parent_run_dir,
        followup_parent_worktree=None,
        parent_has_persisted_plan=False,
    )

    assert decision.blocked is True
    assert decision.diff_source == "none"
    message = (decision.block_message or "").lower()
    assert "nothing to continue" in message
    # No legacy profile names leak into operator diagnostics.
    for legacy in ("advanced", "lite", "enterprise"):
        assert legacy not in message


# ── F2: off-isolation discrimination via source checkout ────────────────────


def test_classify_path_equals_source_checkout_not_isolated_even_if_marked(
    tmp_path: Path,
) -> None:
    """A worktree whose path IS the source checkout is NOT isolated, even when
    the meta wrongly claims ``isolation='per_run'`` — its dirtiness is the
    shared checkout's, not an undelivered isolated diff."""
    project = tmp_path / "api"
    _init_repo(project)
    (project / "app.txt").write_text("base\ndirty source\n", encoding="utf-8")
    assert has_uncommitted(str(project))

    decision = classify_followup_worktree(
        parent_run_dir=tmp_path / "parent",
        # path == the shared source checkout despite the isolated marker.
        followup_parent_worktree={"path": str(project), "isolation": "per_run"},
        parent_has_persisted_plan=True,
        source_checkout=project,
    )

    # Not reused as a dirty isolated worktree — routed to plan-artifact.
    assert decision.diff_source == "plan_artifact"
    assert decision.blocked is False
    assert decision.effective_parent_worktree is None


def test_classify_incomplete_meta_path_only_not_isolated(tmp_path: Path) -> None:
    """A stale/incomplete ``meta.worktree`` with a path but no ``isolation`` key,
    pointing at the dirty source checkout, must NOT be read as an isolated diff:
    with a plan it is a plan-artifact continuation, without one it blocks."""
    project = tmp_path / "api"
    _init_repo(project)
    (project / "app.txt").write_text("base\ndirty source\n", encoding="utf-8")
    assert has_uncommitted(str(project))

    with_plan = classify_followup_worktree(
        parent_run_dir=tmp_path / "parent",
        followup_parent_worktree={"path": str(project)},  # no isolation key
        parent_has_persisted_plan=True,
        source_checkout=project,
    )
    assert with_plan.diff_source == "plan_artifact"
    assert with_plan.blocked is False

    without_plan = classify_followup_worktree(
        parent_run_dir=tmp_path / "parent",
        followup_parent_worktree={"path": str(project)},
        parent_has_persisted_plan=False,
        source_checkout=project,
    )
    # Never reused / artifact-blocked as an undelivered diff.
    assert without_plan.diff_source == "none"
    assert without_plan.blocked is True


# ── F1: contradictory child profile must not slip through as continuation ────


def _seed_plan_only_parent(parent_dir: Path) -> None:
    """Write a parent run dir that is a plan-artifact continuation candidate:
    a persisted plan, an off-isolation worktree block, and no undelivered diff."""
    parent_dir.mkdir(parents=True, exist_ok=True)
    (parent_dir / "parsed_plan.json").write_text("{\"x\": 1}", encoding="utf-8")
    (parent_dir / "meta.json").write_text(
        json.dumps({"id": parent_dir.name, "worktree": {"isolation": "off"}})
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "profile_name", ["planning", "research", "code_review", "delivery_audit"],
)
def test_promotion_blocks_contradictory_child_profile(
    tmp_path: Path, profile_name: str,
) -> None:
    """A plan-only parent + a plan-only or review-only child profile must be
    blocked loudly at the promotion chokepoint, never silently allowed to run
    as a false plan-artifact continuation."""
    parent_dir = tmp_path / "parent"
    _seed_plan_only_parent(parent_dir)

    with pytest.raises(FollowupPlanContinuationError) as exc:
        resolve_followup_plan_promotion(
            resume_mode="followup",
            explicit_from_run_plan_parent_dir=None,
            followup_parent_run_dir=str(parent_dir),
            profile_name=profile_name,
            project_dir=str(tmp_path / "api"),
        )
    msg = str(exc.value).lower()
    assert profile_name in msg
    # The remediation names semantic profiles, never legacy ones.
    assert "feature" in msg
    for legacy in ("advanced", "lite", "enterprise"):
        assert legacy not in msg


def test_promotion_promotes_implementation_child_profile(tmp_path: Path) -> None:
    """Sanity: a promotable child profile (feature) still promotes."""
    parent_dir = tmp_path / "parent"
    _seed_plan_only_parent(parent_dir)

    promoted = resolve_followup_plan_promotion(
        resume_mode="followup",
        explicit_from_run_plan_parent_dir=None,
        followup_parent_run_dir=str(parent_dir),
        profile_name="feature",
        project_dir=str(tmp_path / "api"),
    )
    assert promoted == parent_dir


def test_promotion_does_not_block_contradictory_profile_with_undelivered_diff(
    tmp_path: Path,
) -> None:
    """A contradictory child profile is only blocked on the plan-only path. When
    the parent carries an undelivered diff (artifact), promotion returns None and
    strict diff continuity (not the contradiction guard) governs the run."""
    parent_dir = tmp_path / "parent"
    _seed_plan_only_parent(parent_dir)
    # Add an undelivered artifact diff on top of the plan.
    (parent_dir / "diff.patch").write_text(
        "diff --git a/app.txt b/app.txt\n@@ -1 +1,2 @@\n base\n+x\n",
        encoding="utf-8",
    )

    promoted = resolve_followup_plan_promotion(
        resume_mode="followup",
        explicit_from_run_plan_parent_dir=None,
        followup_parent_run_dir=str(parent_dir),
        profile_name="code_review",
        project_dir=str(tmp_path / "api"),
    )
    assert promoted is None
