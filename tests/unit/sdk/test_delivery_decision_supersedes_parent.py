"""An out-of-band delivery of a correction child closes its parent.

A live correction child supersedes its ``fix``-parked / rejected-FA parent in
its own finalization. A child parked on a deferred delivery gate finalizes
with ``status='pending'`` — nothing to supersede yet — and its delivery lands
later through ``decide_delivery``. Until now that path settled only the child,
so the parent kept reading ``halted / commit_decision_fix`` and its diagnosis
said ``blocked_worktree`` / ``start_followup`` instead of ``closed_by_followup``
(dogfood parent ``20260908_131908_4064f0``).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.io.git_helpers import create_worktree
from pipeline.engine.commit_delivery import resolve_commit_delivery
from sdk.run_control.delivery import decide_delivery
from sdk.run_control.diagnosis import run_diagnosis

pytestmark = [pytest.mark.sdk, pytest.mark.git_worktree]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@orcho.invalid")
    _git(repo, "config", "user.name", "Orcho Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return _git(repo, "rev-parse", "HEAD")


def _lineage(tmp_path: Path, *, correction_child: bool = True) -> tuple[Path, Path]:
    """A ``fix``-parked parent and its correction child parked on delivery.

    Returns ``(runs_dir, repo)``. The parent meta is the shape the engine
    leaves after ``orcho_delivery_decide fix``: ``halted`` /
    ``commit_decision_fix`` with a ``fix_requested`` gate.
    """
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    runs = tmp_path / "runs"
    parent_dir = runs / "parent"
    child_dir = runs / "child"
    parent_dir.mkdir(parents=True)
    child_dir.mkdir(parents=True)
    result = create_worktree(
        repo=repo, base_ref=head, target_path=parent_dir / "checkout",
        branch_name="orcho/run/parent",
    )
    assert result.ok, result.error
    wt = parent_dir / "checkout"
    (wt / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    (parent_dir / "meta.json").write_text(json.dumps({
        "status": "halted",
        "halt_reason": "commit_decision_fix",
        "halted_at": "2026-09-08T12:01:00+00:00",
        "project": str(repo),
        "profile": "feature",
        "phases": {"final_acceptance": {"verdict": "REJECTED", "short_summary": "C6 pending"}},
        "commit_delivery": {
            "action": "fix", "status": "fix_requested", "run_id": "parent",
            "decision_id": "parent", "project_path": str(repo), "source_path": str(wt),
            "baseline_ref": head, "dirty": True, "include_untracked": True,
            "release_verdict": "REJECTED", "pr_url": None,
        },
        "worktree": {"path": str(wt), "kind": "primary", "root_run_id": "parent"},
    }), encoding="utf-8")

    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=wt,
        run_dir=child_dir,
        run_id="child",
        session={"status": "done", "phases": {"final_acceptance": {
            "verdict": "APPROVED", "short_summary": "ok"}}},
        commit_config={"enabled": True, "add_untracked": True,
                       "branch_policy": "bypass", "publish": "off"},
        no_interactive=True,
        decision_mode="defer",
    )
    assert decision.status == "pending" and decision.action == "none"
    child_meta = {
        "status": "halted",
        "halt_reason": "commit_delivery_pending",
        "project": str(repo),
        "parent_run_id": "parent",
        "parent_run_dir": str(parent_dir),
        "commit_delivery": decision.to_dict(),
    }
    if correction_child:
        child_meta.update({"profile": "correction", "resume_mode": "followup"})
        (child_dir / "correction_context.md").write_text("# Correction Context\n", encoding="utf-8")
    else:
        child_meta.update({"profile": "feature"})
    (child_dir / "meta.json").write_text(json.dumps(child_meta), encoding="utf-8")
    return runs, repo


def _parent(runs: Path) -> dict:
    return json.loads((runs / "parent" / "meta.json").read_text(encoding="utf-8"))


def test_out_of_band_approve_of_a_correction_child_closes_the_parent(tmp_path: Path) -> None:
    runs, _repo = _lineage(tmp_path)
    assert run_diagnosis("parent", runs_dir=runs, cwd=None).condition != "closed_by_followup"

    result = decide_delivery("child", "approve", runs_dir=runs, cwd=None)
    assert result.accepted and result.status == "committed", result
    # Where the commit lands (checkout vs delivery branch) is the replay
    # policy's concern, not this seam's: the child delivered either way.

    parent = _parent(runs)
    assert parent["status"] == "done"
    assert "halt_reason" not in parent
    assert "commit_delivery" not in parent
    marker = parent["superseded_by_followup"]
    assert marker["child_run_id"] == "child"
    assert marker["delivery_status"] == "committed"

    diagnosis = run_diagnosis("parent", runs_dir=runs, cwd=None)
    assert diagnosis.condition == "closed_by_followup"
    assert diagnosis.recommended_run_id == "child"
    assert diagnosis.recommended_next_action != "start_followup"


def test_skip_also_closes_the_parent(tmp_path: Path) -> None:
    runs, _repo = _lineage(tmp_path)
    result = decide_delivery("child", "skip", runs_dir=runs, cwd=None)
    assert result.accepted, result
    assert _parent(runs)["superseded_by_followup"]["delivery_status"] == "skipped"


def test_halt_leaves_the_parent_open(tmp_path: Path) -> None:
    runs, _repo = _lineage(tmp_path)
    result = decide_delivery("child", "halt", runs_dir=runs, cwd=None)
    assert result.accepted, result
    parent = _parent(runs)
    assert parent["status"] == "halted"
    assert "superseded_by_followup" not in parent


def test_non_correction_child_never_touches_the_parent(tmp_path: Path) -> None:
    runs, _repo = _lineage(tmp_path, correction_child=False)
    result = decide_delivery("child", "approve", runs_dir=runs, cwd=None)
    assert result.accepted, result
    parent = _parent(runs)
    assert parent["status"] == "halted"
    assert "superseded_by_followup" not in parent
