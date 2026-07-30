from __future__ import annotations

import ast
import inspect
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.io.git_helpers import GitOpResult
from pipeline.engine.workspace_cleanup import execute_workspace_cleanup, select_workspace_cleanup
from pipeline.engine.worktree import _compute_retention_timestamp

NOW = datetime(2026, 7, 29, tzinfo=UTC)


def _run(
    tmp_path: Path,
    run_id: str,
    *,
    status: str = "done",
    deadline: str = "2026-07-28T00:00:00Z",
    handoff: bool = False,
    path: Path | None = None,
    projects: dict | None = None,
) -> tuple[Path, Path]:
    runs = tmp_path / "runspace" / "runs"
    run_dir = runs / run_id
    checkout = path or tmp_path / "runspace" / "worktrees" / f"wt_{run_id}" / "checkout"
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / "retained.txt").write_text("retained checkout evidence\n")
    meta = {
        "status": status,
        "worktree": {
            "path": str(checkout),
            "source_repo_path": str(tmp_path / "repo"),
            "retention_until": deadline,
        },
    }
    if handoff:
        meta["phase_handoff"] = {"id": "pending"}
    if projects is not None:
        meta["projects"] = projects
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(json.dumps(meta))
    return runs, checkout


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(checkout: Path) -> None:
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "config", "user.email", "test@orcho.invalid")
    _git(checkout, "config", "user.name", "Orcho Test")


def _commit_all(checkout: Path, message: str) -> None:
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-q", "-m", message)


@pytest.mark.parametrize(
    ("status", "handoff", "deadline", "selected", "reason"),
    [
        ("done", False, "2026-07-28T00:00:00Z", True, "retention_expired"),
        ("halted", False, "2026-07-30T00:00:00Z", False, "retention_active"),
        ("running", False, "2026-07-28T00:00:00Z", False, "status_not_stopped"),
        ("done", True, "2026-07-28T00:00:00Z", True, "retention_expired"),
        ("awaiting_phase_handoff", True, "2026-07-29T00:00:00Z", True, "pause_retention_expired"),
        ("awaiting_phase_handoff", True, "2026-07-29T00:00:01Z", False, "active_handoff_or_gate"),
    ],
)
def test_status_pause_and_deadline_matrix(tmp_path, status, handoff, deadline, selected, reason):
    runs, _ = _run(tmp_path, "one", status=status, handoff=handoff, deadline=deadline)
    plan = select_workspace_cleanup(runs, now=NOW)
    verdict = (*plan.selected, *plan.protected)[0]
    assert verdict.selected is selected
    assert verdict.reason == reason


def test_uncommitted_changes_protect_a_checkout(tmp_path):
    runs, checkout = _run(tmp_path, "one")
    _init_repo(checkout)  # retained.txt stays untracked: work never committed
    plan = select_workspace_cleanup(runs, now=NOW)
    assert not plan.selected
    assert [verdict.reason for verdict in plan.protected] == ["uncommitted_changes"]


def test_unpushed_commits_protect_and_pushed_clean_checkout_is_reclaimable(tmp_path):
    runs, checkout = _run(tmp_path, "one")
    _init_repo(checkout)
    _commit_all(checkout, "first")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(checkout, "remote", "add", "origin", str(remote))
    _git(checkout, "push", "-q", "origin", "main")

    plan = select_workspace_cleanup(runs, now=NOW)
    assert [verdict.reason for verdict in plan.selected] == ["retention_expired"]

    (checkout / "local.txt").write_text("only here\n")
    _commit_all(checkout, "second")
    plan = select_workspace_cleanup(runs, now=NOW)
    assert not plan.selected
    assert [verdict.reason for verdict in plan.protected] == ["unpushed_commits"]


