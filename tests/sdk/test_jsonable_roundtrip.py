"""IPC-friendliness — every public SDK return type round-trips through JSON."""
from __future__ import annotations

import json
from pathlib import Path

from sdk import (
    aggregate_cost,
    list_history,
    list_metrics,
    list_prompts,
    load_status,
    show_pricing,
    to_jsonable,
)


def test_list_history_json(populated_runs: Path):
    rows = list_history(runs_dir=populated_runs)
    payload = to_jsonable(rows)
    json.dumps(payload)  # raises TypeError on bad shape


def test_load_status_json(populated_runs: Path):
    status = load_status(runs_dir=populated_runs)
    json.dumps(to_jsonable(status))


def test_list_metrics_json(populated_runs: Path):
    rows = list_metrics(last=10, runs_dir=populated_runs)
    json.dumps(to_jsonable(rows))


def test_aggregate_cost_json(populated_runs: Path):
    report = aggregate_cost(runs_dir=populated_runs, window="all", top_n=2)
    json.dumps(to_jsonable(report))


def test_show_pricing_json():
    table = show_pricing()
    json.dumps(to_jsonable(table))


def test_list_prompts_returns_jsonable():
    names = list_prompts()
    assert isinstance(names, list)
    json.dumps(to_jsonable(names))
