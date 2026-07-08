"""Snapshot test: `format_cost_report` reproduces the legacy
`cmd_cost` output sections against a synthetic `CostReport`.

Covers: empty state, top-N runs, by-phase, by-agent, totals,
pricing footer with snapshot age, and the `~` token marker for
estimated entries.
"""
from __future__ import annotations

import re
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
    ProjectBreakdown,
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
        any_estimated=False,
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
    assert "Top 2 runs by cost reference" in out
    assert "20260507_120000" in out
    assert "rounds×2, retries×1" in out
    # By-phase section
    assert "By phase (sum across runs)" in out
    assert "plan" in out and "implement" in out
    # By runtime/provider section + estimate marker
    assert "By runtime/provider (sum across phases)" in out
    assert "claude" in out
    assert "~" in out  # token-estimate marker for non-exact agent
    # Totals
    assert "Cost reference  runtime-reported $1.23" in out
    assert "not a billing receipt" in out
    assert "20,000" in out
    # Time formatting under 1h
    assert "Time            2.0m" in out


def test_cost_report_color_preserves_plain_contract():
    from core.io.ansi import C, get_color_enabled, set_color_enabled, strip_ansi

    r = _make_report(
        any_estimated=True,
        agent_breakdown=(
            AgentBreakdown(
                provider="claude-glm",
                cost=1.23,
                tokens=20_000,
                runs=4,
                tokens_exact=False,
            ),
        ),
    )
    before = get_color_enabled()
    try:
        set_color_enabled(False)
        plain = format_cost_report(r)
        set_color_enabled(True)
        colored = format_cost_report(r)
    finally:
        set_color_enabled(before)

    assert strip_ansi(colored) == plain
    assert C.CYAN in colored
    assert C.GREEN in colored
    assert C.GREY in colored


def test_glm_runtime_row_declares_quota_mode():
    r = _make_report(
        agent_breakdown=(
            AgentBreakdown(
                provider="claude-glm",
                cost=1.23,
                tokens=20_000,
                runs=4,
                tokens_exact=True,
            ),
        ),
    )
    out = format_cost_report(r)
    assert "claude-glm" in out
    assert "subscription/quota runtime; not API billing" in out


def test_sub_pipeline_rows_do_not_render_as_phases():
    r = _make_report(
        phase_breakdown=(
            PhaseBreakdown(
                name="plan",
                cost=1.0,
                tokens=100,
                runs=1,
                tokens_exact=True,
            ),
            PhaseBreakdown(
                name="api",
                cost=0.0,
                tokens=4_399,
                runs=1,
                tokens_exact=False,
                kind="sub_pipeline",
            ),
            PhaseBreakdown(
                name="web",
                cost=0.0,
                tokens=4_399,
                runs=1,
                tokens_exact=False,
                kind="sub_pipeline",
            ),
        ),
    )
    out = format_cost_report(r)
    phase_section = out.split("By phase (sum across runs):", 1)[1].split(
        "By role", 1,
    )[0]
    assert "api" not in phase_section
    assert "web" not in phase_section
    assert "By child pipeline" not in out
    assert not re.search(r"^\s+api\s", out, flags=re.MULTILINE)
    assert not re.search(r"^\s+web\s", out, flags=re.MULTILINE)


def test_workspace_project_breakdown_renders_project_units():
    r = _make_report(
        project_breakdown=(
            ProjectBreakdown(
                name="orcho-core",
                path="/workspace/orcho-core",
                cost=5.0,
                tokens=500,
                runs=2,
                tokens_exact=False,
                cost_estimated=True,
            ),
            ProjectBreakdown(
                name="orcho-mcp",
                path="/workspace/orcho-mcp",
                cost=4.0,
                tokens=400,
                runs=1,
                tokens_exact=True,
            ),
        ),
        phase_breakdown=(
            PhaseBreakdown(
                name="api",
                cost=10.0,
                tokens=10_000,
                runs=1,
                tokens_exact=True,
                kind="sub_pipeline",
            ),
            PhaseBreakdown(
                name="plan",
                cost=1.0,
                tokens=100,
                runs=1,
                tokens_exact=True,
            ),
        ),
        agent_breakdown=(),
    )

    out = format_cost_report(r)
    project_section = out.split("By workspace project", 1)[1].split(
        "By phase",
        1,
    )[0]
    assert "project runs + cross-project slices" in out
    assert "orcho-core" in project_section
    assert "estimated-api ~$5.00" in project_section
    assert "orcho-mcp" in project_section
    assert not re.search(r"^\s+api\s", project_section, flags=re.MULTILINE)