def test_reclaim_both_reclaims_clean_pushed_expired_pause_and_records_both_reasons(tmp_path):
    runs, checkout = _run(
        tmp_path,
        "paused",
        status="awaiting_phase_handoff",
        handoff=True,
        deadline="2026-07-28T00:00:00Z",
    )
    _init_repo(checkout)
    _commit_all(checkout, "published")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(checkout, "remote", "add", "origin", str(remote))
    _git(checkout, "push", "-q", "origin", "main")

    plan = select_workspace_cleanup(runs, now=NOW)
    assert [verdict.reason for verdict in plan.selected] == ["pause_retention_expired"]
    assert [verdict.reason for verdict in plan.root_selected] == ["pause_retention_expired"]
    assert not plan.root_protected

    execution = execute_workspace_cleanup(
        runs,
        tier="both",
        disposition="delete",
        now=NOW,
        registration_checker=lambda *_: False,
    )
    assert not (runs / "paused").exists()
    assert [row["reason"] for row in execution.receipt["selected"]] == [
        "pause_retention_expired"
    ]
    assert [row["reason"] for row in execution.receipt["root_selected"]] == [
        "pause_retention_expired"
    ]
    assert not execution.receipt["root_protected"]


def test_expired_pause_with_a_real_cross_gate_stays_protected(tmp_path):
    # A real cross gate is a live coordination point, not a stale pause: it
    # protects the checkout even past the retention deadline. Only a bare open
    # handoff expires with retention (covered above).
    runs, _ = _run(
        tmp_path,
        "gated",
        status="awaiting_phase_handoff",
        handoff=True,
        deadline="2026-07-28T00:00:00Z",
    )
    meta_path = runs / "gated" / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["pending_gate"] = {"id": "cross-gate-1"}
    meta_path.write_text(json.dumps(meta))

    plan = select_workspace_cleanup(runs, now=NOW)
    assert not plan.selected
    assert [verdict.reason for verdict in plan.protected] == ["active_handoff_or_gate"]
    assert not plan.root_selected


@pytest.mark.parametrize(("kind", "reason"), [("dirty", "uncommitted_changes"), ("unpushed", "unpushed_commits")])
def test_expired_pause_still_preserves_checkout_value(tmp_path, kind, reason):
    runs, checkout = _run(
        tmp_path,
        "pause",
        status="awaiting_phase_handoff",
        handoff=True,
        deadline="2026-07-28T00:00:00Z",
    )
    _init_repo(checkout)
    if kind == "unpushed":
        _commit_all(checkout, "local only")
    plan = select_workspace_cleanup(runs, now=NOW)
    assert [verdict.reason for verdict in plan.protected] == [reason]


def test_absent_stamp_uses_root_timestamp_cutoff_but_present_malformed_stamp_protects(tmp_path):
    runs, _ = _run(
        tmp_path, "20260530_000000_old", status="awaiting_phase_handoff", handoff=True
    )
    _, _ = _run(
        tmp_path, "20260725_000000_young", status="awaiting_phase_handoff", handoff=True
    )
    for run_id in ("20260530_000000_old", "20260725_000000_young"):
        meta_path = runs / run_id / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["worktree"].pop("retention_until")
        meta_path.write_text(json.dumps(meta))
    malformed_path = runs / "20260530_000000_old" / "meta.json"
    malformed = json.loads(malformed_path.read_text())
    malformed["worktree"]["retention_until"] = None
    malformed_path.write_text(json.dumps(malformed))

    plan = select_workspace_cleanup(runs, now=NOW, older_than=timedelta(days=7))
    assert {verdict.snapshot.run_id: verdict.reason for verdict in plan.protected} == {
        "20260530_000000_old": "retention_invalid",
        "20260725_000000_young": "active_handoff_or_gate",
    }

    malformed["worktree"].pop("retention_until")
    malformed_path.write_text(json.dumps(malformed))
    plan = select_workspace_cleanup(runs, now=NOW, older_than=timedelta(days=7))
    assert [verdict.reason for verdict in plan.selected] == ["pause_retention_expired"]


