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

from core.observability import pricing as _pricing
from sdk._runspace_context import (
    accounting_enabled_for_context,
    workspace_from_runs_dir,
)
from sdk._time import parse_window, run_ts_to_datetime
from sdk.runs import _CWD_DEFAULT, find_runs_dir, load_json_optional, load_meta
from sdk.types import (
    AgentBreakdown,
    CostReport,
    CostRunRow,
    PhaseBreakdown,
    ProjectBreakdown,
)


def _provider_for_model(model: str) -> str:
    m = model.lower()
    if m.startswith("glm"):
        return "claude-glm"
    if m.startswith("claude"):
        return "claude"
    if m.startswith(("gpt", "o3", "o4", "codex")):
        return "codex"
    if m.startswith("gemini"):
        return "gemini"
    return "other"


def _project_group_root_from_runs_dir(runs_dir: Path) -> Path | None:
    workspace = workspace_from_runs_dir(runs_dir)
    if workspace is None:
        return None
    if workspace.name == "workspace-orchestrator":
        return workspace.parent
    return workspace


def _resolved_project_identity(
    raw_path: object,
    *,
    project_group_root: Path | None,
) -> tuple[str, str] | None:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    try:
        project_path = Path(raw_path).expanduser().resolve(strict=False)
    except OSError:
        return None
    if project_group_root is not None:
        try:
            root = project_group_root.expanduser().resolve(strict=False)
        except OSError:
            return None
        if not project_path.is_relative_to(root):
            return None
    if not project_path.name:
        return None
    return project_path.name, str(project_path)


def _metrics_tokens_exact(phases: dict) -> bool:
    phase_values = [ph for ph in phases.values() if isinstance(ph, dict)]
    if not phase_values:
        return False
    return all(bool(ph.get("tokens_exact", False)) for ph in phase_values)


def _metrics_cost_estimated(phases: dict) -> bool:
    return any(
        bool(ph.get("cost_estimated"))
        for ph in phases.values()
        if isinstance(ph, dict)
    )


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
    use_accounting = accounting_enabled_for_context(
        workspace=workspace,
        runs_dir=rd,
    )

    rows: list[CostRunRow] = []
    raw_runs: list[tuple[CostRunRow, dict, dict]] = []

    for d in sorted((d for d in rd.iterdir() if d.is_dir()), reverse=True):
        ts = run_ts_to_datetime(d.name)
        if cutoff is not None and ts is not None and ts < cutoff:
            continue
        m = load_json_optional(d / "metrics.json")
        if not m:
            continue
        meta = load_meta(d)
        row = CostRunRow(
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
        rows.append(
            row
        )
        raw_runs.append((row, meta, m))

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
    phase_kinds: dict[str, str] = {}

    agent_costs: dict[str, float] = {}
    agent_tokens: dict[str, int] = {}
    agent_runs: dict[str, int] = {}
    agent_exact: dict[str, bool] = {}
    agent_cost_estimated: dict[str, bool] = {}

    project_group_root = _project_group_root_from_runs_dir(rd)
    project_names: dict[str, str] = {}
    project_costs: dict[str, float] = {}
    project_tokens: dict[str, int] = {}
    project_runs: dict[str, int] = {}
    project_exact: dict[str, bool] = {}
    project_cost_estimated: dict[str, bool] = {}

    priced_entries_count = 0

    def add_project_cost(
        *,
        identity: tuple[str, str] | None,
        cost: float,
        tokens: int,
        runs_count: int,
        tokens_exact: bool,
        cost_estimated: bool,
    ) -> None:
        if identity is None:
            return
        name, path = identity
        project_names[path] = name
        project_costs[path] = project_costs.get(path, 0.0) + cost
        project_tokens[path] = project_tokens.get(path, 0) + tokens
        project_runs[path] = project_runs.get(path, 0) + runs_count
        project_exact[path] = project_exact.get(path, True) and tokens_exact
        project_cost_estimated[path] = (
            project_cost_estimated.get(path, False) or cost_estimated
        )

    for row, meta, metrics in raw_runs:
        phases = metrics.get("phases", {}) or {}
        if not isinstance(phases, dict):
            phases = {}
        projects_map = meta.get("projects")
        if not isinstance(projects_map, dict):
            projects_map = {}

        if not projects_map:
            add_project_cost(
                identity=_resolved_project_identity(
                    meta.get("project"),
                    project_group_root=project_group_root,
                ),
                cost=row.cost,
                tokens=row.tokens,
                runs_count=1,
                tokens_exact=_metrics_tokens_exact(phases),
                cost_estimated=row.cost_estimated or _metrics_cost_estimated(phases),
            )

        for ph_name, ph in phases.items():
            raw_cost = ph.get("cost_usd_equivalent", None) if use_accounting else None
            tokens = int(ph.get("total_tokens", 0) or 0)
            exact = bool(ph.get("tokens_exact", False))
            model = str(ph.get("model", ""))
            kind = str(ph.get("kind") or "phase").strip() or "phase"

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
            previous_kind = phase_kinds.get(ph_name)
            phase_kinds[ph_name] = (
                kind if previous_kind in (None, kind) else "mixed"
            )
            if kind == "sub_pipeline":
                add_project_cost(
                    identity=_resolved_project_identity(
                        projects_map.get(ph_name),
                        project_group_root=project_group_root,
                    ),
                    cost=cost,
                    tokens=tokens,
                    runs_count=1,
                    tokens_exact=exact,
                    cost_estimated=cost_estimated,
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
            kind=phase_kinds.get(name, "phase"),
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
    project_breakdown = tuple(
        ProjectBreakdown(
            name=project_names[path],
            path=path,
            cost=project_costs[path],
            tokens=project_tokens.get(path, 0),
            runs=project_runs.get(path, 0),
            tokens_exact=project_exact.get(path, False),
            cost_estimated=project_cost_estimated.get(path, False),
        )
        for path in sorted(
            project_costs,
            key=lambda k: (project_costs[k], project_tokens[k]),
            reverse=True,
        )
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
        project_breakdown=project_breakdown,
    )


__all__ = ["aggregate_cost"]
