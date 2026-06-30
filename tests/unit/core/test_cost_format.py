"""Snapshot test: `format_cost_report` reproduces the legacy
`cmd_cost` output sections against a synthetic `CostReport`.

Covers: empty state, top-N runs, by-phase, by-agent, totals,
pricing footer with snapshot age, and the `~` token marker for
estimated entries.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from cli._formatters import format_cost_report, format_pricing
from sdk.types import (
    AgentBreakdown,
    CostReport,
    CostRunRow,
    PhaseBreakdown,
    PricingEntry,
    PricingTable,
)


def _make_report(**overrides) -> CostReport:
    base = dict(
        runs_dir=Path("/tmp/runs"),
        window="7d",
        cutoff=datetime(2026, 5, 2),
        total_runs=2,
        total_cost=1.23,
        total_tokens=20_000,
        total_tokens_in=12_000,
        total_tokens_out=8_000,
        total_duration_s=120.0,
        rows=(),
        top_runs=(
            CostRunRow(
                run_id="20260507_120000",
                task="Add feature X",
                cost=0.80,
                tokens=12_000,
                tokens_in=8_000,
                tokens_out=4_000,
                duration_s=80.0,
                rounds=2,
                retries=1,
            ),
            CostRunRow(
                run_id="20260506_090000",
                task="Refactor Y",
                cost=0.43,
                tokens=8_000,
                tokens_in=4_000,
                tokens_out=4_000,
                duration_s=40.0,
                rounds=1,
                retries=0,
            ),
        ),
        phase_breakdown=(
            PhaseBreakdown(
                name="plan", cost=0.50, tokens=10_000, runs=2, tokens_exact=True
            ),
            PhaseBreakdown(
                name="implement", cost=0.73, tokens=10_000, runs=2, tokens_exact=False
            ),
        ),
        agent_breakdown=(
            AgentBreakdown(
                provider="claude",
                cost=1.23,
                tokens=20_000,
                runs=4,
                tokens_exact=False,
            ),
        ),
        priced_entries_count=0,
        pricing_source=None,
        pricing_snapshot_date=None,
        pricing_snapshot_age_days=None,
        any_estimated=True,
    )
    base.update(overrides)
    return CostReport(**base)


def test_empty_report():
    r = _make_report(
        cutoff=None,
        total_runs=0,
        total_cost=0.0,
        total_tokens=0,
        total_tokens_in=0,
        total_tokens_out=0,
        total_duration_s=0.0,
        top_runs=(),
        phase_breakdown=(),
        agent_breakdown=(),
        any_estimated=False,
    )
    out = format_cost_report(r)
    assert out == "No runs found for all time in /tmp/runs"


def test_full_report_sections_present():
    r = _make_report()
    out = format_cost_report(r)
    # Header row
    assert "Cost report · window=7d · 2 runs · workspace=/tmp/runs" in out
    # Top-N section
    assert "Top 2 expensive runs" in out
    assert "20260507_120000" in out
    assert "rounds×2, retries×1" in out
    # By-phase section
    assert "By phase (sum across runs)" in out
    assert "plan" in out and "implement" in out
    # By-agent section + estimate marker
    assert "By agent (sum across phases)" in out
    assert "claude" in out
    assert "~" in out  # token-estimate marker for non-exact agent
    # Totals
    assert "API-equivalent  $1.23" in out
    assert "20,000" in out
    # Time formatting under 1h
    assert "Time            2.0m" in out


def test_accounting_disabled_report_has_no_dollar_output():
    r = _make_report(accounting_enabled=False, total_cost=0.0)
    out = format_cost_report(r)
    assert "Accounting is disabled" in out
    assert "Dollar estimates were not calculated." in out
    assert "$" not in out
    assert "API-equivalent" not in out


def test_pricing_footer_when_priced_entries_present():
    r = _make_report(
        priced_entries_count=3,
        pricing_source="user",
        pricing_snapshot_date="2026-04-30",
        pricing_snapshot_age_days=10,
    )
    out = format_cost_report(r)
    assert "3 entries priced from" in out
    assert "~/.orcho/pricing.local.toml" in out
    assert "2026-04-30" in out


def test_pricing_footer_warns_when_snapshot_stale():
    r = _make_report(
        priced_entries_count=1,
        pricing_source="bundled",
        pricing_snapshot_date="2026-01-01",
        pricing_snapshot_age_days=120,
    )
    out = format_cost_report(r)
    assert "120 days old" in out
    assert "orcho pricing refresh" in out


def test_pricing_show_explains_provider_cost_sources():
    table = PricingTable(
        entries=(
            PricingEntry(
                model="gpt-5.5",
                input_per_million=5.0,
                output_per_million=30.0,
                source="user",
            ),
        ),
        user_snapshot_date="2026-05-14",
        bundled_snapshot_date=None,
        snapshot_age_days=47,
        user_path=None,
        bundled_path=None,
    )

    out = format_pricing(table)

    assert "Provider cost notes" in out
    assert "Claude reports native cost" in out
    assert "OpenAI/Codex token-only runs" in out
    assert "Gemini provider-cost behavior is not assumed" in out


def test_top_phase_note_present_when_has_cost():
    r = _make_report()
    out = format_cost_report(r)
    assert "Top phase:" in out


def test_no_top_phase_note_when_total_cost_zero():
    r = _make_report(
        total_cost=0.0,
        phase_breakdown=(
            PhaseBreakdown(name="plan", cost=0.0, tokens=10, runs=1, tokens_exact=True),
        ),
        any_estimated=False,
    )
    out = format_cost_report(r)
    assert "Top phase" not in out
    assert "API-equivalent  —" in out
