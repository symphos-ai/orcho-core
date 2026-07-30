"""Thin CLI adapter and renderer for ``orcho workspace cleanup``."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal

from sdk.cleanup import (
    WorkspaceCleanupReasonSummary,
    WorkspaceCleanupReceipt,
    WorkspaceCleanupReport,
    reclaim_workspace_cleanup,
    report_workspace_cleanup,
)


@dataclass(frozen=True, slots=True)
class WorkspaceCleanupCliResult:
    workspace: Path
    runs_dir: Path
    tier: Literal["report", "worktrees", "both"]
    disposition: Literal["archive", "delete"] | None
    older_than_days: int
    report: WorkspaceCleanupReport
    receipt: WorkspaceCleanupReceipt | None


def workspace_cleanup_from_args(args: object) -> WorkspaceCleanupCliResult:
    """Resolve the workspace, select, and optionally execute a cleanup tier."""
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
        report = report_workspace_cleanup(
            workspace=getattr(args, "workspace", None), older_than=older_than
        )
        return WorkspaceCleanupCliResult(
            report.runs_dir.parent.parent, report.runs_dir, tier, None, older_than_days, report, None,
        )
    disposition: Literal["archive", "delete"] = "delete" if delete else "archive"
    # Keep the selection that the operator requested visible beside the
    # execution receipt.  Re-selecting after mutation would turn reclaimed
    # historical paths into a misleading post-cleanup protection report.
    report = report_workspace_cleanup(
        workspace=getattr(args, "workspace", None), older_than=older_than
    )
    receipt = reclaim_workspace_cleanup(
        tier="both" if tier == "both" else "worktrees",
        disposition=disposition,
        workspace=getattr(args, "workspace", None),
        older_than=older_than,
    )
    return WorkspaceCleanupCliResult(
        report.runs_dir.parent.parent,
        report.runs_dir,
        tier,
        disposition,
        older_than_days,
        report,
        receipt,
    )


def format_workspace_cleanup(result: WorkspaceCleanupCliResult) -> str:
    """Render a deterministic human report; mutation facts come from receipt.

    The report summarises by reason instead of enumerating every run: a real
    workspace holds thousands of references, and per-run detail lives in the
    SDK report and the durable receipt.
    """
    report = result.report
    lines = [
        f"Workspace: {result.workspace}",
        f"Runs dir: {result.runs_dir}",
        f"Tier: {result.tier}",
        f"Disposition: {result.disposition or 'report (no changes)'}",
        f"Run-root cutoff: {result.older_than_days} days",
        f"Reclaimable: {report.reclaimable_count}",
        f"Protected: {report.protected_count}",
        f"Nothing to reclaim: {report.inert_count}",
        f"Eligible run roots: {report.reclaimable_run_root_count}",
        f"Protected run roots: {report.protected_run_root_count}",
    ]
    lines += _reason_summary("reclaimable", report.reclaimable_reasons)
    lines += _reason_summary("protected", report.protected_reasons)
    lines += _reason_summary("eligible run root", report.reclaimable_run_root_reasons)
    lines += _reason_summary("protected run root", report.protected_run_root_reasons)
    if result.receipt is not None:
        receipt = result.receipt
        lines.extend([
            f"Status: {receipt.status}",
            f"Bytes selected: {receipt.bytes_selected}",
            f"Bytes archived: {receipt.bytes_archived}",
            f"Bytes reclaimed: {receipt.bytes_reclaimed}",
            f"Receipt: {receipt.receipt_path}",
        ])
        for archive_path in receipt.archive_paths:
            lines.append(f"Archive: {archive_path}")
        for error in receipt.errors:
            lines.append(f"Partial failure: {error}")
    return "\n".join(lines)


def _reason_summary(
    label: str, summaries: tuple[WorkspaceCleanupReasonSummary, ...]
) -> list[str]:
    return [
        f"  {label} {summary.reason}: {summary.count} — {summary.detail}"
        for summary in summaries
    ]
