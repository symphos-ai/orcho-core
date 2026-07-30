from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import sdk.cleanup as cleanup
from sdk import to_jsonable


def _run(tmp_path: Path, run_id: str, *, status: str = "done", checkout: bool = True) -> Path:
    runs = tmp_path / "runspace" / "runs"
    run_dir = runs / run_id
    run_dir.mkdir(parents=True)
    worktree = tmp_path / "runspace" / "worktrees" / run_id / "checkout"
    if checkout:
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: /missing/.git/worktrees/test\\n")
    (run_dir / "meta.json").write_text(json.dumps({
        "status": status,
        "worktree": {
            "path": str(worktree),
            "source_repo_path": str(tmp_path / "repo"),
            "retention_until": "2020-01-01T00:00:00Z",
        },
    }))
    return runs


def test_report_projects_bound_engine_selection_without_writes(tmp_path: Path) -> None:
    runs = _run(tmp_path, "old")
    _run(tmp_path, "live", status="running")
    _run(tmp_path, "gone", checkout=False)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    # Compare against the engine plan through the bound SDK re-export, not a
    # direct engine import — this test must obey the same boundary it asserts.
    plan = cleanup.select_workspace_cleanup(runs)

    report = cleanup.report_workspace_cleanup(runs_dir=runs, cwd=None)

    assert (report.reclaimable_count, report.protected_count, report.inert_count) == (
        len(plan.selected), len(plan.protected), len(plan.inert)
    )
    assert report.reclaimable_reasons == (
        cleanup.WorkspaceCleanupReasonSummary("retention_expired", 1, plan.selected[0].detail),
    )
    assert report.protected_reasons == (
        cleanup.WorkspaceCleanupReasonSummary("status_not_stopped", 1, plan.protected[0].detail),
    )
    assert report.inert_reasons == (
        cleanup.WorkspaceCleanupReasonSummary("worktree_gone", 1, plan.inert[0].detail),
    )
    assert json.loads(json.dumps(to_jsonable(report)))["reclaimable_count"] == 1
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
    assert not (runs.parent / "cleanup_receipts").exists()
    assert not (runs.parent / "cleanup_archive").exists()


def test_report_omits_none_cutoff_and_summaries_are_deterministic(tmp_path: Path, monkeypatch) -> None:
    runs = _run(tmp_path, "one")
    calls: list[dict] = []
    select = cleanup.select_workspace_cleanup

    def recorded(*args, **kwargs):
        calls.append(kwargs)
        return select(*args, **kwargs)

    monkeypatch.setattr(cleanup, "select_workspace_cleanup", recorded)
    report = cleanup.report_workspace_cleanup(runs_dir=runs, cwd=None)
    assert calls == [{}]
    assert cleanup.report_workspace_cleanup(runs_dir=runs, cwd=None, older_than=timedelta(days=2)).runs_dir == runs
    assert report.reclaimable_reasons == tuple(sorted(report.reclaimable_reasons, key=lambda row: (-row.count, row.reason)))


def test_reclaim_omits_none_cutoff_and_copies_only_receipt_facts(tmp_path: Path, monkeypatch) -> None:
    runs = _run(tmp_path, "one")
    calls: list[dict] = []

    class Execution:
        receipt_path = Path("/receipt.json")
        receipt = {
            "tier": "worktrees", "disposition": "archive", "status": "partial",
            "bytes_selected": 8, "bytes_archived": 5, "bytes_reclaimed": 0,
            "errors": [{"message": "copy failed"}],
            "results": [{"archive_path": "/archive/one"}],
        }

    def execute(*_args, **kwargs):
        calls.append(kwargs)
        return Execution()

    monkeypatch.setattr(cleanup, "execute_workspace_cleanup", execute)
    receipt = cleanup.reclaim_workspace_cleanup(
        runs_dir=runs, cwd=None, tier="worktrees", disposition="archive"
    )
    assert calls == [{"tier": "worktrees", "disposition": "archive"}]
    assert receipt.error_count == 1
    assert receipt.errors == ("{'message': 'copy failed'}",)
    assert receipt.archive_paths == (Path("/archive/one"),)
