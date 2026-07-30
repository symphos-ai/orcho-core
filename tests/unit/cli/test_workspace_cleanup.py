from __future__ import annotations

import argparse
import ast
import json
from datetime import timedelta
from pathlib import Path

import pytest

from cli._workspace_cleanup import (
    WorkspaceCleanupCliResult,
    format_workspace_cleanup,
    workspace_cleanup_from_args,
)
from cli.orcho import build_parser, cmd_workspace_cleanup
from sdk.cleanup import (
    WorkspaceCleanupReasonSummary,
    WorkspaceCleanupReceipt,
    WorkspaceCleanupReport,
    report_workspace_cleanup,
)


def _report(runs: Path) -> WorkspaceCleanupReport:
    return WorkspaceCleanupReport(
        runs_dir=runs,
        reclaimable_count=1,
        protected_count=1,
        inert_count=1,
        reclaimable_reasons=(WorkspaceCleanupReasonSummary("retention_expired", 1, "old checkout"),),
        protected_reasons=(WorkspaceCleanupReasonSummary("status_not_stopped", 1, "live run"),),
        inert_reasons=(WorkspaceCleanupReasonSummary("worktree_gone", 1, "gone checkout"),),
        reclaimable_run_root_count=1,
        protected_run_root_count=1,
        reclaimable_run_root_reasons=(WorkspaceCleanupReasonSummary("retention_expired", 1, "old root"),),
        protected_run_root_reasons=(WorkspaceCleanupReasonSummary("retention_active", 1, "young root"),),
    )


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


def test_delete_without_tier_is_rejected_by_validation():
    args = argparse.Namespace(workspace=None, reclaim_worktrees=False, reclaim_both=False, delete=True)
    with pytest.raises(ValueError, match="requires"):
        workspace_cleanup_from_args(args)


def test_default_is_report_noop(tmp_path, monkeypatch):
    runs = tmp_path / "runspace" / "runs"
    calls: list[dict] = []

    def report(**kwargs):
        calls.append(kwargs)
        return _report(runs)

    monkeypatch.setattr("cli._workspace_cleanup.report_workspace_cleanup", report)
    monkeypatch.setattr(
        "cli._workspace_cleanup.reclaim_workspace_cleanup",
        lambda **_kwargs: pytest.fail("report must not execute"),
    )
    result = workspace_cleanup_from_args(
        argparse.Namespace(workspace=None, reclaim_worktrees=False, reclaim_both=False, delete=False),
    )
    assert result.tier == "report"
    assert result.receipt is None
    assert calls == [{"workspace": None, "older_than": timedelta(days=30)}]


@pytest.mark.parametrize(("both", "delete", "tier", "disposition"), [
    (False, False, "worktrees", "archive"),
    (True, True, "both", "delete"),
])
def test_reclaim_tiers_forward_archive_delete_scope(tmp_path, monkeypatch, both, delete, tier, disposition):
    runs = tmp_path / "runspace" / "runs"
    report_calls: list[dict] = []
    reclaim_calls: list[dict] = []
    monkeypatch.setattr(
        "cli._workspace_cleanup.report_workspace_cleanup",
        lambda **kwargs: report_calls.append(kwargs) or _report(runs),
    )
    receipt = WorkspaceCleanupReceipt(
        Path("/receipt.json"), tier, disposition, "complete", 1, 1, 0, 0, (), ()
    )
    monkeypatch.setattr(
        "cli._workspace_cleanup.reclaim_workspace_cleanup",
        lambda **kwargs: reclaim_calls.append(kwargs) or receipt,
    )
    result = workspace_cleanup_from_args(argparse.Namespace(
        workspace=None, reclaim_worktrees=not both, reclaim_both=both, delete=delete, older_than=7,
    ))
    assert result.tier == tier
    assert report_calls == [{"workspace": None, "older_than": timedelta(days=7)}]
    assert reclaim_calls == [{
        "tier": tier, "disposition": disposition, "workspace": None,
        "older_than": timedelta(days=7),
    }]