def test_role_and_task_slices_are_derived_from_phase_rows_only():
    r = _make_report(
        total_cost=16.0,
        phase_breakdown=(
            PhaseBreakdown(
                name="plan",
                cost=1.0,
                tokens=100,
                runs=1,
                tokens_exact=True,
            ),
            PhaseBreakdown(
                name="implement",
                cost=2.0,
                tokens=200,
                runs=1,
                tokens_exact=True,
            ),
            PhaseBreakdown(
                name="repair_changes",
                cost=3.0,
                tokens=300,
                runs=1,
                tokens_exact=False,
                cost_estimated=True,
            ),
            PhaseBreakdown(
                name="api",
                cost=10.0,
                tokens=10_000,
                runs=1,
                tokens_exact=True,
                kind="sub_pipeline",
            ),
        ),
        agent_breakdown=(),
    )

    out = format_cost_report(r)
    role_section = out.split("By role (derived from phase map):", 1)[1].split(
        "By task",
        1,
    )[0]
    task_section = out.split("By task (derived from phase map):", 1)[1].split(
        "Totals:",
        1,
    )[0]

    assert "implementation_engineer" in role_section
    assert "systems_architect" in role_section
    assert re.search(
        r"implementation_engineer\s+estimated-api ~\$5\.00\s+\(83\.3%\)\s+"
        r"×2\s+~\s*500 tok",
        role_section,
    )
    assert not re.search(r"^\s+api\s", role_section, flags=re.MULTILINE)

    assert "implement" in task_section
    assert "repair_changes" in task_section
    assert "plan" in task_section
    assert not re.search(r"^\s+api\s", task_section, flags=re.MULTILINE)


def test_accounting_disabled_report_has_no_dollar_output():
    r = _make_report(accounting_enabled=False, total_cost=0.0)
    out = format_cost_report(r)
    assert "Accounting is disabled" in out
    assert "Dollar estimates were not calculated." in out
    assert "$" not in out
    assert "Cost reference" not in out


def test_pricing_footer_when_priced_entries_present():
    r = _make_report(
        priced_entries_count=3,
        pricing_source="user",
        pricing_snapshot_date="2026-04-30",
        pricing_snapshot_age_days=10,
    )
    out = format_cost_report(r)
    assert "3 phase entries estimated from" in out
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
    assert "Cost reference  —" in out


def test_estimated_cost_report_marks_estimated_api_source():
    r = _make_report(
        any_estimated=True,
        top_runs=(
            CostRunRow(
                run_id="20260507_120000",
                task="Estimated run",
                cost=1.23,
                tokens=20_000,
                tokens_in=12_000,
                tokens_out=8_000,
                duration_s=120.0,
                rounds=1,
                retries=0,
                cost_estimated=True,
            ),
        ),
    )
    out = format_cost_report(r)
    assert "estimated-api ~$1.23" in out


def test_no_invoice_wording():
    """No billing-receipt phrasing anywhere; top-runs header stays 'cost reference'."""
    r = _make_report()
    out = format_cost_report(r)
    assert "expensive runs" not in out
    assert "API-equivalent" not in out
    assert "runs by cost reference" in out


def test_accounting_note_renders_exactly_once():
    r = _make_report()
    out = format_cost_report(r)
    assert out.count("not a billing receipt") == 1


