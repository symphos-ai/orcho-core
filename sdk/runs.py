"""Run discovery — the entry point for every read/report SDK call.

`find_runs_dir` resolves the runs directory using an explicit context
triple (`workspace`, `runs_dir`, `cwd`), or falls back to env / walk-up
when none is given. The CLI passes `args.workspace` and lets the rest
default; embedders that don't want walk-up set `cwd=None` and pass
`workspace=` or `runs_dir=` explicitly.

Resolution order:

1. explicit ``runs_dir``
2. explicit ``workspace`` → ``workspace/runspace/runs``
3. ``$ORCHO_RUNSPACE/runs``
4. walk-up from ``cwd`` (with sibling-scan) — only when ``cwd`` is not
   ``None``. Walk-up beats ``$ORCHO_WORKSPACE`` because physical user
   presence inside a tree is a stronger context signal than a global
   env var (CLI: user sits in ``atas/bot_1`` while
   ``$ORCHO_WORKSPACE`` globally points at ``qcg`` → user expects
   ``atas`` runs, not ``qcg``).
5. ``$ORCHO_WORKSPACE/runspace/runs`` (engine resolver, fallback)

A `NoWorkspace` is raised when nothing resolves; `find_run` raises
`RunNotFound` when the requested id doesn't exist.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.infra import config
from sdk.errors import NoWorkspace, RunNotFound
from sdk.types import RunRef

_WALKUP_MAX_LEVELS = 6

# Sentinel: distinguishes "caller omitted cwd → walk-up from os.getcwd"
# from "caller passed cwd=None → walk-up disabled". Has a stable repr
# so the SDK schema snapshot is byte-deterministic across runs.
class _CwdDefault:
    __slots__ = ()

    def __repr__(self) -> str:
        return "_CWD_DEFAULT"


_CWD_DEFAULT: object = _CwdDefault()


def _coerce_path(value: Path | str | None) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser()


def _walkup_runs_dir(cwd: Path) -> Path | None:
    """Walk up from `cwd` looking for `runspace/runs/`.

    At each level checks:
    - `candidate/runspace/runs` — cwd inside the workspace itself
    - `candidate/<sibling>/runspace/runs` — cwd as a sibling of the
      workspace (common when the project repo and workspace-orchestrator
      sit side-by-side under one umbrella directory)

    Stops after `_WALKUP_MAX_LEVELS` to bound filesystem scans.
    """
    try:
        cwd = cwd.resolve()
    except OSError:
        return None

    for level, candidate in enumerate((cwd, *cwd.parents)):
        if level > _WALKUP_MAX_LEVELS:
            break
        runs = candidate / "runspace" / "runs"
        if runs.is_dir():
            return runs
        try:
            for sub in sorted(candidate.iterdir()):
                if not sub.is_dir() or sub.name.startswith("."):
                    continue
                sub_runs = sub / "runspace" / "runs"
                if sub_runs.is_dir():
                    return sub_runs
        except (OSError, PermissionError):
            continue
    return None


def find_runs_dir(
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> Path:
    """Resolve the runs directory.

    See module docstring for the full resolution order. Raises
    `NoWorkspace` when nothing resolves.

    `cwd` defaults to `Path.cwd()` resolved at *call time* — walk-up is
    enabled. Embedders that want strict no-walk-up behaviour pass
    `cwd=None` explicitly (the sentinel default and `None` are
    distinct: only `None` disables walk-up).
    """
    rd = _coerce_path(runs_dir)
    if rd is not None:
        if rd.is_dir():
            return rd
        raise NoWorkspace(f"Provided runs_dir does not exist: {rd}")

    ws = _coerce_path(workspace)
    if ws is not None:
        candidate = ws / "runspace" / "runs"
        if candidate.is_dir():
            return candidate
        raise NoWorkspace(
            f"Provided workspace has no runspace/runs/: {ws}"
        )

    if env_runspace := os.environ.get("ORCHO_RUNSPACE"):
        candidate = Path(env_runspace) / "runs"
        if candidate.is_dir():
            return candidate

    walk_up_enabled = cwd is not None
    if walk_up_enabled:
        cwd_path = Path.cwd() if cwd is _CWD_DEFAULT else _coerce_path(cwd)
        if cwd_path is not None:
            walked = _walkup_runs_dir(cwd_path)
            if walked is not None:
                return walked

    try:
        return config.get_runs_dir()
    except config.WorkspaceNotResolvedError:
        pass

    if not walk_up_enabled:
        raise NoWorkspace(
            "No workspace/runs_dir given and walk-up disabled (cwd=None)."
        )

    raise NoWorkspace(
        "Could not resolve runs directory from workspace, runs_dir, "
        "$ORCHO_RUNSPACE, walk-up from cwd, or $ORCHO_WORKSPACE."
    )


def find_run(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> RunRef:
    """Locate a run by id, or return the newest one when `run_id` is None.

    Raises `NoWorkspace` (no runs directory) or `RunNotFound` (the
    requested id doesn't exist or there are no runs at all).
    """
    rd = find_runs_dir(workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    if not rd.exists():
        raise NoWorkspace(f"Resolved runs dir does not exist: {rd}")

    if run_id:
        run_dir = rd / run_id
        if not run_dir.is_dir():
            raise RunNotFound(f"No run directory: {run_dir}")
        return RunRef(run_id=run_id, run_dir=run_dir)

    dirs = sorted((d for d in rd.iterdir() if d.is_dir()), reverse=True)
    if not dirs:
        raise RunNotFound(f"No runs in {rd}")
    return RunRef(run_id=dirs[0].name, run_dir=dirs[0])


def load_meta(run_dir: Path) -> dict[str, Any]:
    """Load `meta.json` from a run directory; returns `{}` on read errors.

    Tolerant by design: callers (status, history, cost) want to render
    a row even when one run's `meta.json` is missing or corrupt.
    """
    try:
        return json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_json_optional(path: Path) -> dict[str, Any]:
    """Tolerant JSON loader used by SDK readers; returns `{}` on failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
