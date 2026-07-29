"""Thin CLI adapter and renderer for ``orcho workspace cleanup``."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pipeline.engine.workspace_cleanup import (
    WorkspaceCleanupExecution,
    WorkspaceCleanupPlan,
    execute_workspace_cleanup,
    select_workspace_cleanup,
)
from sdk.runs import find_runs_dir


@dataclass(frozen=True, slots=True)
class WorkspaceCleanupCliResult:
    workspace: Path
    runs_dir: Path
    tier: Literal["report", "worktrees", "both"]
    disposition: Literal["archive", "delete"] | None
    plan: WorkspaceCleanupPlan
    execution: WorkspaceCleanupExecution | None


def workspace_cleanup_from_args(args: object) -> WorkspaceCleanupCliResult:
    """Resolve the workspace, select, and optionally execute a cleanup tier."""
    runs_dir = find_runs_dir(workspace=getattr(args, "workspace", None))
    worktrees = bool(getattr(args, "reclaim_worktrees", False))
    both = bool(getattr(args, "reclaim_both", False))
    delete = bool(getattr(args, "delete", False))
    if delete and not (worktrees or both):
        raise ValueError("--delete requires --reclaim-worktrees or --reclaim-both")
    tier: Literal["report", "worktrees", "both"] = (
        "both" if both else "worktrees" if worktrees else "report"
    )
    if tier == "report":
        plan = select_workspace_cleanup(runs_dir)
        return WorkspaceCleanupCliResult(runs_dir.parent.parent, runs_dir, tier, None, plan, None)
    disposition: Literal["archive", "delete"] = "delete" if delete else "archive"
    # Keep the selection that the operator requested visible beside the
    # execution receipt.  Re-selecting after mutation would turn reclaimed
    # historical paths into a misleading post-cleanup protection report.
    plan = select_workspace_cleanup(runs_dir)
    execution = execute_workspace_cleanup(
        runs_dir,
        tier="both" if tier == "both" else "worktrees",
        disposition=disposition,
        archive_root=runs_dir.parent / "cleanup_archive",
    )
    return WorkspaceCleanupCliResult(
        runs_dir.parent.parent,
        runs_dir,
        tier,
        disposition,
        plan,
        execution,
    )


def format_workspace_cleanup(result: WorkspaceCleanupCliResult) -> str:
    """Render a deterministic human report; mutation facts come from receipt."""
    lines = [
        f"Workspace: {result.workspace}",
        f"Runs dir: {result.runs_dir}",
        f"Tier: {result.tier}",
        f"Disposition: {result.disposition or 'report (no changes)'}",
        f"Reclaimable: {len(result.plan.selected)}",
        f"Protected: {len(result.plan.protected)}",
    ]
    for verdict in result.plan.selected:
        lines.append(f"  reclaimable {verdict.snapshot.run_id}: {verdict.reason} — {verdict.detail}")
    for verdict in result.plan.protected:
        lines.append(f"  protected {verdict.snapshot.run_id}: {verdict.reason} — {verdict.detail}")
    if result.execution is not None:
        receipt = result.execution.receipt
        lines.extend([
            f"Status: {receipt['status']}",
            f"Bytes selected: {receipt['bytes_selected']}",
            f"Bytes archived: {receipt['bytes_archived']}",
            f"Bytes reclaimed: {receipt['bytes_reclaimed']}",
            f"Receipt: {result.execution.receipt_path}",
        ])
        for item in receipt["selected"]:
            lines.append(
                f"  selected {item['run_id']}: {item['reason']} — {item['detail']}"
            )
        for item in receipt["protected"]:
            lines.append(
                f"  protected {item.get('run_id', '?')}: {item['reason']} — {item['detail']}"
            )
        for row in receipt["results"]:
            if row.get("archive_path"):
                lines.append(f"Archive: {row['archive_path']}")
        for error in receipt["errors"]:
            lines.append(f"Partial failure: {error}")
    return "\n".join(lines)
