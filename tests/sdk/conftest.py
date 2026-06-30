"""Shared fixtures for SDK tests.

Builds a synthetic runs directory under `tmp_path` so SDK calls can be
exercised against deterministic state instead of an ad-hoc workspace.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def runs_root(tmp_path: Path) -> Path:
    """Create an empty `runspace/runs/` and return it.

    Nested under a dotted parent (`.ws/`) so sibling-scan walk-up from
    unrelated tests can't accidentally pick this directory as a "found"
    workspace — the walker skips dotted children at every level.
    """
    rd = tmp_path / ".ws" / "runspace" / "runs"
    rd.mkdir(parents=True)
    return rd


def _write_run(
    runs_dir: Path,
    run_id: str,
    *,
    meta: dict | None = None,
    metrics: dict | None = None,
) -> Path:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    if meta is not None:
        (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if metrics is not None:
        (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    return run_dir


@pytest.fixture
def populated_runs(runs_root: Path) -> Path:
    """Three runs with mixed shapes (claude, codex-no-cost, cross)."""
    _write_run(
        runs_root,
        "20260507_120000",
        meta={
            "project": "/tmp/projA",
            "task": "Add feature X",
            "status": "success",
            "profile": "advanced",
            "timestamp": "2026-05-07T12:00:00",
            "phases": {"plan": {}, "implement": {}},
        },
        metrics={
            "total_tokens": 12000,
            "total_tokens_in": 8000,
            "total_tokens_out": 4000,
            "total_duration_s": 60.0,
            "total_rounds": 1,
            "total_retries": 0,
            "total_cost_usd_equivalent": 0.42,
            "phases": {
                "plan": {
                    "model": "claude-sonnet-4-6",
                    "total_tokens": 5000,
                    "tokens_exact": True,
                    "cost_usd_equivalent": 0.12,
                },
                "implement": {
                    "model": "claude-sonnet-4-6",
                    "total_tokens": 7000,
                    "tokens_exact": True,
                    "cost_usd_equivalent": 0.30,
                },
            },
        },
    )
    _write_run(
        runs_root,
        "20260506_090000",
        meta={
            "project": "/tmp/projA",
            "task": "Codex feature Y",
            "status": "success",
            "profile": "advanced",
            "timestamp": "2026-05-06T09:00:00",
        },
        metrics={
            "total_tokens": 8000,
            "total_duration_s": 30.0,
            "phases": {
                "plan": {
                    "model": "gpt-4.1",
                    "total_tokens": 8000,
                    "tokens_exact": True,
                    # cost_usd_equivalent missing — pricing fallback path
                },
            },
        },
    )
    _write_run(
        runs_root,
        "20260505_080000",
        meta={
            "task": "Cross feature",
            "projects": {"unity": "/tmp/u", "api": "/tmp/a"},
            "status": "success",
            "timestamp": "2026-05-05T08:00:00",
        },
        metrics={"total_tokens": 100, "total_duration_s": 5.0, "phases": {}},
    )
    return runs_root
