"""Shared runspace context helpers for read-only SDK views."""
from __future__ import annotations

from pathlib import Path

from core.infra import config


def workspace_from_runs_dir(runs_dir: Path) -> Path | None:
    """Infer the workspace directory from a standard ``runspace/runs`` path."""
    if runs_dir.name == "runs" and runs_dir.parent.name == "runspace":
        return runs_dir.parent.parent
    return None


def accounting_enabled_for_context(
    *,
    workspace: Path | str | None,
    runs_dir: Path,
) -> bool:
    """Resolve accounting with the same workspace layer as the run lookup."""
    resolved_workspace = Path(workspace).expanduser() if workspace else None
    if resolved_workspace is None:
        resolved_workspace = workspace_from_runs_dir(runs_dir)
    if resolved_workspace is not None:
        return config.accounting_enabled_for_workspace(resolved_workspace)
    return config.accounting_enabled()


__all__ = [
    "accounting_enabled_for_context",
    "workspace_from_runs_dir",
]
