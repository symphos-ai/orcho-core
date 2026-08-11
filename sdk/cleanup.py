"""Typed public workspace-cleanup report and reclaim operations.

Reporting is read-only.  Reclaiming is explicitly tiered and creates the
engine's durable receipt before it changes a checkout or a run root.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pipeline.engine.workspace_cleanup import (
    execute_workspace_cleanup,
    select_workspace_cleanup,
)
from sdk.runs import _CWD_DEFAULT, find_runs_dir


@dataclass(frozen=True, slots=True)
class WorkspaceCleanupReasonSummary:
    """Deterministic count for one engine cleanup reason and its detail."""

    reason: str
    count: int
    detail: str


@dataclass(frozen=True, slots=True)
class WorkspaceCleanupReport:
    """Read-only cleanup selection projection for a resolved runs directory."""

    runs_dir: Path
    reclaimable_count: int
    protected_count: int
    inert_count: int
    reclaimable_reasons: tuple[WorkspaceCleanupReasonSummary, ...]
    protected_reasons: tuple[WorkspaceCleanupReasonSummary, ...]
    inert_reasons: tuple[WorkspaceCleanupReasonSummary, ...]
    reclaimable_run_root_count: int
    protected_run_root_count: int
    reclaimable_run_root_reasons: tuple[WorkspaceCleanupReasonSummary, ...]
    protected_run_root_reasons: tuple[WorkspaceCleanupReasonSummary, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceCleanupReceipt:
    """Immutable projection of the engine's final durable cleanup receipt."""

    receipt_path: Path
    tier: Literal["worktrees", "both"]
    disposition: Literal["archive", "delete"]
    status: str
    bytes_selected: int
    bytes_archived: int
    bytes_reclaimed: int
    error_count: int
    errors: tuple[str, ...]
    archive_paths: tuple[Path, ...]


def report_workspace_cleanup(
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
    older_than: timedelta | None = None,
    force: bool = False,
) -> WorkspaceCleanupReport:
    """Return a read-only cleanup selection report without writing anything."""
    resolved_runs_dir = find_runs_dir(workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    kwargs = {} if older_than is None else {"older_than": older_than}
    if force:
        kwargs["force"] = True
    plan = select_workspace_cleanup(resolved_runs_dir, **kwargs)
    return WorkspaceCleanupReport(
        runs_dir=resolved_runs_dir,
        reclaimable_count=len(plan.selected),
        protected_count=len(plan.protected),
        inert_count=len(plan.inert),
        reclaimable_reasons=_summaries(plan.selected),
        protected_reasons=_summaries(plan.protected),
        inert_reasons=_summaries(plan.inert),
        reclaimable_run_root_count=len(plan.root_selected),
        protected_run_root_count=len(plan.root_protected),
        reclaimable_run_root_reasons=_summaries(plan.root_selected),
        protected_run_root_reasons=_summaries(plan.root_protected),
    )


def reclaim_workspace_cleanup(
    *,
    tier: Literal["worktrees", "both"],
    disposition: Literal["archive", "delete"],
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
    older_than: timedelta | None = None,
    force: bool = False,
) -> WorkspaceCleanupReceipt:
    """Perform explicit cleanup and return facts copied from its durable receipt."""
    resolved_runs_dir = find_runs_dir(workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    kwargs = {} if older_than is None else {"older_than": older_than}
    if force:
        kwargs["force"] = True
    execution = execute_workspace_cleanup(
        resolved_runs_dir, tier=tier, disposition=disposition, **kwargs
    )
    receipt = execution.receipt
    errors = tuple(str(error) for error in receipt.get("errors", ()))
    archive_paths = tuple(
        Path(row["archive_path"])
        for row in receipt.get("results", ())
        if isinstance(row, dict) and row.get("archive_path")
    )
    return WorkspaceCleanupReceipt(
        receipt_path=execution.receipt_path,
        tier=receipt["tier"],
        disposition=receipt["disposition"],
        status=receipt["status"],
        bytes_selected=receipt["bytes_selected"],
        bytes_archived=receipt["bytes_archived"],
        bytes_reclaimed=receipt["bytes_reclaimed"],
        error_count=len(errors),
        errors=errors,
        archive_paths=archive_paths,
    )


def _summaries(verdicts: tuple) -> tuple[WorkspaceCleanupReasonSummary, ...]:
    counts: dict[str, tuple[int, str]] = {}
    for verdict in verdicts:
        count, detail = counts.get(verdict.reason, (0, verdict.detail))
        counts[verdict.reason] = (count + 1, detail)
    return tuple(
        WorkspaceCleanupReasonSummary(reason, count, detail)
        for reason, (count, detail) in sorted(
            counts.items(), key=lambda item: (-item[1][0], item[0])
        )
    )


__all__ = [
    "WorkspaceCleanupReasonSummary",
    "WorkspaceCleanupReport",
    "WorkspaceCleanupReceipt",
    "report_workspace_cleanup",
    "reclaim_workspace_cleanup",
]
