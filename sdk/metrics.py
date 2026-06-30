"""Per-run and historical metrics."""
from __future__ import annotations

from pathlib import Path

from core.observability import metrics as _core_metrics
from core.observability.metrics import accounting_enabled, scrub_accounting_fields
from sdk.runs import (
    _CWD_DEFAULT,
    find_run,
    find_runs_dir,
    load_json_optional,
)
from sdk.types import RunMetrics


def _build_run_metrics(run_dir: Path) -> RunMetrics:
    raw = load_json_optional(run_dir / "metrics.json")
    if not accounting_enabled():
        raw = scrub_accounting_fields(raw)
    return RunMetrics(
        run_id=run_dir.name,
        run_dir=run_dir,
        total_tokens=int(raw.get("total_tokens", 0) or 0),
        total_tokens_in=int(raw.get("total_tokens_in", 0) or 0),
        total_tokens_out=int(raw.get("total_tokens_out", 0) or 0),
        total_duration_s=float(raw.get("total_duration_s", 0.0) or 0.0),
        total_rounds=int(raw.get("total_rounds", 0) or 0),
        total_retries=int(raw.get("total_retries", 0) or 0),
        total_cost_usd_equivalent=float(raw.get("total_cost_usd_equivalent", 0.0) or 0.0),
        phases=dict(raw.get("phases", {}) or {}),
        raw=raw,
    )


def get_run_metrics(
    run_id: str,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> RunMetrics:
    """Load metrics for a specific run.

    Raises `RunNotFound` when the id has no run dir; `NoWorkspace`
    when the runs directory itself can't be resolved. The returned
    `RunMetrics.raw` carries the full `metrics.json` for embedders
    that need fields the SDK hasn't promoted yet.
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    return _build_run_metrics(ref.run_dir)


def list_metrics(
    last: int,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> list[RunMetrics]:
    """Return the most recent `last` runs as `RunMetrics` rows.

    Wraps `core.observability.metrics.load_historical_runs` for the
    historical scan; reuses the SDK's `RunMetrics` dataclass for the
    return shape (fields are a strict superset of the legacy
    `RunSummary`). Runs without a `metrics.json` are skipped.
    """
    rd = find_runs_dir(workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    legacy = _core_metrics.load_historical_runs(rd, last_n=last)
    out: list[RunMetrics] = []
    for s in legacy:
        run_dir = rd / s.run_id
        # Re-load to pick up tokens_in/out and phases that the legacy
        # `RunSummary` doesn't carry.
        out.append(_build_run_metrics(run_dir))
    return out


__all__ = ["get_run_metrics", "list_metrics"]
