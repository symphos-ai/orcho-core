"""Cost-reference aggregation across runs in a sliding window.

Pure aggregation — no print, no `sys.exit`. The CLI's
`format_cost_report` formatter consumes a `CostReport`; embedders
consume the same shape. Pricing fallback for codex-shaped phases (which
report tokens but no `cost_usd_equivalent`) goes through
`core.observability.pricing.estimate_cost_from_total`, and a
`priced_entries_count` field tracks how many entries got priced from
the snapshot so the formatter can mark them honestly.
"""
from __future__ import annotations

from pathlib import Path

from core.infra import config
from core.observability import pricing as _pricing
from sdk._time import parse_window, run_ts_to_datetime
from sdk.runs import _CWD_DEFAULT, find_runs_dir, load_json_optional, load_meta
from sdk.types import (
    AgentBreakdown,
    CostReport,
    CostRunRow,
    PhaseBreakdown,
)


def _provider_for_model(model: str) -> str:
    m = model.lower()
    if m.startswith("claude"):
        return "claude"
    if m.startswith(("gpt", "o3", "o4", "codex")):
        return "codex"
    if m.startswith("gemini"):
        return "gemini"
    return "other"


def _workspace_from_runs_dir(runs_dir: Path) -> Path | None:
    if runs_dir.name == "runs" and runs_dir.parent.name == "runspace":
        return runs_dir.parent.parent
    return None


def _accounting_enabled_for_context(
    *,
    workspace: Path | str | None,
    runs_dir: Path,
) -> bool:
    resolved_workspace = Path(workspace).expanduser() if workspace else None
    if resolved_workspace is None:
        resolved_workspace = _workspace_from_runs_dir(runs_dir)
    if resolved_workspace is not None:
        return config.accounting_enabled_for_workspace(resolved_workspace)
    return config.accounting_enabled()


