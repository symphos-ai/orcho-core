"""Run history listing — recent runs with their meta projection."""
from __future__ import annotations

from pathlib import Path

from sdk._time import run_ts_to_datetime
from sdk.errors import NoWorkspace
from sdk.runs import _CWD_DEFAULT, find_runs_dir, load_meta
from sdk.types import RunSummary


def list_history(
    last: int | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> list[RunSummary]:
    """List the most recent runs (newest first).

    `last=None` returns every run; otherwise the first `last` rows.
    Returns an empty list when the runs directory exists but is empty
    — only `NoWorkspace` is raised when the directory itself can't be
    resolved.
    """
    rd = find_runs_dir(workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    if not rd.exists():
        raise NoWorkspace(f"Resolved runs dir does not exist: {rd}")

    dirs = sorted((d for d in rd.iterdir() if d.is_dir()), reverse=True)
    if last is not None:
        dirs = dirs[:last]

    out: list[RunSummary] = []
    for d in dirs:
        meta = load_meta(d)
        cross_aliases: tuple[str, ...] = ()
        project: str | None = None
        if meta:
            projects_dict = meta.get("projects")
            if isinstance(projects_dict, dict) and projects_dict:
                cross_aliases = tuple(projects_dict.keys())
            else:
                project = meta.get("project")

        out.append(
            RunSummary(
                run_id=d.name,
                run_dir=d,
                task=str((meta or {}).get("task", "")),
                status=(meta or {}).get("status"),
                project=project,
                cross_aliases=cross_aliases,
                timestamp=run_ts_to_datetime(d.name),
            )
        )
    return out