def test_reverification_preserves_expired_pause_predicate_and_rejects_young_pause(tmp_path):
    runs, _ = _run(
        tmp_path,
        "20260530_000000_old",
        status="awaiting_phase_handoff",
        handoff=True,
    )
    import pipeline.engine.workspace_cleanup as cleanup

    plan = cleanup.select_workspace_cleanup(runs, now=NOW, older_than=timedelta(days=7))
    assert cleanup._reverify_group(list(plan.selected), runs, NOW, timedelta(days=7)) is not None

    meta_path = runs / "20260530_000000_old" / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["worktree"]["retention_until"] = "2026-08-01T00:00:00Z"
    meta_path.write_text(json.dumps(meta))
    assert cleanup._reverify_group(list(plan.selected), runs, NOW, timedelta(days=7)) is None


def test_checkout_whose_repository_is_gone_is_reclaimable(tmp_path):
    runs, checkout = _run(tmp_path, "one")
    (checkout / ".git").write_text("gitdir: /nonexistent/repo/.git/worktrees/one\n")
    plan = select_workspace_cleanup(runs, now=NOW)
    assert [verdict.reason for verdict in plan.selected] == ["retention_expired"]


def test_shared_checkout_is_protected_when_one_reference_is_live(tmp_path):
    runs, checkout = _run(tmp_path, "one")
    _run(tmp_path, "two", status="running", path=checkout)
    plan = select_workspace_cleanup(runs, now=NOW)
    assert not plan.selected
    assert {v.reason for v in plan.protected} == {"shared_checkout_protected", "status_not_stopped"}


def test_unreadable_sibling_meta_does_not_block_an_unrelated_checkout(tmp_path):
    runs, _ = _run(tmp_path, "one")
    broken = runs / "two"
    broken.mkdir()
    (broken / "meta.json").write_text("{")

    plan = select_workspace_cleanup(runs, now=NOW)

    assert [verdict.reason for verdict in plan.selected] == ["retention_expired"]
    assert [verdict.reason for verdict in plan.protected] == ["meta_unreadable"]


def test_reverify_rejects_a_reference_that_went_live_after_selection(tmp_path):
    runs, _ = _run(tmp_path, "one")
    import pipeline.engine.workspace_cleanup as cleanup

    plan = cleanup.select_workspace_cleanup(runs, now=NOW)
    meta_path = runs / "one" / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["status"] = "running"
    meta_path.write_text(json.dumps(meta))

    assert cleanup._reverify_group(list(plan.selected), runs, NOW) is None


def test_reverify_confirms_an_unchanged_group(tmp_path):
    runs, checkout = _run(tmp_path, "one")
    import pipeline.engine.workspace_cleanup as cleanup

    plan = cleanup.select_workspace_cleanup(runs, now=NOW)
    fresh = cleanup._reverify_group(list(plan.selected), runs, NOW)

    assert fresh is not None
    assert [verdict.snapshot.worktree_path for verdict in fresh] == [checkout]


def test_manifest_files_do_not_gate_reclamation(tmp_path):
    """Manifests prove identity, not value; their absence blocks nothing."""
    runs, checkout = _run(tmp_path, "one")
    manifest = checkout.parent / "manifest.json"
    manifest.write_text(json.dumps({"checkout_path": str(checkout), "attached_run_ids": ["one"]}))
    manifest.unlink()

    plan = select_workspace_cleanup(runs, now=NOW)

    assert [verdict.reason for verdict in plan.selected] == ["retention_expired"]
    assert not plan.protected


def test_writer_retention_timestamp_is_accepted_by_selection(tmp_path):
    deadline = _compute_retention_timestamp(retention_days=1)
    assert deadline is not None and deadline.endswith("+00:00")
    parsed = datetime.fromisoformat(deadline)
    runs, _ = _run(tmp_path, "one", deadline=deadline)
    plan = select_workspace_cleanup(runs, now=parsed + timedelta(seconds=1))
    assert [verdict.reason for verdict in plan.selected] == ["retention_expired"]