def test_report_renders_separate_root_summaries(tmp_path, monkeypatch):
    runs = tmp_path / "runspace" / "runs"
    monkeypatch.setattr("cli._workspace_cleanup.report_workspace_cleanup", lambda **_kwargs: _report(runs))
    result = workspace_cleanup_from_args(
        argparse.Namespace(workspace=None, reclaim_worktrees=False, reclaim_both=False, delete=False),
    )
    rendered = format_workspace_cleanup(result)
    assert "Run-root cutoff: 30 days" in rendered
    assert "Eligible run roots: 1" in rendered
    assert "eligible run root retention_expired: 1" in rendered
    assert "protected run root retention_active: 1" in rendered


def test_formatter_preserves_receipt_labels(tmp_path: Path) -> None:
    runs = tmp_path / "runspace" / "runs"
    result = WorkspaceCleanupCliResult(
        tmp_path, runs, "worktrees", "archive", 30, _report(runs), WorkspaceCleanupReceipt(
            Path("/receipt.json"), "worktrees", "archive", "partial", 8, 5, 0,
            1, ("copy failed",), (Path("/archive/one"),),
        )
    )
    rendered = format_workspace_cleanup(result)
    for label in (
        "Status: partial", "Bytes selected: 8", "Bytes archived: 5",
        "Bytes reclaimed: 0", "Receipt: /receipt.json", "Archive: /archive/one",
        "Partial failure: copy failed",
    ):
        assert label in rendered


def test_cli_report_equals_direct_sdk_report(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runs = workspace / "runspace" / "runs"
    run_dir = runs / "gone"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "status": "done",
        "worktree": {"path": str(tmp_path / "missing"), "retention_until": "2020-01-01T00:00:00Z"},
    }))

    direct = report_workspace_cleanup(workspace=workspace, cwd=None)
    cli = workspace_cleanup_from_args(argparse.Namespace(
        workspace=workspace, reclaim_worktrees=False, reclaim_both=False, delete=False,
    ))
    assert cli.report.runs_dir == direct.runs_dir
    assert cli.report.reclaimable_count == direct.reclaimable_count
    assert cli.report.protected_count == direct.protected_count
    assert cli.report.inert_count == direct.inert_count
    assert cli.report.reclaimable_reasons == direct.reclaimable_reasons
    assert cli.report.protected_reasons == direct.protected_reasons
    assert cli.report.inert_reasons == direct.inert_reasons
    assert cli.report.reclaimable_run_root_reasons == direct.reclaimable_run_root_reasons
    assert cli.report.protected_run_root_reasons == direct.protected_run_root_reasons


def test_partial_receipt_returns_exit_code_one(monkeypatch):
    partial = type("Result", (), {"receipt": WorkspaceCleanupReceipt(
        Path("/receipt.json"), "worktrees", "archive", "partial", 0, 0, 0, 1, ("failed",), ()
    )})()
    monkeypatch.setattr("cli._workspace_cleanup.workspace_cleanup_from_args", lambda _args: partial)
    monkeypatch.setattr("cli._workspace_cleanup.format_workspace_cleanup", lambda _result: "cleanup")
    assert cmd_workspace_cleanup(argparse.Namespace()) == 1


def test_direct_engine_cleanup_imports_are_confined_to_sdk_and_engine_tests():
    root = Path(__file__).resolve().parents[3]
    # Scan source only. Build/vendor trees carry copies of the source (e.g.
    # ``build/lib/sdk/cleanup.py`` from a packaging test) that would otherwise
    # register as phantom boundary violations.
    ignored = {"build", "dist", ".git", ".venv", "venv", "node_modules", "__pycache__", ".eggs"}
    found: list[str] = []
    for path in root.rglob("*.py"):
        if ignored.intersection(path.relative_to(root).parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            # `from pipeline.engine.workspace_cleanup import ...`
            (isinstance(node, ast.ImportFrom) and node.module == "pipeline.engine.workspace_cleanup")
            # `from pipeline.engine import workspace_cleanup`
            or (
                isinstance(node, ast.ImportFrom)
                and node.module == "pipeline.engine"
                and any(alias.name == "workspace_cleanup" for alias in node.names)
            )
            # `import pipeline.engine.workspace_cleanup`
            or any(alias.name == "pipeline.engine.workspace_cleanup" for alias in node.names)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ):
            found.append(str(path.relative_to(root)))
    assert sorted(found) == [
        "sdk/cleanup.py",
        "tests/unit/pipeline/engine/test_workspace_cleanup.py",
    ]
