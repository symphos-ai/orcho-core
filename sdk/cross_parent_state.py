"""Read-only SDK access to the canonical cross-parent reduction."""

from __future__ import annotations

from pathlib import Path

from pipeline.run_state.cross_parent import CrossParentState
from pipeline.run_state.cross_parent_disk import load_cross_parent_state as _load
from sdk.runs import _CWD_DEFAULT, find_run


def load_cross_parent_state(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> CrossParentState:
    """Load the immutable canonical state for one durable cross run.

    Context resolution exactly matches the other SDK readers.  The adapter only
    reads the resolved run directory and never persists a derived snapshot.
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    return _load(ref.run_dir)


__all__ = ["load_cross_parent_state"]