def test_cross_root_requires_every_declared_child(tmp_path):
    runs, _ = _run(tmp_path, "cross", projects={"a": "/a", "b": "/b"})
    _run(tmp_path, "ignored")  # A separate mono run must not affect cross grouping.
    parent = runs / "cross"
    for alias, status in (("a", "done"), ("b", "running")):
        checkout = tmp_path / "runspace" / "worktrees" / f"wt_{alias}" / "checkout"
        checkout.mkdir(parents=True)
        (parent / alias).mkdir()
        (parent / alias / "meta.json").write_text(
            json.dumps(
                {
                    "status": status,
                    "worktree": {
                        "path": str(checkout),
                        "source_repo_path": str(tmp_path / "repo"),
                        "retention_until": "2026-07-28T00:00:00Z",
                    },
                }
            )
        )
    plan = select_workspace_cleanup(runs, now=NOW)
    assert "cross" not in plan.root_run_ids_for_both


@pytest.mark.parametrize(
    ("status", "handoff", "deadline", "reason", "root_selected"),
    [
        ("running", False, "2026-07-28T00:00:00Z", "parent_not_stopped", False),
        ("done", True, "2026-07-28T00:00:00Z", "retention_expired", True),
        (
            "awaiting_phase_handoff",
            True,
            "2026-07-28T00:00:00Z",
            "retention_expired",
            True,
        ),
        (
            "awaiting_phase_handoff",
            True,
            "2026-08-01T00:00:00Z",
            "parent_active_handoff_or_gate",
            False,
        ),
    ],
)
def test_cross_parent_retention_applies_to_all_children(
    tmp_path, status, handoff, deadline, reason, root_selected
):
    runs, _ = _run(
        tmp_path,
        "cross",
        status=status,
        handoff=handoff,
        deadline=deadline,
        projects={"a": "/a", "b": "/b"},
    )
    parent = runs / "cross"
    for alias in ("a", "b"):
        checkout = tmp_path / "runspace" / "worktrees" / f"wt_{alias}" / "checkout"
        checkout.mkdir(parents=True)
        (parent / alias).mkdir()
        (parent / alias / "meta.json").write_text(
            json.dumps(
                {
                    "status": "done",
                    "worktree": {
                        "path": str(checkout),
                        "source_repo_path": str(tmp_path / "repo"),
                        "retention_until": "2026-07-28T00:00:00Z",
                    },
                }
            )
        )
    plan = select_workspace_cleanup(runs, now=NOW)
    child_verdicts = [
        verdict
        for verdict in (*plan.selected, *plan.protected)
        if verdict.snapshot.run_id in {"a", "b"}
    ]
    assert child_verdicts and {verdict.reason for verdict in child_verdicts} == {reason}
    assert ("cross" in plan.root_run_ids_for_both) is root_selected


def test_symlink_escape_and_unreadable_meta_are_protected(tmp_path):
    runs, _ = _run(tmp_path, "plain")
    (runs / "broken").mkdir()
    (runs / "broken" / "meta.json").write_text("{")
    outside = tmp_path / "outside"
    outside.mkdir()
    escaped = tmp_path / "runspace" / "worktrees" / "escaped"
    escaped.symlink_to(outside, target_is_directory=True)
    _run(tmp_path, "escape", path=escaped)
    plan = select_workspace_cleanup(runs, now=NOW)
    assert {v.reason for v in plan.protected} == {"meta_unreadable", "worktree_path_unsafe"}
    assert [v.snapshot.run_id for v in plan.selected] == ["plain"]


def test_runs_without_a_retained_checkout_are_inert_not_protected(tmp_path):
    runs, _ = _run(tmp_path, "one")
    bare = runs / "bare"
    bare.mkdir()
    (bare / "meta.json").write_text(json.dumps({"status": "done"}))
    no_meta = runs / "no-meta"
    no_meta.mkdir()
    plan = select_workspace_cleanup(runs, now=NOW)
    assert sorted(verdict.reason for verdict in plan.inert) == ["meta_missing", "worktree_missing"]
    assert not plan.protected