def aggregate_cost(
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
    window: str = "30d",
    top_n: int = 5,
) -> CostReport:
    """Aggregate cost-reference values across runs whose timestamps fall
    within `window`.

    Window strings: ``"30d"`` / ``"7d"`` / ``"all"``. Returns a fully-
    populated `CostReport` even when no runs match — `total_runs == 0`
    in that case and the formatter renders the corresponding empty
    state.
    """
    rd = find_runs_dir(workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    cutoff = parse_window(window)
    use_accounting = _accounting_enabled_for_context(
        workspace=workspace,
        runs_dir=rd,
    )

    rows: list[CostRunRow] = []
    raw_phases: list[tuple[str, dict]] = []  # (run_id, phases dict) for second pass

    for d in sorted((d for d in rd.iterdir() if d.is_dir()), reverse=True):
        ts = run_ts_to_datetime(d.name)
        if cutoff is not None and ts is not None and ts < cutoff:
            continue
        m = load_json_optional(d / "metrics.json")
        if not m:
            continue
        meta = load_meta(d)
        rows.append(
            CostRunRow(
                run_id=d.name,
                task=str(meta.get("task", ""))[:60],
                cost=(
                    float(m.get("total_cost_usd_equivalent", 0.0) or 0.0)
                    if use_accounting else 0.0
                ),
                tokens=int(m.get("total_tokens", 0) or 0),
                tokens_in=int(m.get("total_tokens_in", 0) or 0),
                tokens_out=int(m.get("total_tokens_out", 0) or 0),
                duration_s=float(m.get("total_duration_s", 0.0) or 0.0),
                rounds=int(m.get("total_rounds", 0) or 0),
                retries=int(m.get("total_retries", 0) or 0),
                cost_estimated=bool(m.get("cost_estimated")),
            )
        )
        raw_phases.append((d.name, m.get("phases", {}) or {}))

    if not rows:
        return CostReport(
            runs_dir=rd,
            window=window,
            cutoff=cutoff,
            total_runs=0,
            total_cost=0.0,
            total_tokens=0,
            total_tokens_in=0,
            total_tokens_out=0,
            total_duration_s=0.0,
            rows=(),
            top_runs=(),
            phase_breakdown=(),
            agent_breakdown=(),
            priced_entries_count=0,
            pricing_source=None,
            pricing_snapshot_date=None,
            pricing_snapshot_age_days=_pricing.snapshot_age_days(),
            any_estimated=False,
            accounting_enabled=use_accounting,
        )

    total_cost = sum(r.cost for r in rows)
    total_tokens = sum(r.tokens for r in rows)
    total_tokens_in = sum(r.tokens_in for r in rows)
    total_tokens_out = sum(r.tokens_out for r in rows)
    total_dur = sum(r.duration_s for r in rows)

    phase_costs: dict[str, float] = {}
    phase_tokens: dict[str, int] = {}
    phase_runs: dict[str, int] = {}
    phase_exact: dict[str, bool] = {}
    phase_cost_estimated: dict[str, bool] = {}

    agent_costs: dict[str, float] = {}
    agent_tokens: dict[str, int] = {}
    agent_runs: dict[str, int] = {}
    agent_exact: dict[str, bool] = {}
    agent_cost_estimated: dict[str, bool] = {}

    priced_entries_count = 0

    for _run_id, phases in raw_phases:
        for ph_name, ph in phases.items():
            raw_cost = ph.get("cost_usd_equivalent", None) if use_accounting else None
            tokens = int(ph.get("total_tokens", 0) or 0)
            exact = bool(ph.get("tokens_exact", False))
            model = str(ph.get("model", ""))

            if use_accounting and raw_cost is None and exact and tokens > 0 and model:
                priced = _pricing.estimate_cost_from_total(model, tokens)
                cost = float(priced) if priced is not None else 0.0
                cost_estimated = priced is not None
                if priced is not None:
                    priced_entries_count += 1
            else:
                cost = float(raw_cost or 0.0)
                cost_estimated = bool(ph.get("cost_estimated"))

            phase_costs[ph_name] = phase_costs.get(ph_name, 0.0) + cost
            phase_runs[ph_name] = phase_runs.get(ph_name, 0) + 1
            phase_tokens[ph_name] = phase_tokens.get(ph_name, 0) + tokens
            phase_exact[ph_name] = phase_exact.get(ph_name, True) and exact
            phase_cost_estimated[ph_name] = (
                phase_cost_estimated.get(ph_name, False) or cost_estimated
            )

            # Agent-breakdown identity: prefer the recorded runtime id when the
            # phase carries one (so a wrapper runtime such as ``claude-glm`` is
            # its own row, not collapsed into ``claude``). Fall back to
            # model→provider only for legacy phases written without ``runtime``.
            runtime_id = str(ph.get("runtime") or "").strip() or _provider_for_model(model)
            agent_costs[runtime_id] = agent_costs.get(runtime_id, 0.0) + cost
            agent_tokens[runtime_id] = agent_tokens.get(runtime_id, 0) + tokens
            agent_runs[runtime_id] = agent_runs.get(runtime_id, 0) + 1
            agent_exact[runtime_id] = agent_exact.get(runtime_id, True) and exact
            agent_cost_estimated[runtime_id] = (
                agent_cost_estimated.get(runtime_id, False) or cost_estimated
            )

    phase_breakdown = tuple(
        PhaseBreakdown(
            name=name,
            cost=phase_costs[name],
            tokens=phase_tokens.get(name, 0),
            runs=phase_runs.get(name, 0),
            tokens_exact=phase_exact.get(name, False),
            cost_estimated=phase_cost_estimated.get(name, False),
        )
        for name in sorted(phase_costs, key=lambda k: phase_costs[k], reverse=True)
    )
    agent_breakdown = tuple(
        AgentBreakdown(
            provider=runtime_id,
            cost=agent_costs[runtime_id],
            tokens=agent_tokens.get(runtime_id, 0),
            runs=agent_runs.get(runtime_id, 0),
            tokens_exact=agent_exact.get(runtime_id, False),
            cost_estimated=agent_cost_estimated.get(runtime_id, False),
        )
        for runtime_id in sorted(agent_costs, key=lambda k: agent_costs[k], reverse=True)
    )

    sorted_rows = sorted(rows, key=lambda r: (r.cost, r.tokens), reverse=True)
    top_runs = tuple(sorted_rows[: max(top_n, 0)])

    snap_user = _pricing.user_snapshot_date()
    snap_bundled = _pricing.snapshot_date()
    if priced_entries_count and snap_user:
        pricing_source = "user"
        pricing_snapshot_date = snap_user.isoformat()
    elif priced_entries_count and snap_bundled:
        pricing_source = "bundled"
        pricing_snapshot_date = snap_bundled.isoformat()
    else:
        pricing_source = None
        pricing_snapshot_date = None

    any_estimated = any(r.cost_estimated for r in rows) or any(
        pb.cost_estimated for pb in phase_breakdown
    ) or any(
        ab.cost_estimated for ab in agent_breakdown
    )

    return CostReport(
        runs_dir=rd,
        window=window,
        cutoff=cutoff,
        total_runs=len(rows),
        total_cost=total_cost,
        total_tokens=total_tokens,
        total_tokens_in=total_tokens_in,
        total_tokens_out=total_tokens_out,
        total_duration_s=total_dur,
        rows=tuple(rows),
        top_runs=top_runs,
        phase_breakdown=phase_breakdown,
        agent_breakdown=agent_breakdown,
        priced_entries_count=priced_entries_count,
        pricing_source=pricing_source,
        pricing_snapshot_date=pricing_snapshot_date,
        pricing_snapshot_age_days=_pricing.snapshot_age_days(),
        any_estimated=any_estimated,
        accounting_enabled=use_accounting,
    )


__all__ = ["aggregate_cost"]
