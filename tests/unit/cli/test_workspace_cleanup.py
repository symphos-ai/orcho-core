from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from cli._workspace_cleanup import workspace_cleanup_from_args
from cli.orcho import build_parser
from pipeline.engine.workspace_cleanup import WorkspaceCleanupPlan


def test_parser_accepts_only_one_reclaim_tier():
    parser = build_parser()
    args = parser.parse_args(["workspace", "cleanup", "--reclaim-worktrees"])
    assert args.reclaim_worktrees is True
    with pytest.raises(SystemExit):
        parser.parse_args([
            "workspace", "cleanup", "--reclaim-worktrees", "--reclaim-both",
        ])


def test_delete_without_tier_is_rejected_by_validation(tmp_path, monkeypatch):
    monkeypatch.setattr("cli._workspace_cleanup.find_runs_dir", lambda **_: tmp_path / "runspace" / "runs")
    args = argparse.Namespace(workspace=None, reclaim_worktrees=False, reclaim_both=False, delete=True)
    with pytest.raises(ValueError, match="requires"):
        workspace_cleanup_from_args(args)


def test_default_is_report_noop(tmp_path, monkeypatch):
    runs = tmp_path / "runspace" / "runs"
    plan = WorkspaceCleanupPlan(runs, (), (), ())
    monkeypatch.setattr("cli._workspace_cleanup.find_runs_dir", lambda **_: runs)
    monkeypatch.setattr("cli._workspace_cleanup.select_workspace_cleanup", lambda _runs: plan)
    monkeypatch.setattr("cli._workspace_cleanup.execute_workspace_cleanup", lambda *_a, **_k: pytest.fail("report must not execute"))
    result = workspace_cleanup_from_args(
        argparse.Namespace(workspace=None, reclaim_worktrees=False, reclaim_both=False, delete=False),
    )
    assert result.tier == "report"
    assert result.execution is None


@pytest.mark.parametrize(("both", "delete", "tier", "disposition"), [
    (False, False, "worktrees", "archive"),
    (True, True, "both", "delete"),
])
def test_reclaim_tiers_forward_archive_delete_scope(tmp_path, monkeypatch, both, delete, tier, disposition):
    runs = tmp_path / "runspace" / "runs"
    calls: list[dict] = []
    plan = WorkspaceCleanupPlan(runs, (), (), ())
    monkeypatch.setattr("cli._workspace_cleanup.find_runs_dir", lambda **_: runs)
    monkeypatch.setattr("cli._workspace_cleanup.select_workspace_cleanup", lambda _runs: plan)

    class Execution:
        receipt = {"status": "complete"}
        receipt_path = Path("/receipt.json")

    def execute(*_args, **kwargs):
        calls.append(kwargs)
        return Execution()

    monkeypatch.setattr("cli._workspace_cleanup.execute_workspace_cleanup", execute)
    result = workspace_cleanup_from_args(argparse.Namespace(
        workspace=None, reclaim_worktrees=not both, reclaim_both=both, delete=delete,
    ))
    assert result.tier == tier
    assert calls == [{
        "tier": tier, "disposition": disposition,
        "archive_root": runs.parent / "cleanup_archive",
    }]
