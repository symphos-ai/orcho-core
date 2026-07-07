"""`aggregate_cost` aggregation against synthetic runs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdk import aggregate_cost
from sdk.types import CostReport


def _write_costed_run(runs_dir: Path) -> None:
    run_dir = runs_dir / "20260507_120000"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps({"task": "costed run"}),
        encoding="utf-8",
    )
    (run_dir / "metrics.json").write_text(
        json.dumps(
            {
                "total_tokens": 100,
                "total_tokens_in": 70,
                "total_tokens_out": 30,
                "total_duration_s": 1.0,
                "total_cost_usd_equivalent": 1.23,
                "phases": {
                    "plan": {
                        "model": "claude-sonnet-4-6",
                        "total_tokens": 100,
                        "tokens_exact": True,
                        "cost_usd_equivalent": 1.23,
                    },
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def accounting_on(monkeypatch: pytest.MonkeyPatch):
    from core.infra import config
    monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
    config._reset_config()
    yield
    config._reset_config()


def test_empty_runs_dir_returns_zero_report(runs_root: Path):
    report = aggregate_cost(runs_dir=runs_root, window="all")
    assert isinstance(report, CostReport)
    assert report.total_runs == 0
    assert report.total_cost == 0.0
    assert report.rows == ()
    assert report.phase_breakdown == ()


def test_basic_aggregation(populated_runs: Path, accounting_on):
    report = aggregate_cost(runs_dir=populated_runs, window="all", top_n=2)
    # Cross run has no metrics phases → 2 single-project rows surface in totals
    # (cross run still shows up because it has metrics.json).
    assert report.total_runs == 3
    # Two phases with explicit cost contribute 0.42; codex run may add a
    # priced fallback if pricing snapshot is populated, otherwise 0.
    assert report.total_cost == pytest.approx(0.42)
    # Top runs sorted by cost desc → biggest claude-cost run first.
    assert report.top_runs[0].run_id == "20260507_120000"
    # By-phase: claude phases were tokens_exact=True.
    plan = next(p for p in report.phase_breakdown if p.name == "plan")
    assert plan.runs >= 1
    # By-agent — claude provider should always be present here.
    providers = {a.provider for a in report.agent_breakdown}
    assert "claude" in providers


def test_cost_estimated_source_survives_aggregation(
    runs_root: Path,
    accounting_on,
) -> None:
    rid = runs_root / "20260508_120000"
    rid.mkdir()
    (rid / "meta.json").write_text(json.dumps({"task": "estimated"}))
    (rid / "metrics.json").write_text(
        json.dumps(
            {
                "total_tokens": 100,
                "total_tokens_in": 70,
                "total_tokens_out": 30,
                "total_duration_s": 1.0,
                "total_cost_usd_equivalent": 0.12,
                "cost_estimated": True,
                "phases": {
                    "plan": {
                        "model": "gpt-5.5",
                        "total_tokens": 100,
                        "tokens_exact": True,
                        "cost_usd_equivalent": 0.12,
                        "cost_estimated": True,
                    },
                },
            }
        )
    )

    report = aggregate_cost(runs_dir=runs_root, window="all")

    assert report.any_estimated is True
    assert report.top_runs[0].cost_estimated is True
    assert report.phase_breakdown[0].cost_estimated is True
    assert report.agent_breakdown[0].cost_estimated is True


def test_workspace_accounting_applies_to_explicit_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from core.infra import config

    monkeypatch.delenv("ORCHO_ACCOUNTING", raising=False)
    monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
    monkeypatch.delenv("ORCHO_DISABLE_LOCAL_CONFIG", raising=False)
    config._reset_config()

    workspace = tmp_path / "workspace-orchestrator"
    runs_dir = workspace / "runspace" / "runs"
    runs_dir.mkdir(parents=True)
    (workspace / ".orcho").mkdir()
    (workspace / ".orcho" / "config.local.json").write_text(
        json.dumps({"accounting": {"enabled": True}}),
        encoding="utf-8",
    )
    _write_costed_run(runs_dir)

    try:
        report = aggregate_cost(workspace=workspace, window="all")
        assert report.accounting_enabled is True
        assert report.total_cost == pytest.approx(1.23)
    finally:
        config._reset_config()


def test_workspace_accounting_applies_to_walkup_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from core.infra import config

    monkeypatch.delenv("ORCHO_ACCOUNTING", raising=False)
    monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
    # Also clear ORCHO_RUNSPACE: find_runs_dir() resolves it (as
    # $ORCHO_RUNSPACE/runs) ahead of cwd walk-up, so an ambient value — e.g.
    # when this suite runs inside an Orcho-managed worktree — would shadow the
    # walk-up path this test exercises and point aggregation at the real
    # runspace. Deleting it keeps the walk-up resolution hermetic.
    monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
    monkeypatch.delenv("ORCHO_DISABLE_LOCAL_CONFIG", raising=False)
    config._reset_config()

    workspace = tmp_path / "workspace-orchestrator"
    project = tmp_path / "orcho-core"
    project.mkdir()
    runs_dir = workspace / "runspace" / "runs"
    runs_dir.mkdir(parents=True)
    (workspace / ".orcho").mkdir()
    (workspace / ".orcho" / "config.local.json").write_text(
        json.dumps({"accounting": {"enabled": True}}),
        encoding="utf-8",
    )
    _write_costed_run(runs_dir)

    try:
        report = aggregate_cost(cwd=project, window="all")
        assert report.accounting_enabled is True
        assert report.total_cost == pytest.approx(1.23)
    finally:
        config._reset_config()


def test_accounting_disabled_does_not_calculate_money(populated_runs: Path):
    report = aggregate_cost(runs_dir=populated_runs, window="all", top_n=2)
    assert report.accounting_enabled is False
    assert report.total_runs == 3
    assert report.total_cost == 0.0
    assert report.priced_entries_count == 0


def test_unknown_model_buckets_to_other(runs_root: Path, accounting_on):
    # Hand-craft a run with a model the provider mapper can't classify.
    import json
    rid = runs_root / "20260504_070000"
    rid.mkdir()
    (rid / "meta.json").write_text(json.dumps({"task": "weird", "project": "/x"}))
    (rid / "metrics.json").write_text(
        json.dumps(
            {
                "total_tokens": 1000,
                "total_duration_s": 1.0,
                "phases": {
                    "plan": {
                        "model": "mystery-llm-9000",
                        "total_tokens": 1000,
                        "tokens_exact": True,
                        "cost_usd_equivalent": 1.23,
                    }
                },
            }
        )
    )
    report = aggregate_cost(runs_dir=runs_root, window="all")
    providers = {a.provider for a in report.agent_breakdown}
    assert "other" in providers


def test_window_excludes_old_runs(populated_runs: Path):
    # All synthetic runs are dated 2026-05-{05..07}; today is 2026-05-09 per
    # the harness. A 1d window includes nothing.
    report = aggregate_cost(runs_dir=populated_runs, window="1d")
    assert report.total_runs == 0


def test_malformed_metrics_skipped(runs_root: Path):
    bad = runs_root / "20260507_999999"
    bad.mkdir()
    (bad / "metrics.json").write_text("{ not json")
    report = aggregate_cost(runs_dir=runs_root, window="all")
    assert report.total_runs == 0