def test_checkout_gone_from_disk_is_inert_not_unsafe(tmp_path):
    runs, checkout = _run(tmp_path, "one")
    shutil.rmtree(checkout)
    plan = select_workspace_cleanup(runs, now=NOW)
    assert [verdict.reason for verdict in plan.inert] == ["worktree_gone"]
    assert not plan.protected


def test_legacy_in_run_worktree_layout_is_a_sanctioned_location(tmp_path):
    runs = tmp_path / "runspace" / "runs"
    legacy = runs / "old" / "worktrees" / "wt_x" / "checkout"
    _run(tmp_path, "old", path=legacy)
    plan = select_workspace_cleanup(runs, now=NOW)
    assert [verdict.reason for verdict in plan.selected] == ["retention_expired"]


def test_report_selection_writes_no_receipt_archive_or_meta(tmp_path):
    runs, _ = _run(tmp_path, "one")
    meta_before = (runs / "one" / "meta.json").read_bytes()
    select_workspace_cleanup(runs, now=NOW)
    assert not (runs.parent / "cleanup_receipts").exists()
    assert not (runs.parent / "archive").exists()
    assert (runs / "one" / "meta.json").read_bytes() == meta_before


def test_archive_failure_does_not_start_worktree_removal(tmp_path, monkeypatch):
    runs, checkout = _run(tmp_path, "one")
    called = False

    def removal(**_kwargs):
        nonlocal called
        called = True
        return GitOpResult(ok=True)

    monkeypatch.setattr("pipeline.engine.worktree.reclaim_registered_worktree", removal)
    monkeypatch.setattr(
        "pipeline.engine.workspace_cleanup.shutil.copytree",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("no space")),
    )
    execution = execute_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: True)
    assert not called
    assert checkout.exists()
    assert execution.receipt["status"] == "partial"
    assert execution.receipt_path.exists()


def test_archive_worktree_tier_marks_meta_and_keeps_run_root(tmp_path, monkeypatch):
    runs, checkout = _run(tmp_path, "one")
    calls: list[tuple[Path, Path]] = []

    def removal(*, project_dir, path):
        calls.append((project_dir, path))
        return GitOpResult(ok=True, path=path)

    monkeypatch.setattr("pipeline.engine.worktree.reclaim_registered_worktree", removal)
    execution = execute_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: True)
    meta = json.loads((runs / "one" / "meta.json").read_text())
    assert calls == [(tmp_path / "repo", checkout)]
    assert (runs / "one").is_dir()
    assert meta["worktree"]["reclaimed"]["disposition"] == "archive"
    assert execution.receipt["bytes_archived"] > 0
    assert execution.receipt["bytes_reclaimed"] == 0
    assert execution.receipt["errors"] == []


def test_unregistered_checkout_is_removed_without_touching_git(tmp_path, monkeypatch):
    runs, checkout = _run(tmp_path, "one")
    monkeypatch.setattr(
        "pipeline.engine.worktree.reclaim_registered_worktree",
        lambda **_kwargs: pytest.fail("unregistered checkout must not use the git route"),
    )
    execution = execute_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: False)
    assert not checkout.exists()
    assert execution.receipt["errors"] == []
    assert [op["kind"] for op in execution.receipt["operations"]] == [
        "archive_snapshot",
        "unregistered_checkout_remove",
    ]


def test_reclaimed_worktree_is_reported_as_historical_not_path_unsafe(tmp_path, monkeypatch):
    runs, checkout = _run(tmp_path, "one")
    monkeypatch.setattr(
        "pipeline.engine.worktree.reclaim_registered_worktree",
        lambda **kwargs: GitOpResult(ok=True, path=kwargs["path"]),
    )
    execute_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: True)
    plan = select_workspace_cleanup(runs, now=NOW)
    assert plan.inert[0].snapshot.worktree_path == checkout
    assert plan.inert[0].reason == "already_reclaimed"


