from __future__ import annotations

import ast
import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.io.git_helpers import GitOpResult
from pipeline.engine.workspace_cleanup import execute_workspace_cleanup, select_workspace_cleanup
from pipeline.engine.worktree import _compute_retention_timestamp

NOW = datetime(2026, 7, 29, tzinfo=UTC)


def _run(
    tmp_path: Path, run_id: str, *, status: str = "done", deadline: str = "2026-07-28T00:00:00Z",
    handoff: bool = False, path: Path | None = None, projects: dict | None = None,
) -> tuple[Path, Path]:
    runs = tmp_path / "runspace" / "runs"
    run_dir = runs / run_id
    checkout = path or tmp_path / "runspace" / "worktrees" / f"wt_{run_id}" / "checkout"
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / "retained.txt").write_text("retained checkout evidence\n")
    manifest = checkout.parent / "manifest.json"
    manifest.write_text(json.dumps({"checkout_path": str(checkout), "attached_run_ids": [run_id]}))
    meta = {
        "status": status,
        "worktree": {
            "path": str(checkout), "source_repo_path": str(tmp_path / "repo"),
            "retention_until": deadline, "manifest_path": str(manifest),
        },
    }
    if handoff:
        meta["phase_handoff"] = {"id": "pending"}
    if projects is not None:
        meta["projects"] = projects
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(json.dumps(meta))
    return runs, checkout


@pytest.mark.parametrize(("status", "handoff", "deadline", "selected", "reason"), [
    ("done", False, "2026-07-28T00:00:00Z", True, "retention_expired"),
    ("halted", False, "2026-07-30T00:00:00Z", False, "retention_active"),
    ("running", False, "2026-07-28T00:00:00Z", False, "status_not_stopped"),
    ("done", True, "2026-07-28T00:00:00Z", False, "active_handoff_or_gate"),
])
def test_status_pause_and_deadline_matrix(tmp_path, status, handoff, deadline, selected, reason):
    runs, _ = _run(tmp_path, "one", status=status, handoff=handoff, deadline=deadline)
    plan = select_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: True)
    verdict = (*plan.selected, *plan.protected)[0]
    assert verdict.selected is selected
    assert verdict.reason == reason


def test_shared_checkout_is_protected_when_one_reference_is_live(tmp_path):
    runs, checkout = _run(tmp_path, "one")
    _run(tmp_path, "two", status="running", path=checkout)
    (checkout.parent / "manifest.json").write_text(json.dumps({
        "checkout_path": str(checkout), "attached_run_ids": ["one", "two"],
    }))
    plan = select_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: True)
    assert not plan.selected
    assert {v.reason for v in plan.protected} == {"shared_checkout_protected", "status_not_stopped"}


def test_shared_checkout_is_protected_when_manifest_attachment_meta_is_unreadable(tmp_path):
    runs, checkout = _run(tmp_path, "one")
    broken = runs / "two"
    broken.mkdir()
    (broken / "meta.json").write_text("{")
    (checkout.parent / "manifest.json").write_text(json.dumps({
        "checkout_path": str(checkout), "attached_run_ids": ["one", "two"],
    }))

    plan = select_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: True)

    assert not plan.selected
    assert {verdict.reason for verdict in plan.protected} == {
        "meta_unreadable", "shared_reference_unproven",
    }


def test_reverify_group_rejects_manifest_attachment_added_after_selection(tmp_path):
    runs, checkout = _run(tmp_path, "one")
    import pipeline.engine.workspace_cleanup as cleanup

    plan = cleanup.select_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: True)
    _run(tmp_path, "two", status="running", path=checkout)
    (checkout.parent / "manifest.json").write_text(json.dumps({
        "checkout_path": str(checkout), "attached_run_ids": ["one", "two"],
    }))

    assert cleanup._reverify_group(
        list(plan.selected), runs, NOW, lambda *_: True,
    ) is None


def test_missing_manifest_protects_without_failing_selection(tmp_path):
    runs, checkout = _run(tmp_path, "one")
    (checkout.parent / "manifest.json").unlink()

    plan = select_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: True)

    assert not plan.selected
    assert [verdict.reason for verdict in plan.protected] == ["shared_reference_unproven"]


def test_external_manifest_path_protects_without_failing_selection(tmp_path):
    runs, checkout = _run(tmp_path, "one")
    external_manifest = tmp_path / "external-manifest.json"
    external_manifest.write_text(json.dumps({
        "checkout_path": str(checkout), "attached_run_ids": ["one"],
    }))
    meta_path = runs / "one" / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["worktree"]["manifest_path"] = str(external_manifest)
    meta_path.write_text(json.dumps(meta))

    plan = select_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: True)

    assert not plan.selected
    assert [verdict.reason for verdict in plan.protected] == ["shared_reference_unproven"]


def test_writer_retention_timestamp_is_accepted_by_selection(tmp_path):
    deadline = _compute_retention_timestamp(retention_days=1)
    assert deadline is not None and deadline.endswith("+00:00")
    parsed = datetime.fromisoformat(deadline)
    runs, _ = _run(tmp_path, "one", deadline=deadline)
    plan = select_workspace_cleanup(
        runs, now=parsed + timedelta(seconds=1), registration_checker=lambda *_: True,
    )
    assert [verdict.reason for verdict in plan.selected] == ["retention_expired"]