def test_source_labels_on_breakdown_and_totals():
    """Mixed runtime-reported / estimated-api rows surface both source
    prefixes next to dollar values across top runs, phases, runtime/provider
    and the totals line."""
    r = _make_report(
        total_cost=2.0,
        any_estimated=True,
        top_runs=(
            CostRunRow(
                run_id="20260507_120000", task="Reported run", cost=1.0,
                tokens=100, tokens_in=50, tokens_out=50, duration_s=10.0,
                rounds=1, retries=0, cost_estimated=False,
            ),
            CostRunRow(
                run_id="20260506_090000", task="Estimated run", cost=1.0,
                tokens=100, tokens_in=50, tokens_out=50, duration_s=10.0,
                rounds=1, retries=0, cost_estimated=True,
            ),
        ),
        phase_breakdown=(
            PhaseBreakdown(name="plan", cost=1.0, tokens=100, runs=1,
                           tokens_exact=True, cost_estimated=False),
            PhaseBreakdown(name="implement", cost=1.0, tokens=100, runs=1,
                           tokens_exact=False, cost_estimated=True),
        ),
        agent_breakdown=(
            AgentBreakdown(provider="claude", cost=1.0, tokens=100, runs=1,
                           tokens_exact=True, cost_estimated=False),
            AgentBreakdown(provider="codex", cost=1.0, tokens=100, runs=1,
                           tokens_exact=False, cost_estimated=True),
        ),
    )
    out = format_cost_report(r)
    # Both source prefixes appear next to dollar values (top runs + phases +
    # runtime/provider carry one row each).
    assert "runtime-reported $" in out
    assert "estimated-api ~$" in out
    # Totals line carries a source label too.
    assert "Cost reference  estimated-api ~$2.00" in out


def test_breakdown_pct_share_never_exceeds_100():
    """Phase percentages are share-of-breakdown: each <=100 and they sum to
    ~100 even when the breakdown cost sum exceeds total_cost (which under the
    old total_cost denominator rendered as a broken pie)."""
    r = _make_report(
        total_cost=1.0,
        any_estimated=False,
        phase_breakdown=(
            PhaseBreakdown(name="plan", cost=0.80, tokens=100, runs=1,
                           tokens_exact=True),
            PhaseBreakdown(name="implement", cost=0.80, tokens=100, runs=1,
                           tokens_exact=True),
        ),
        agent_breakdown=(),
    )
    out = format_cost_report(r)
    phase_section = out.split("By phase (sum across runs):", 1)[1].split(
        "By role",
        1,
    )[0]
    pcts = [float(m) for m in re.findall(r"\(([\d.]+)%\)", phase_section)]
    assert pcts, "expected at least one phase percentage"
    assert all(p <= 100.0 for p in pcts)
    assert abs(sum(pcts) - 100.0) < 0.5


def test_breakdown_pct_suppressed_when_phase_sum_is_zero():
    """A zero phase-breakdown sum suppresses the % column entirely."""
    r = _make_report(
        total_cost=1.0,
        phase_breakdown=(
            PhaseBreakdown(name="plan", cost=0.0, tokens=100, runs=1,
                           tokens_exact=True),
        ),
        agent_breakdown=(),
    )
    out = format_cost_report(r)
    assert re.findall(r"\(([\d.]+)%\)", out) == []


def test_estimated_footer_does_not_name_runtime():
    """Estimated-entries footer identifies only estimated entries, names no
    runtime/model, yet keeps the pricing source and the stale warning."""
    r = _make_report(
        priced_entries_count=3,
        pricing_source="bundled",
        pricing_snapshot_date="2026-01-01",
        pricing_snapshot_age_days=120,
    )
    out = format_cost_report(r)
    assert "3 phase entries estimated from" in out
    assert "(codex)" not in out
    assert "Estimated cost (codex)" not in out
    assert "codex" not in out.split("phase entries estimated from")[1].splitlines()[0]
    # Stale warning + refresh hint are preserved.
    assert "120 days old" in out
    assert "orcho pricing refresh" in out