def test_archive_worktree_tier_marks_embedded_cross_meta(tmp_path, monkeypatch):
    runs, _ = _run(tmp_path, "cross", projects={"a": "/a"})
    parent = runs / "cross"
    checkout = tmp_path / "runspace" / "worktrees" / "wt_a" / "checkout"
    checkout.mkdir(parents=True)
    child_worktree = {
        "path": str(checkout),
        "source_repo_path": str(tmp_path / "repo"),
        "retention_until": "2026-07-28T00:00:00Z",
    }
    (parent / "a").mkdir()
    (parent / "a" / "meta.json").write_text(
        json.dumps({"status": "done", "worktree": child_worktree})
    )
    parent_meta = json.loads((parent / "meta.json").read_text())
    parent_meta["phases"] = {"projects": {"a": {"status": "done", "worktree": child_worktree}}}
    (parent / "meta.json").write_text(json.dumps(parent_meta))
    monkeypatch.setattr(
        "pipeline.engine.worktree.reclaim_registered_worktree",
        lambda **kwargs: GitOpResult(ok=True, path=kwargs["path"]),
    )
    execute_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: True)
    updated = json.loads((parent / "meta.json").read_text())
    assert updated["phases"]["projects"]["a"]["worktree"]["reclaimed"]["disposition"] == "archive"


def test_execution_reverifies_groups_without_rescanning_workspace(tmp_path, monkeypatch):
    runs, _ = _run(tmp_path, "one")
    _run(tmp_path, "two")
    import pipeline.engine.workspace_cleanup as cleanup

    select = cleanup.select_workspace_cleanup
    calls = 0

    def counted_select(*args, **kwargs):
        nonlocal calls
        calls += 1
        return select(*args, **kwargs)

    monkeypatch.setattr(cleanup, "select_workspace_cleanup", counted_select)
    monkeypatch.setattr(
        "pipeline.engine.worktree.reclaim_registered_worktree",
        lambda **kwargs: GitOpResult(ok=True, path=kwargs["path"]),
    )
    execute_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: True)
    assert calls == 1


@pytest.mark.parametrize("disposition", ["archive", "delete"])
def test_both_tier_receipt_has_counters_and_removes_root_after_worktree(
    tmp_path, monkeypatch, disposition
):
    runs, checkout = _run(tmp_path, "one")
    report = select_workspace_cleanup(runs, now=NOW)
    order: list[str] = []

    def removal(*, project_dir, path):
        order.append("worktree")
        return GitOpResult(ok=True, path=path)

    monkeypatch.setattr("pipeline.engine.worktree.reclaim_registered_worktree", removal)
    execution = execute_workspace_cleanup(
        runs,
        tier="both",
        disposition=disposition,
        now=NOW,
        registration_checker=lambda *_: True,
    )
    assert order == ["worktree"]
    assert not (runs / "one").exists()
    assert execution.receipt_path.exists()
    assert {
        "selected",
        "protected",
        "inert",
        "results",
        "errors",
        "bytes_selected",
        "bytes_archived",
        "bytes_reclaimed",
    } <= execution.receipt.keys()
    assert execution.receipt["root_selected"][0]["root_run_id"] == "one"
    assert [
        result["path"] for result in execution.receipt["results"] if result["kind"] == "run_root"
    ] == [str(runs / root_id) for root_id in report.root_run_ids_for_both]
    operations = [op["kind"] for op in execution.receipt["operations"]]
    if disposition == "archive":
        assert operations[-2:] == ["run_archive_snapshot", "run_root_remove"]
    else:
        assert operations[-1:] == ["run_root_remove"]
    assert (
        execution.receipt["bytes_archived" if disposition == "archive" else "bytes_reclaimed"] > 0
    )