def test_cross_root_requires_every_declared_child(tmp_path):
    runs, _ = _run(tmp_path, "cross", projects={"a": "/a", "b": "/b"})
    _run(tmp_path, "ignored")  # A separate mono run must not affect cross grouping.
    parent = runs / "cross"
    for alias, status in (("a", "done"), ("b", "running")):
        checkout = tmp_path / "runspace" / "worktrees" / f"wt_{alias}" / "checkout"
        checkout.mkdir(parents=True)
        manifest = checkout.parent / "manifest.json"
        manifest.write_text(json.dumps({"checkout_path": str(checkout), "attached_run_ids": [alias]}))
        (parent / alias).mkdir()
        (parent / alias / "meta.json").write_text(json.dumps({"status": status, "worktree": {
            "path": str(checkout), "source_repo_path": str(tmp_path / "repo"),
            "retention_until": "2026-07-28T00:00:00Z", "manifest_path": str(manifest),
        }}))
    plan = select_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: True)
    assert "cross" not in plan.root_run_ids_for_both


@pytest.mark.parametrize(("status", "handoff", "reason"), [
    ("running", False, "parent_not_stopped"),
    ("done", True, "parent_active_handoff_or_gate"),
])
def test_live_or_paused_cross_parent_protects_all_children(tmp_path, status, handoff, reason):
    runs, _ = _run(tmp_path, "cross", status=status, handoff=handoff, projects={"a": "/a", "b": "/b"})
    parent = runs / "cross"
    for alias in ("a", "b"):
        checkout = tmp_path / "runspace" / "worktrees" / f"wt_{alias}" / "checkout"
        checkout.mkdir(parents=True)
        manifest = checkout.parent / "manifest.json"
        manifest.write_text(json.dumps({"checkout_path": str(checkout), "attached_run_ids": [alias]}))
        (parent / alias).mkdir()
        (parent / alias / "meta.json").write_text(json.dumps({"status": "done", "worktree": {
            "path": str(checkout), "source_repo_path": str(tmp_path / "repo"),
            "retention_until": "2026-07-28T00:00:00Z", "manifest_path": str(manifest),
        }}))
    plan = select_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: True)
    child_verdicts = [
        verdict for verdict in plan.protected if verdict.snapshot.run_id in {"a", "b"}
    ]
    assert child_verdicts and {verdict.reason for verdict in child_verdicts} == {reason}
    assert "cross" not in plan.root_run_ids_for_both


def test_symlink_escape_unreadable_meta_and_unregistered_are_protected(tmp_path):
    runs, checkout = _run(tmp_path, "unregistered")
    (runs / "broken").mkdir()
    (runs / "broken" / "meta.json").write_text("{")
    outside = tmp_path / "outside"
    outside.mkdir()
    escaped = tmp_path / "runspace" / "worktrees" / "escaped"
    escaped.symlink_to(outside, target_is_directory=True)
    _run(tmp_path, "escape", path=escaped)
    plan = select_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: False)
    assert {v.reason for v in plan.protected} >= {"meta_unreadable", "worktree_path_unsafe", "registration_unproven"}


def test_report_selection_writes_no_receipt_archive_or_meta(tmp_path):
    runs, _ = _run(tmp_path, "one")
    meta_before = (runs / "one" / "meta.json").read_bytes()
    select_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: True)
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
    monkeypatch.setattr("pipeline.engine.workspace_cleanup.shutil.copytree", lambda *_a, **_k: (_ for _ in ()).throw(OSError("no space")))
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


def test_reclaimed_worktree_is_reported_as_historical_not_path_unsafe(tmp_path, monkeypatch):
    runs, checkout = _run(tmp_path, "one")
    monkeypatch.setattr(
        "pipeline.engine.worktree.reclaim_registered_worktree",
        lambda **kwargs: GitOpResult(ok=True, path=kwargs["path"]),
    )
    execute_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: True)
    plan = select_workspace_cleanup(runs, now=NOW, registration_checker=lambda *_: True)
    assert plan.protected[0].snapshot.worktree_path == checkout
    assert plan.protected[0].reason == "already_reclaimed"


def test_archive_worktree_tier_marks_embedded_cross_meta(tmp_path, monkeypatch):
    runs, _ = _run(tmp_path, "cross", projects={"a": "/a"})
    parent = runs / "cross"
    checkout = tmp_path / "runspace" / "worktrees" / "wt_a" / "checkout"
    checkout.mkdir(parents=True)
    manifest = checkout.parent / "manifest.json"
    manifest.write_text(json.dumps({"checkout_path": str(checkout), "attached_run_ids": ["a"]}))
    child_worktree = {
        "path": str(checkout), "source_repo_path": str(tmp_path / "repo"),
        "retention_until": "2026-07-28T00:00:00Z", "manifest_path": str(manifest),
    }
    (parent / "a").mkdir()
    (parent / "a" / "meta.json").write_text(json.dumps({"status": "done", "worktree": child_worktree}))
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
def test_both_tier_receipt_has_counters_and_removes_root_after_worktree(tmp_path, monkeypatch, disposition):
    runs, checkout = _run(tmp_path, "one")
    order: list[str] = []

    def removal(*, project_dir, path):
        order.append("worktree")
        return GitOpResult(ok=True, path=path)

    monkeypatch.setattr("pipeline.engine.worktree.reclaim_registered_worktree", removal)
    execution = execute_workspace_cleanup(
        runs, tier="both", disposition=disposition, now=NOW,
        registration_checker=lambda *_: True,
    )
    assert order == ["worktree"]
    assert not (runs / "one").exists()
    assert execution.receipt_path.exists()
    assert {"selected", "protected", "results", "errors", "bytes_selected", "bytes_archived", "bytes_reclaimed"} <= execution.receipt.keys()
    assert execution.receipt["bytes_archived" if disposition == "archive" else "bytes_reclaimed"] > 0


def test_cleanup_module_never_recursively_deletes_a_worktree_path():
    import pipeline.engine.workspace_cleanup as cleanup

    tree = ast.parse(inspect.getsource(cleanup))
    offenders = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "rmtree"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in {"path", "source"}
    ]
    assert not offenders
