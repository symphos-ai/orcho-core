"""Thin CLI adapter and renderer for ``orcho workspace cleanup``."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
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
    older_than_days: int
    plan: WorkspaceCleanupPlan
    execution: WorkspaceCleanupExecution | None


def workspace_cleanup_from_args(args: object) -> WorkspaceCleanupCliResult:
    """Resolve the workspace, select, and optionally execute a cleanup tier."""
    runs_dir = find_runs_dir(workspace=getattr(args, "workspace", None))
    worktrees = bool(getattr(args, "reclaim_worktrees", False))
    both = bool(getattr(args, "reclaim_both", False))
    delete = bool(getattr(args, "delete", False))
    older_than_days = int(getattr(args, "older_than", 30))
    if older_than_days <= 0:
        raise ValueError("--older-than must be a positive integer")
    older_than = timedelta(days=older_than_days)
    if delete and not (worktrees or both):
        raise ValueError("--delete requires --reclaim-worktrees or --reclaim-both")
    tier: Literal["report", "worktrees", "both"] = (
        "both" if both else "worktrees" if worktrees else "report"
    )
    if tier == "report":
        plan = select_workspace_cleanup(runs_dir, older_than=older_than)
        return WorkspaceCleanupCliResult(
            runs_dir.parent.parent, runs_dir, tier, None, older_than_days, plan, None,
        )
    disposition: Literal["archive", "delete"] = "delete" if delete else "archive"
    # Keep the selection that the operator requested visible beside the
    # execution receipt.  Re-selecting after mutation would turn reclaimed
    # historical paths into a misleading post-cleanup protection report.
    plan = select_workspace_cleanup(runs_dir, older_than=older_than)
    execution = execute_workspace_cleanup(
        runs_dir,
        tier="both" if tier == "both" else "worktrees",
        disposition=disposition,
        older_than=older_than,
        archive_root=runs_dir.parent / "cleanup_archive",
    )
    return WorkspaceCleanupCliResult(
        runs_dir.parent.parent,
        runs_dir,
        tier,
        disposition,
        older_than_days,
        plan,
        execution,
    )


def format_workspace_cleanup(result: WorkspaceCleanupCliResult) -> str:
    """Render a deterministic human report; mutation facts come from receipt.

    The report summarises by reason instead of enumerating every run: a real
    workspace holds thousands of references, and per-run detail lives in the
    plan object and the durable receipt.
    """
    plan = result.plan
    lines = [
        f"Workspace: {result.workspace}",
        f"Runs dir: {result.runs_dir}",
        f"Tier: {result.tier}",
        f"Disposition: {result.disposition or 'report (no changes)'}",
        f"Run-root cutoff: {result.older_than_days} days",
        f"Reclaimable: {len(plan.selected)}",
        f"Protected: {len(plan.protected)}",
        f"Nothing to reclaim: {len(plan.inert)}",
        f"Eligible run roots: {len(plan.root_selected)}",
        f"Protected run roots: {len(plan.root_protected)}",
    ]
    lines += _reason_summary("reclaimable", plan.selected)
    lines += _reason_summary("protected", plan.protected)
    lines += _reason_summary("eligible run root", plan.root_selected)
    lines += _reason_summary("protected run root", plan.root_protected)
    if result.execution is not None:
        receipt = result.execution.receipt
        lines.extend([
            f"Status: {receipt['status']}",
            f"Bytes selected: {receipt['bytes_selected']}",
            f"Bytes archived: {receipt['bytes_archived']}",
            f"Bytes reclaimed: {receipt['bytes_reclaimed']}",
            f"Receipt: {result.execution.receipt_path}",
        ])
        for row in receipt["results"]:
            if row.get("archive_path"):
                lines.append(f"Archive: {row['archive_path']}")
        for error in receipt["errors"]:
            lines.append(f"Partial failure: {error}")
    return "\n".join(lines)


def _reason_summary(label: str, verdicts: tuple) -> list[str]:
    counts: dict[str, tuple[int, str]] = {}
    for verdict in verdicts:
        count, detail = counts.get(verdict.reason, (0, verdict.detail))
        counts[verdict.reason] = (count + 1, detail)
    return [
        f"  {label} {reason}: {count} — {detail}"
        for reason, (count, detail) in sorted(
            counts.items(), key=lambda item: (-item[1][0], item[0]),
        )
    ]