def test_both_tier_removes_an_aged_inert_root_with_no_checkout(tmp_path):
    runs = tmp_path / "runspace" / "runs"
    root = runs / "20260601_000000_no_checkout"
    root.mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"status": "done"}))

    execution = execute_workspace_cleanup(runs, tier="both", disposition="delete", now=NOW)

    assert not root.exists()
    assert execution.receipt["root_selected"][0]["dependency_paths"] == []
    assert [result["kind"] for result in execution.receipt["results"]] == ["run_root"]


def test_both_tier_does_not_remove_root_after_a_failed_checkout_group(tmp_path, monkeypatch):
    runs, _ = _run(tmp_path, "one")
    monkeypatch.setattr(
        "pipeline.engine.worktree.reclaim_registered_worktree",
        lambda **kwargs: GitOpResult(ok=False, error="still registered", path=kwargs["path"]),
    )
    execution = execute_workspace_cleanup(
        runs,
        tier="both",
        now=NOW,
        registration_checker=lambda *_: True,
    )
    assert (runs / "one").exists()
    assert not [result for result in execution.receipt["results"] if result["kind"] == "run_root"]


def test_both_tier_revalidates_root_after_checkout_reclamation(tmp_path, monkeypatch):
    runs, checkout = _run(tmp_path, "one")

    def removal(*, project_dir, path):
        meta_path = runs / "one" / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["status"] = "running"
        meta_path.write_text(json.dumps(meta))
        return GitOpResult(ok=True, path=path)

    monkeypatch.setattr("pipeline.engine.worktree.reclaim_registered_worktree", removal)
    execution = execute_workspace_cleanup(
        runs,
        tier="both",
        now=NOW,
        registration_checker=lambda *_: True,
    )

    assert (runs / "one").exists()
    assert execution.receipt["root_protected"][-1]["reason"] == "changed_before_execution"


def test_root_matrix_projects_real_workspace_facts_and_expires_cross_pause(tmp_path):
    runs = tmp_path / "runspace" / "runs"
    no_meta = runs / "20260601_000000_no_meta"
    no_meta.mkdir(parents=True)
    no_worktree = runs / "20260601_000001_no_worktree"
    no_worktree.mkdir()
    (no_worktree / "meta.json").write_text(json.dumps({"status": "done"}))

    _, gone = _run(tmp_path, "20260601_000002_gone")
    shutil.rmtree(gone)
    _, reclaimed = _run(tmp_path, "20260601_000003_reclaimed")
    reclaimed_meta = json.loads((runs / "20260601_000003_reclaimed" / "meta.json").read_text())
    reclaimed_meta["worktree"]["reclaimed"] = {"disposition": "archive"}
    (runs / "20260601_000003_reclaimed" / "meta.json").write_text(json.dumps(reclaimed_meta))

    _, _ = _run(
        tmp_path,
        "20260601_000004_cross_pause",
        status="awaiting_phase_handoff",
        handoff=True,
        projects={"a": "/a"},
    )
    (runs / "20260601_000004_cross_pause" / "cross_checkpoint.json").write_text(
        json.dumps({"phase_handoff_pending": True})
    )
    cross_pause_meta = json.loads(
        (runs / "20260601_000004_cross_pause" / "meta.json").read_text()
    )
    cross_pause_meta.pop("worktree")
    (runs / "20260601_000004_cross_pause" / "meta.json").write_text(json.dumps(cross_pause_meta))
    child = runs / "20260601_000004_cross_pause" / "a"
    child.mkdir()
    (child / "meta.json").write_text(
        json.dumps(
            {
                "status": "done",
                "worktree": {
                    "path": str(tmp_path / "runspace" / "worktrees" / "old_a" / "checkout"),
                    "source_repo_path": str(tmp_path / "repo"),
                    "retention_until": "2026-07-28T00:00:00Z",
                    "reclaimed": {"disposition": "archive"},
                },
            }
        )
    )
    _run(
        tmp_path,
        "20260601_000005_young_pause",
        status="awaiting_phase_handoff",
        handoff=True,
        deadline="2026-08-01T00:00:00Z",
    )
    _, dirty = _run(tmp_path, "20260601_000006_dirty")
    _init_repo(dirty)
    _, unpushed = _run(tmp_path, "20260601_000007_unpushed")
    _init_repo(unpushed)
    _commit_all(unpushed, "local only")

    plan = select_workspace_cleanup(runs, now=NOW)

    assert {verdict.facts.root_id for verdict in plan.root_selected} == {
        "20260601_000000_no_meta",
        "20260601_000001_no_worktree",
        "20260601_000002_gone",
        "20260601_000003_reclaimed",
        "20260601_000004_cross_pause",
    }
    assert {
        verdict.facts.root_id: verdict.reason for verdict in plan.root_protected
    } == {
        "20260601_000005_young_pause": "active_handoff_or_gate",
        "20260601_000006_dirty": "uncommitted_changes",
        "20260601_000007_unpushed": "unpushed_commits",
    }
    assert next(
        verdict.reason
        for verdict in plan.root_selected
        if verdict.facts.root_id == "20260601_000004_cross_pause"
    ) == "pause_retention_expired"


