from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path

import pytest

from cli._workspace_cleanup import format_workspace_cleanup, workspace_cleanup_from_args
from cli.orcho import build_parser
from pipeline.engine.workspace_cleanup import WorkspaceCleanupPlan
from pipeline.engine.workspace_run_retention import RunRootFacts, RunRootVerdict


def test_parser_accepts_only_one_reclaim_tier():
    parser = build_parser()
    args = parser.parse_args(["workspace", "cleanup", "--reclaim-worktrees"])
    assert args.reclaim_worktrees is True
    with pytest.raises(SystemExit):
        parser.parse_args([
            "workspace", "cleanup", "--reclaim-worktrees", "--reclaim-both",
        ])


def test_older_than_parser_defaults_customizes_and_rejects_non_positive():
    parser = build_parser()
    assert parser.parse_args(["workspace", "cleanup"]).older_than == 30
    assert parser.parse_args(["workspace", "cleanup", "--older-than", "7"]).older_than == 7
    with pytest.raises(SystemExit):
        parser.parse_args(["workspace", "cleanup", "--older-than", "0"])


def test_delete_without_tier_is_rejected_by_validation(tmp_path, monkeypatch):
    monkeypatch.setattr("cli._workspace_cleanup.find_runs_dir", lambda **_: tmp_path / "runspace" / "runs")
    args = argparse.Namespace(workspace=None, reclaim_worktrees=False, reclaim_both=False, delete=True)
    with pytest.raises(ValueError, match="requires"):
        workspace_cleanup_from_args(args)


def test_default_is_report_noop(tmp_path, monkeypatch):
    runs = tmp_path / "runspace" / "runs"
    plan = WorkspaceCleanupPlan(runs, (), (), (), ())
    monkeypatch.setattr("cli._workspace_cleanup.find_runs_dir", lambda **_: runs)
    calls: list[dict] = []

    def select(_runs, **kwargs):
        calls.append(kwargs)
        return plan

    monkeypatch.setattr("cli._workspace_cleanup.select_workspace_cleanup", select)
    monkeypatch.setattr("cli._workspace_cleanup.execute_workspace_cleanup", lambda *_a, **_k: pytest.fail("report must not execute"))
    result = workspace_cleanup_from_args(
        argparse.Namespace(workspace=None, reclaim_worktrees=False, reclaim_both=False, delete=False),
    )
    assert result.tier == "report"
    assert result.execution is None
    assert calls == [{"older_than": timedelta(days=30)}]


@pytest.mark.parametrize(("both", "delete", "tier", "disposition"), [
    (False, False, "worktrees", "archive"),
    (True, True, "both", "delete"),
])
def test_reclaim_tiers_forward_archive_delete_scope(tmp_path, monkeypatch, both, delete, tier, disposition):
    runs = tmp_path / "runspace" / "runs"
    calls: list[dict] = []
    plan = WorkspaceCleanupPlan(runs, (), (), (), ())
    monkeypatch.setattr("cli._workspace_cleanup.find_runs_dir", lambda **_: runs)
    select_calls: list[dict] = []

    def select(_runs, **kwargs):
        select_calls.append(kwargs)
        return plan

    monkeypatch.setattr("cli._workspace_cleanup.select_workspace_cleanup", select)

    class Execution:
        receipt = {"status": "complete"}
        receipt_path = Path("/receipt.json")

    def execute(*_args, **kwargs):
        calls.append(kwargs)
        return Execution()

    monkeypatch.setattr("cli._workspace_cleanup.execute_workspace_cleanup", execute)
    result = workspace_cleanup_from_args(argparse.Namespace(
        workspace=None, reclaim_worktrees=not both, reclaim_both=both, delete=delete, older_than=7,
    ))
    assert result.tier == tier
    assert select_calls == [{"older_than": timedelta(days=7)}]
    assert calls == [{
        "tier": tier, "disposition": disposition,
        "older_than": timedelta(days=7),
        "archive_root": runs.parent / "cleanup_archive",
    }]


def test_report_renders_separate_root_summaries(tmp_path, monkeypatch):
    runs = tmp_path / "runspace" / "runs"
    facts = RunRootFacts(
        root_id="old",
        run_dir=runs / "old",
        meta=None,
        meta_exists=False,
        status=None,
        retention_until=None,
        active_handoff=False,
        checkpoint_handoff=False,
        active_gate=False,
        path_safe=True,
        nested_checkout_unidentified=False,
        checkout_blocker=None,
        dependency_paths=(),
    )
    eligible = RunRootVerdict(facts, True, "retention_expired", "old root")
    protected = RunRootVerdict(facts, False, "retention_active", "young root")
    plan = WorkspaceCleanupPlan(runs, (), (), (), (), (eligible,), (protected,))
    monkeypatch.setattr("cli._workspace_cleanup.find_runs_dir", lambda **_: runs)
    monkeypatch.setattr("cli._workspace_cleanup.select_workspace_cleanup", lambda *_a, **_k: plan)
    result = workspace_cleanup_from_args(
        argparse.Namespace(workspace=None, reclaim_worktrees=False, reclaim_both=False, delete=False),
    )
    rendered = format_workspace_cleanup(result)
    assert "Run-root cutoff: 30 days" in rendered
    assert "Eligible run roots: 1" in rendered
    assert "eligible run root retention_expired: 1" in rendered
    assert "protected run root retention_active: 1" in rendered