def test_absent_meta_checkpoint_gate_blocks_legacy_root(tmp_path):
    runs = tmp_path / "runspace" / "runs"
    root = runs / "20260601_000000_checkpoint"
    root.mkdir(parents=True)
    (root / "cross_checkpoint.json").write_text(json.dumps({"pending_gate": {"id": "gate"}}))

    plan = select_workspace_cleanup(runs, now=NOW)

    assert [(verdict.facts.root_id, verdict.reason) for verdict in plan.root_protected] == [
        (root.name, "active_handoff_or_gate")
    ]


def test_unidentified_legacy_checkout_blocks_no_meta_root(tmp_path):
    runs = tmp_path / "runspace" / "runs"
    root = runs / "20260601_000000_legacy"
    checkout = root / "worktrees" / "wt_x" / "checkout"
    checkout.mkdir(parents=True)
    (checkout / ".git").write_text("gitdir: /still-live/worktree\n")

    plan = select_workspace_cleanup(runs, now=NOW)

    assert [(verdict.facts.root_id, verdict.reason) for verdict in plan.root_protected] == [
        (root.name, "nested_checkout_unidentified")
    ]


def test_both_tier_root_revalidation_does_not_rescan_unrelated_roots(tmp_path, monkeypatch):
    runs, _ = _run(tmp_path, "one")
    _run(tmp_path, "two")
    import pipeline.engine.workspace_cleanup as cleanup

    select = cleanup.select_workspace_cleanup
    calls = 0

    def counted_select(*args, **kwargs):
        nonlocal calls
        calls += 1
        return select(*args, **kwargs)

    monkeypatch.setattr(cleanup, "select_workspace_cleanup", counted_select)
    monkeypatch.setattr(
        "pipeline.engine.worktree.reclaim_registered_worktree",
        lambda **kwargs: GitOpResult(ok=True, path=kwargs["path"]),
    )
    execute_workspace_cleanup(
        runs, tier="both", now=NOW, registration_checker=lambda *_: True
    )
    assert calls == 1


def test_only_the_unregistered_removal_helper_may_delete_a_checkout_tree():
    """A registered checkout must go through git; rmtree is confined to one place.

    Removing a *registered* worktree directory leaves a stale registration in
    the source repository, so git owns that route. An unregistered directory
    has no registration to damage — but the permission to delete it must stay
    in the single helper that proves it, never leak into the general paths.
    """
    import pipeline.engine.workspace_cleanup as cleanup

    tree = ast.parse(inspect.getsource(cleanup))
    allowed = "_remove_unregistered_checkout"
    offenders = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef) or func.name == allowed:
            continue
        offenders += [
            node
            for node in ast.walk(func)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "rmtree"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in {"path", "source"}
        ]
    assert not offenders
