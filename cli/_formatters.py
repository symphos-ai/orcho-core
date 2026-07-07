"""Pure formatters for CLI output.

Every function here takes typed SDK values (dataclasses) and returns a
`str`. They never call `print`, never write files, never raise. The
goal is byte-for-byte parity with the previous handler-embedded output
so the golden-output diff in REA-3.8's DoD passes whitespace-only.
"""
from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterable
from pathlib import Path

from core.io.ansi import C, paint
from core.observability.accounting_display import (
    ACCOUNTING_REFERENCE_NOTE,
    format_cost_reference,
)
from sdk import (
    CostReport,
    DetectedRuntime,
    EvidenceBundle,
    FineTuneResult,
    OrchoError,
    PricingTable,
    ProfileCustomizeResult,
    PromptResolution,
    RefreshResult,
    RunDiffRecord,
    RunMetrics,
    RunStatus,
    RunSummary,
    VerifyEnvResult,
    VerifyListResult,
    VerifyRunResult,
    WorkspaceInitResult,
)

# ─────────────────────────────────────────────────────────────────────────────
# status
# ─────────────────────────────────────────────────────────────────────────────


def _pending_handoff_id(status: RunStatus) -> str | None:
    """Current pending phase-handoff id for an awaiting run, else ``None``.

    Reads the durable ``meta.phase_handoff['id']`` — the same field
    ``sdk.phase_handoff.phase_handoff_decide`` validates against and the
    ``run_control`` snapshot surfaces as ``pending_action.handoff_id``. Only
    surfaced while the run is ``awaiting_phase_handoff`` so a stale payload on
    a resumed/terminal run is not mistaken for an open decision.
    """
    meta = status.meta
    if meta is None or meta.status != "awaiting_phase_handoff":
        return None
    payload = (status.raw_meta or {}).get("phase_handoff")
    if isinstance(payload, dict):
        handoff_id = payload.get("id")
        if isinstance(handoff_id, str) and handoff_id:
            return handoff_id
    return None


def format_status(status: RunStatus, *, verbose: bool = False) -> str:
    """Reproduce the previous `cmd_status` printer (cli/orcho.py:296-345)."""
    out: list[str] = []
    sep = "─" * 60
    out.append("")
    out.append(sep)
    out.append(f"  Run:     {status.run_ref.run_id}")
    out.append(sep)

    meta = status.meta
    if meta is not None:
        if meta.projects:
            out.append(f"  Project: [cross] {', '.join(meta.projects)}")
        else:
            out.append(f"  Project: {Path(meta.project or '?').name}")
        out.append(f"  Task:    {(meta.task or '?')[:80]}")
        out.append(f"  Status:  {meta.status or '?'}")
        # Pending phase-handoff id: only present while the run awaits an
        # operator decision. Names the exact id the operator must pass to
        # ``phase_handoff_decide`` / ``orcho run --resume`` so the current
        # round (e.g. ``review_changes:repair_round:2``) is unambiguous.
        pending_handoff = _pending_handoff_id(status)
        if pending_handoff is not None:
            out.append(f"  Pending handoff: {pending_handoff}")
        out.append(f"  Profile: {meta.profile or '?'}")
        out.append(f"  Time:    {meta.timestamp or '?'}")
        if meta.phases:
            out.append("")
            out.append(f"  Phases completed: {', '.join(meta.phases)}")

    if status.raw_metrics:
        out.append("")
        out.append(
            f"  Tokens:  {status.total_tokens:,} "
            f"(in={status.total_tokens_in:,} out={status.total_tokens_out:,})"
        )
        out.append(f"  Time:    {status.total_duration_s:.1f}s")
        if status.total_rounds:
            out.append(f"  Rounds:  {status.total_rounds}")
        if status.total_retries:
            out.append(f"  Retries: {status.total_retries}")

    if status.sub_projects:
        out.append("")
        out.append("  Projects:")
        for sp in status.sub_projects:
            out.append(f"    [{sp.name}]  status={sp.status or '?'}")

    out.append("")
    out.append(f"  Dir: {status.run_ref.run_dir}")

    if verbose and status.raw_meta:
        out.append("")
        out.append("  Detailed Meta:")
        out.append(f"  {'-' * 14}")
        meta_text = json.dumps(status.raw_meta, indent=2, ensure_ascii=False)
        for line in meta_text.split("\n"):
            out.append(f"    {line}")

    out.append(sep)
    out.append("")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# history
# ─────────────────────────────────────────────────────────────────────────────


def format_history(rows: list[RunSummary]) -> str:
    """Reproduce `cmd_history` (cli/orcho.py:877-893)."""
    if not rows:
        return "No runs found."

    out: list[str] = []
    out.append("")
    out.append(f"  {'Run ID':<22} {'Status':<20} {'Project':<22} Task")
    out.append(f"  {'─' * 82}")
    for r in rows:
        no_meta = (
            not r.cross_aliases
            and r.project is None
            and r.status is None
            and not r.task
        )
        if no_meta:
            project, status, task = "?", "?", "(no meta.json)"
        else:
            if r.cross_aliases:
                project = "[cross] " + ",".join(r.cross_aliases)
                if len(project) > 22:
                    project = project[:21] + "…"
            elif r.project:
                project = Path(r.project).name
            else:
                project = "?"
            status = r.status or "?"
            full_task = r.task or "?"
            task = full_task[:36] + ("…" if len(full_task) > 36 else "")
        out.append(f"  {r.run_id:<22} {status:<20} {project:<22} {task}")
    out.append("")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# metrics
# ─────────────────────────────────────────────────────────────────────────────


def format_metrics_run(m: RunMetrics) -> str:
    """Reproduce `_print_run_metrics` (cli/orcho.py:382-405)."""
    out: list[str] = []
    sep = "─" * 60
    out.append("")
    out.append(sep)
    out.append(f"  Metrics: {m.run_id}")
    out.append(sep)
    out.append(
        f"  Tokens:  {m.total_tokens:,} "
        f"(in={m.total_tokens_in:,} out={m.total_tokens_out:,})"
    )
    out.append(f"  Time:    {m.total_duration_s:.1f}s")
    if m.total_rounds:
        out.append(f"  Rounds:  {m.total_rounds}")
    if m.total_retries:
        out.append(f"  Retries: {m.total_retries}")

    if m.phases:
        out.append("")
        out.append(
            f"  {'Phase':<12} {'Model':<26} {'In':>8} {'Out':>8} "
            f"{'Total':>8} {'Time':>8}"
        )
        out.append(f"  {'─' * 76}")
        for phase, p in m.phases.items():
            out.append(
                f"  {phase:<12} {p.get('model', ''):<26} "
                f"{p.get('tokens_in', 0):>8,} {p.get('tokens_out', 0):>8,} "
                f"{p.get('total_tokens', 0):>8,} {p.get('duration_s', 0.0):>7.1f}s"
            )
    out.append(sep)
    out.append("")
    return "\n".join(out)


def format_metrics_history(rows: list[RunMetrics], *, runs_dir: Path | None = None) -> str:
    """Reproduce metrics-history block: prelude + table + totals.

    Mirrors `cmd_metrics` historical path (cli/orcho.py:370-378) plus
    `_print_totals` (lines 408-411). Table layout matches the legacy
    `core.observability.metrics.format_history_table`.
    """
    if not rows:
        if runs_dir is not None:
            return f"No runs with metrics found in {runs_dir}"
        return "No runs with metrics found."

    lines: list[str] = []
    lines.append("")
    lines.append(f"  Last {len(rows)} runs:")
    lines.append("")
    lines.append(
        f"{'Run ID':<20} {'Project':<22} {'Tokens':>8} {'Time':>8} {'Rnd':>4}  Task"
    )
    lines.append("-" * 82)
    for m in rows:
        project_raw = (m.raw.get("project") or m.raw.get("meta", {}).get("project")) if m.raw else None
        # Fallback: read meta.json sibling for the project name; cheap because
        # the run dir is already on disk.
        if not project_raw:
            try:
                meta_text = (m.run_dir / "meta.json").read_text(encoding="utf-8")
                project_raw = json.loads(meta_text).get("project")
            except (OSError, json.JSONDecodeError):
                project_raw = None
        project = Path(project_raw).name if project_raw else "?"
        try:
            meta_text = (m.run_dir / "meta.json").read_text(encoding="utf-8")
            task = json.loads(meta_text).get("task", "?")
        except (OSError, json.JSONDecodeError):
            task = "?"
        task_short = task[:32] + "…" if len(task) > 32 else task
        lines.append(
            f"{m.run_id:<20} {project:<22} "
            f"{m.total_tokens:>8,} {m.total_duration_s:>7.1f}s {m.total_rounds:>4}  "
            f"{task_short}"
        )

    total_tok = sum(m.total_tokens for m in rows)
    total_dur = sum(m.total_duration_s for m in rows)
    lines.append("")
    lines.append(
        f"  Total: {total_tok:,} tokens | {total_dur:.1f}s across {len(rows)} runs"
    )
    lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# cost
# ─────────────────────────────────────────────────────────────────────────────


def format_cost_report(report: CostReport) -> str:
    """Reproduce `cmd_cost` output (cli/orcho.py:715-856).

    Pure: takes a `CostReport`, returns the multi-section human report.
    """
    if report.total_runs == 0:
        scope = "all time" if report.cutoff is None else f"the last {report.window}"
        return f"No runs found for {scope} in {report.runs_dir}"

    if not report.accounting_enabled:
        window_label = "all time" if report.cutoff is None else report.window
        return "\n".join([
            "",
            f"  Accounting is disabled · window={window_label} · "
            f"{report.total_runs} runs · workspace={report.runs_dir}",
            f"  {'─' * 70}",
            "",
            "  Dollar estimates were not calculated.",
            "  Enable `accounting.enabled=true` in config.local.json or set "
            "`ORCHO_ACCOUNTING=1` to collect them.",
            "",
            f"  Tokens  {report.total_tokens:,} "
            f"(in={report.total_tokens_in:,} out={report.total_tokens_out:,})",
            f"  Runs    {report.total_runs}",
            "",
        ])

    out: list[str] = []
    has_cost = report.total_cost > 0.0
    window_label = "all time" if report.cutoff is None else report.window

    out.append("")
    out.append(
        f"  Cost report · window={window_label} · {report.total_runs} runs · "
        f"workspace={report.runs_dir}"
    )
    out.append(f"  {'─' * 70}")

    # Top-N runs by cost reference.
    if report.top_runs:
        out.append("")
        out.append(f"  Top {len(report.top_runs)} runs by cost reference:")
        for r in report.top_runs:
            cost_str = (
                format_cost_reference(r.cost, estimated=r.cost_estimated)
                if r.cost > 0
                else "    -- "
            )
            tags: list[str] = []
            if r.rounds > 1:
                tags.append(f"rounds×{r.rounds}")
            if r.retries:
                tags.append(f"retries×{r.retries}")
            tag_str = f"  ⚠ {', '.join(tags)}" if tags else ""
            out.append(
                f"    {r.run_id}  {cost_str}  {r.task:<50}{tag_str}"
            )

    # By-phase.
    if report.phase_breakdown:
        out.append("")
        out.append("  By phase (sum across runs):")
        for ph in report.phase_breakdown:
            tok_marker = " " if ph.tokens_exact else "~"
            cost_str = (
                format_cost_reference(ph.cost, estimated=ph.cost_estimated)
                if ph.cost > 0
                else "  (no $)"
            )
            if report.total_cost and ph.cost > 0:
                pct_str = f"({(ph.cost / report.total_cost * 100.0):>4.1f}%)"
            else:
                pct_str = "        "
            out.append(
                f"    {ph.name:<14} {cost_str}  {pct_str}   "
                f"×{ph.runs}   {tok_marker}{ph.tokens:>9,} tok"
            )

    # By-agent.
    if report.agent_breakdown:
        any_estimated = any(not a.tokens_exact for a in report.agent_breakdown)
        out.append("")
        out.append("  By agent (sum across phases):")
        for ag in report.agent_breakdown:
            pct = (ag.cost / report.total_cost * 100.0) if report.total_cost else 0.0
            tok_marker = " " if ag.tokens_exact else "~"
            cost_str = (
                format_cost_reference(ag.cost, estimated=ag.cost_estimated)
                if ag.cost > 0
                else "  (no $)"
            )
            pct_str = f"({pct:>4.1f}%)" if ag.cost > 0 else "        "
            out.append(
                f"    {ag.provider:<10} {cost_str}  {pct_str}   "
                f"×{ag.runs}   {tok_marker}{ag.tokens:>9,} tok"
            )
        if any_estimated:
            out.append(
                "    ↳ ``~`` = token count includes at least one estimated "
                "entry (provider didn't surface usage)."
            )

    # Top-phase note.
    if has_cost and report.phase_breakdown:
        top = report.phase_breakdown[0]
        top_pct = (top.cost / report.total_cost * 100.0) if report.total_cost else 0.0
        out.append("")
        out.append(
            f"  ↳ Top phase: ``{top.name}`` at {top_pct:.0f}% of cost reference "
            f"this window. Lower ``phases.{top.name}.effort`` to shrink it."
        )

    # Totals.
    out.append("")
    out.append("  Totals:")
    if has_cost:
        out.append(
            "    Cost reference  "
            f"{format_cost_reference(report.total_cost, estimated=report.any_estimated)}"
        )
    else:
        out.append(
            "    Cost reference  — (no run reported cost; token-only, mock, or old runs?)"
        )
    out.append(
        f"    Tokens          {report.total_tokens:,} "
        f"(in={report.total_tokens_in:,} out={report.total_tokens_out:,})"
    )
    hours = report.total_duration_s / 3600.0
    if hours >= 1:
        out.append(f"    Time            {hours:.1f}h ({report.total_duration_s/60:.0f}m)")
    else:
        out.append(
            f"    Time            {report.total_duration_s/60:.1f}m "
            f"({report.total_duration_s:.0f}s)"
        )
    out.append(f"    Runs            {report.total_runs}")

    if has_cost:
        out.append("")
        out.append(f"  ↳ {ACCOUNTING_REFERENCE_NOTE}")

    if report.priced_entries_count:
        n = report.priced_entries_count
        if report.pricing_source == "user":
            src = (
                f"~/.orcho/pricing.local.toml "
                f"(refreshed {report.pricing_snapshot_date})"
            )
        elif report.pricing_source == "bundled":
            src = f"bundled snapshot ({report.pricing_snapshot_date})"
        else:
            src = "pricing.local.toml or bundled snapshot"
        age = report.pricing_snapshot_age_days
        age_warn = ""
        if age is not None and age > 30:
            age_warn = (
                f" — ⚠ {age} days old; "
                f"``orcho pricing refresh`` to update."
            )
        out.append(
            f"  ↳ {n} entr{'y' if n == 1 else 'ies'} "
            f"priced from {src}{age_warn}\n"
            f"    Estimated cost (codex): tokens × rate ÷ 1M, "
            f"split assumed 50/50 (CLI doesn't report in/out)."
        )
    out.append("")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# pricing
# ─────────────────────────────────────────────────────────────────────────────


def format_pricing(table: PricingTable) -> str:
    """Reproduce `cmd_pricing_show` (cli/orcho.py:428-467)."""
    out: list[str] = []
    out.append("")
    out.append("  OpenAI pricing table (effective)")
    out.append("  " + "─" * 60)
    if table.user_snapshot_date:
        out.append(
            f"  user file:        ~/.orcho/pricing.local.toml "
            f"({table.user_snapshot_date})"
        )
    else:
        out.append("  user file:        ~/.orcho/pricing.local.toml (not present)")
    if table.bundled_snapshot_date:
        out.append(f"  bundled snapshot: {table.bundled_snapshot_date}")
    else:
        out.append("  bundled snapshot: empty (orcho ships no hardcoded rates)")
    if table.snapshot_age_days is not None:
        marker = " ⚠ stale" if table.snapshot_age_days > 30 else ""
        out.append(f"  age:              {table.snapshot_age_days} days{marker}")
    out.append("")
    out.append("  Provider cost notes:")
    out.append(
        "    Claude reports native cost in stream output; this table is not used"
    )
    out.append("    for those rows.")
    out.append(
        "    OpenAI/Codex token-only runs can be estimated from this table."
    )
    out.append(
        "    Gemini provider-cost behavior is not assumed here; current Orcho"
    )
    out.append(
        "    treats it as unavailable unless a parser or matching rate card"
    )
    out.append("    supplies cost.")
    out.append("")

    if not table.entries:
        out.append("  No models priced yet.")
        out.append("  Populate via:  orcho pricing refresh --provider openai")
        out.append("  Or hand-edit:  ~/.orcho/pricing.local.toml")
        out.append("")
        return "\n".join(out)

    out.append(f"    {'model':<28} {'in $/1M':>10} {'out $/1M':>10}  source")
    out.append(f"    {'─' * 64}")
    for e in table.entries:
        in_str = f"{e.input_per_million:>10.2f}" if e.input_per_million is not None else f"{'-':>10}"
        out_str = f"{e.output_per_million:>10.2f}" if e.output_per_million is not None else f"{'-':>10}"
        out.append(
            f"    {e.model:<28} {in_str} {out_str}  {e.source}"
        )
    out.append("")
    out.append(
        "  ↳ YOUR responsibility: verify rates against\n"
        "    https://developers.openai.com/api/docs/pricing whenever\n"
        "    cost estimates matter to you."
    )
    out.append("")
    return "\n".join(out)


def format_pricing_refresh_models(models: dict, provenance: str) -> str:
    """Pre-write summary, mirrors cli/orcho.py:513-524."""
    n = len(models)
    out: list[str] = []
    out.append(f"  ↳ Parsed {n} model{'' if n == 1 else 's'} from {provenance}.")
    out.append("")
    out.append(f"    {'model':<28} {'in $/1M':>10} {'out $/1M':>10}")
    for model in sorted(models):
        e = models[model]
        out.append(
            f"    {model:<28} "
            f"{e['input_per_1m_usd']:>10.2f} "
            f"{e['output_per_1m_usd']:>10.2f}"
        )
    out.append("")
    return "\n".join(out)


def format_pricing_refresh_written(result: RefreshResult) -> str:
    """Post-write footer, mirrors cli/orcho.py:535-540."""
    return (
        f"  ✓ Wrote {result.written_path}\n"
        "  ↳ Verify before relying on these numbers. orcho doesn't\n"
        "    cross-check the scraped rates against your actual API\n"
        "    contract — that's on you."
    )


def format_profile_customize(result: ProfileCustomizeResult) -> str:
    """Render ``orcho profile customize`` result."""
    action = "Would update" if result.dry_run else "Updated"
    out = [
        f"{action} profile customization for {result.profile}",
        f"  scope: {result.scope}",
        f"  file:  {result.config_path}",
        "  changes:",
    ]
    out.extend(f"    - {change}" for change in result.changes)
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# evidence
# ─────────────────────────────────────────────────────────────────────────────


def format_evidence(bundle: EvidenceBundle, *, fmt: str = "json") -> str:
    """Render an evidence bundle for stdout (cli/orcho.py:958-964)."""
    if fmt == "md":
        return bundle.markdown
    return json.dumps(
        project_evidence_json(bundle, debug=False),
        indent=2,
        ensure_ascii=False,
    )


def project_evidence_json(
    bundle: EvidenceBundle, *, debug: bool = False,
) -> dict:
    """Return the CLI JSON projection for ``orcho evidence`` stdout.

    ``debug=True`` is the raw schema bundle. The default view keeps the bundle
    shape but hides noisy live diagnostics behind a compact count, so terminal
    state and actionable errors stay readable.
    """
    body = bundle.body
    if debug:
        return body

    projected = dict(body)
    omitted_details: dict[str, object] = {}
    _project_compact_text(
        projected,
        "task",
        omitted_details,
        detail_key="task",
        limit=480,
    )
    _project_plan(projected, omitted_details)
    _project_prompt_render(projected, omitted_details)
    _project_implementation_receipts(projected, omitted_details)

    errors = list(body.get("errors") or [])
    visible_errors, hidden_live_stalls = _project_visible_errors(errors)
    projected["errors"] = visible_errors
    if hidden_live_stalls:
        omitted = dict(projected.get("omitted_diagnostics") or {})
        omitted["command_stalled_live"] = {
            "count": hidden_live_stalls,
            "debug_hint": "rerun with --debug for details",
        }
        projected["omitted_diagnostics"] = omitted
    if omitted_details:
        omitted_details.setdefault(
            "debug_hint", "rerun with --debug for the raw evidence bundle",
        )
        projected["omitted_details"] = omitted_details
    return _order_evidence_projection(projected)


_EVIDENCE_PROJECTION_ORDER = (
    "schema_version",
    "run_id",
    "run_dir",
    "project",
    "projects",
    "status",
    "created_at",
    "profile",
    "task",
    "plan",
    "errors",
    "omitted_diagnostics",
    "findings",
    "gates",
    "phases",
    "commands",
    "metrics",
    "artifacts",
    "implementation_receipts",
    "verification_readiness",
    "verification_receipts",
    "release_summary",
    "worktree",
    "worktree_projects",
    "raw_events_path",
    "omitted_details",
)


def _order_evidence_projection(projected: dict) -> dict:
    ordered = {
        key: projected[key]
        for key in _EVIDENCE_PROJECTION_ORDER
        if key in projected
    }
    ordered.update(
        (key, value)
        for key, value in projected.items()
        if key not in ordered
    )
    return ordered


def _project_compact_text(
    target: dict,
    key: str,
    omitted: dict[str, object],
    *,
    detail_key: str,
    limit: int,
) -> None:
    value = target.get(key)
    if not isinstance(value, str) or len(value) <= limit:
        return
    target[key] = value[: limit - 3].rstrip() + "..."
    omitted[detail_key] = {"full_chars": len(value)}


def _project_plan(projected: dict, omitted: dict[str, object]) -> None:
    plan = projected.get("plan")
    if not isinstance(plan, dict):
        return
    compact: dict[str, object] = {}
    for key in ("source", "short_summary", "subtask_count", "has_contract"):
        if key in plan:
            compact[key] = plan[key]
    goal = plan.get("goal")
    if isinstance(goal, str):
        compact["goal"] = (
            goal[:357].rstrip() + "..." if len(goal) > 360 else goal
        )
    owned_files = plan.get("owned_files")
    if isinstance(owned_files, list):
        compact["owned_files"] = owned_files[:20]
        compact["owned_files_count"] = len(owned_files)
    for key in (
        "acceptance_criteria",
        "commands_to_run",
        "risks",
        "review_focus",
        "mcp_context",
    ):
        value = plan.get(key)
        if isinstance(value, list):
            compact[f"{key}_count"] = len(value)
    omitted["plan"] = {
        "full_chars": len(json.dumps(plan, ensure_ascii=False)),
    }
    projected["plan"] = compact


def _project_prompt_render(projected: dict, omitted: dict[str, object]) -> None:
    prompt_render = projected.get("prompt_render")
    if not isinstance(prompt_render, list) or not prompt_render:
        return
    wire_chars = sum(
        int(entry.get("wire_chars") or 0)
        for entry in prompt_render
        if isinstance(entry, dict)
    )
    omitted["prompt_render"] = {
        "entries": len(prompt_render),
        "wire_chars": wire_chars,
    }
    projected.pop("prompt_render", None)


def _project_implementation_receipts(
    projected: dict, omitted: dict[str, object],
) -> None:
    receipts = projected.get("implementation_receipts")
    if not isinstance(receipts, list) or not receipts:
        return
    projected["implementation_receipts"] = [
        _compact_implementation_receipt(receipt)
        for receipt in receipts
        if isinstance(receipt, dict)
    ]
    omitted["implementation_receipts"] = {"entries": len(receipts)}


def _compact_implementation_receipt(receipt: dict) -> dict:
    compact: dict[str, object] = {}
    for key in (
        "subtask_id",
        "state",
        "runtime",
        "model",
        "skill",
        "duration",
        "error",
        "attestation_summary",
        "attestation_error",
        "attestation_repaired",
    ):
        if key in receipt:
            compact[key] = receipt[key]
    for key in ("depends_on", "done_criteria", "criteria_report"):
        value = receipt.get(key)
        if isinstance(value, list):
            compact[f"{key}_count"] = len(value)
    return compact


def _project_visible_errors(
    errors: list[dict],
) -> tuple[list[dict], int]:
    visible: list[dict] = []
    hidden_live_stalls = 0
    for err in errors:
        if err.get("kind") == "command_stalled" and err.get("terminal") is not True:
            hidden_live_stalls += 1
            continue
        visible.append(err)
    return visible, hidden_live_stalls


def format_run_diff(record: RunDiffRecord) -> str:
    """Render a :class:`RunDiffRecord` for ``orcho diff`` stdout.

    Three branches mirror the SDK contract:

    - ``found=False`` → print the artifact-absent message (no body).
    - ``found=True`` with empty ``files`` → matched-empty path filter:
      print the SDK message only, no stat table or blank body.
    - Normal case → print ``record.content`` verbatim; if truncated,
      append a single-line footer using ``record.max_bytes`` so the
      footer is self-contained.

    Output is whatever the SDK rendered (raw patch / colored preview /
    stat table); this formatter does not add headers or recolor.
    """
    if not record.found:
        return record.message or "No diff artifact recorded for this run."
    if not record.files:
        return record.message or f"No diff entries matched run={record.run_id!r}."

    body = record.content
    if record.truncated:
        suffix = f"... output truncated at {record.max_bytes} bytes ..."
        if not body.endswith("\n"):
            body += "\n"
        body += suffix
    return body


_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_LABEL_RE = re.compile(r"\*\*([^*]+?):\*\*")


def colorize_evidence_markdown(md: str) -> str:
    """Add terminal color to evidence markdown without changing content.

    The evidence bundle renderer stays pure markdown for files, pipes, and
    stored artifacts. This helper is CLI-only presentation for interactive
    stdout.
    """
    out: list[str] = []
    for raw in md.splitlines():
        line = _colorize_evidence_line(raw)
        out.append(line)
    return "\n".join(out) + ("\n" if md.endswith("\n") else "")


def _colorize_evidence_line(line: str) -> str:
    if line.startswith("# "):
        return paint(line, C.MAGENTA, C.BOLD)
    if line.startswith("## "):
        color = C.YELLOW if line == "## Findings" else C.CYAN
        return paint(line, color, C.BOLD)
    if line.startswith("### "):
        return _colorize_finding_heading(line)
    if line.startswith("|"):
        return _colorize_table_row(line)
    if line.startswith("_") and line.endswith("_"):
        return paint(line, C.GREY)
    return _colorize_inline_markdown(line)


def _colorize_finding_heading(line: str) -> str:
    severity = ""
    m = re.match(r"### `([^`]+)`", line)
    if m:
        severity = m.group(1)
    color = {
        "P0": C.RED,
        "P1": C.RED,
        "P2": C.YELLOW,
        "P3": C.GREY,
    }.get(severity, C.YELLOW)
    return paint(line, color, C.BOLD)


def _colorize_table_row(line: str) -> str:
    if _is_separator_row(line):
        return paint(line, C.GREY)

    cells = [part.strip() for part in line.strip().strip("|").split("|")]
    if len(cells) >= 5 and cells[:5] == ["#", "Phase", "Title", "Outcome", "Attempt"]:
        return paint(line, C.CYAN, C.BOLD)
    if len(cells) >= 5 and cells[0].isdigit():
        return _colorize_phase_timeline_row(line)
    return paint(line, C.GREY)


def _is_separator_row(line: str) -> bool:
    body = line.replace("|", "").replace("-", "").replace(":", "").strip()
    return body == ""


def _colorize_phase_timeline_row(line: str) -> str:
    cells = [part.strip() for part in line.strip().strip("|").split("|")]
    if len(cells) < 5:
        return paint(line, C.GREY)
    index, phase, title, outcome, attempt = cells[:5]
    rendered = [
        _dim_cell(index),
        _table_code(phase, C.CYAN),
        paint(title, C.WHITE),
        _outcome_cell(outcome),
        _dim_cell(attempt),
    ]
    return "| " + " | ".join(rendered) + " |"


def _table_code(cell: str, color: str) -> str:
    m = re.fullmatch(r"`([^`]+)`", cell)
    body = m.group(1) if m else cell
    return paint(f"`{body}`", color)


def _outcome_cell(cell: str) -> str:
    m = re.fullmatch(r"`([^`]+)`", cell)
    body = m.group(1) if m else cell
    lowered = body.lower()
    color = C.WHITE
    if any(word in lowered for word in ("rejected", "failed", "halted", "error")):
        color = C.RED
    elif "skipped" in lowered:
        color = C.GREY
    elif any(word in lowered for word in ("approved", "done", "ok", "passed")):
        color = C.GREEN
    return paint(f"`{body}`", color)


def _dim_cell(cell: str) -> str:
    return paint(cell, C.GREY)


def _colorize_inline_markdown(line: str) -> str:
    if line.startswith("**Required fix:**"):
        rest = line.removeprefix("**Required fix:**").lstrip()
        rest = _INLINE_CODE_RE.sub(
            lambda m: paint(m.group(1), C.GREEN), rest,
        )
        return f"{paint('Required fix:', C.YELLOW, C.BOLD)} {rest}".rstrip()
    line = _BOLD_LABEL_RE.sub(
        lambda m: paint(f"{m.group(1)}:", C.CYAN, C.BOLD), line,
    )
    line = _INLINE_CODE_RE.sub(
        lambda m: paint(m.group(1), C.GREEN), line,
    )
    return line


def format_written_paths(paths: Iterable[Path]) -> str:
    """Render a list of written paths (cli/orcho.py:954-955)."""
    return "\n".join(f"Wrote {p}" for p in paths)


# ─────────────────────────────────────────────────────────────────────────────
# prompts
# ─────────────────────────────────────────────────────────────────────────────


def format_prompts_list(
    names: list[str],
    *,
    project_dir: str | None,
    winners: dict[str, str],
) -> str:
    """List view: ``orcho prompts`` / ``orcho prompts --list``.

    `winners` maps prompt name → resolved level (``"core"`` /
    ``"workspace"`` / ``"project"`` / ``"unknown"``).
    """
    out: list[str] = []
    out.append("")
    out.append(f"  Available prompts ({len(names)}):")
    for name in sorted(names):
        winner = winners.get(name, "unknown")
        out.append(f"    {name:<36} [{winner}]")
    out.append("")
    return "\n".join(out)


def format_prompts_resolution(
    res: PromptResolution,
    *,
    project_dir: str | None,
    verbose: bool,
) -> str:
    """Resolution-chain view (cli/orcho.py:996-1021)."""
    out: list[str] = []
    out.append("")
    out.append(f"  Resolution chain for: '{res.name}'")
    if project_dir:
        out.append(f"  Project: {project_dir}")
        out.append("")
    else:
        out.append("")

    for step in res.chain:
        icon = "✅" if step.exists else "○ "
        label = "(ACTIVE)" if step.exists and step.is_winner else "(not found)"
        if step.exists and not step.is_winner:
            label = "(shadowed)"
        out.append(
            f"  {icon}  [{step.location:10s}]  {step.path}  {label}"
        )

    if res.winner is not None:
        winning_step = next(s for s in res.chain if s.is_winner)
        out.append("")
        out.append(f"  → Using: [{winning_step.location}] {res.winner.name}")
        if verbose and res.body is not None:
            out.append("")
            out.append("═" * 80)
            out.append(res.body.strip())
            out.append("═" * 80)
    else:
        out.append("")
        out.append(f"  ✗ No template found for '{res.name}'")

    out.append("")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# workspace init
# ─────────────────────────────────────────────────────────────────────────────


def format_workspace_init(result: WorkspaceInitResult) -> str:
    """Render the `orcho workspace init` outcome for stdout.

    Shape:

      * one-line confirmation (or dry-run prefix);
      * the four key paths a user needs to know;
      * detected child projects, for confidence;
      * any warnings (e.g. existing env file with diverging content);
      * "next steps" — the `source` line + a couple of typical
        commands;
      * the MCP-config snippet, either printed verbatim or with a
        note that a config file was written/merged.
    """
    header = (
        "Orcho workspace initialized (dry run — nothing written)"
        if result.dry_run else
        "Orcho workspace initialized"
    )

    out: list[str] = ["", paint(header, C.GREEN, C.BOLD), ""]
    out.append(_workspace_kv("Project group:", result.group_root))
    out.append(_workspace_kv("Workspace:", result.workspace_dir))
    out.append(_workspace_kv("Runs:", result.runs_dir))
    out.append(_workspace_kv("Env:", result.env_file))
    out.append(_workspace_kv("Local config:", result.local_config_file))
    out.append("")

    extra_projects = getattr(result, "extra_projects", ())
    undetected_count = getattr(result, "undetected_count", 0)
    interactive = getattr(result, "interactive", False)

    if result.detected_projects:
        out.append(_workspace_heading("Detected projects:"))
        for p in result.detected_projects:
            out.append(f"    - {paint(p.name, C.GREEN)}")
        out.append("")
    else:
        # Only claim the group root is truly empty when there were no
        # manually added projects and no undetected candidates either.
        if extra_projects or undetected_count:
            out.append(
                f"  {paint('Detected projects:', C.CYAN, C.BOLD)} "
                f"{paint('(none auto-detected)', C.GREY)}"
            )
        else:
            out.append(
                f"  {paint('Detected projects:', C.CYAN, C.BOLD)} "
                f"{paint('(none — group root is empty)', C.GREY)}"
            )
        out.append("")

    if extra_projects:
        out.append(_workspace_heading("Interactively registered projects:"))
        for p in extra_projects:
            git_hint = (
                paint(f"  (git_dir: {p.git_dir})", C.GREY)
                if p.git_dir else ""
            )
            out.append(f"    - {paint(p.name, C.GREEN)}{git_hint}")
        out.append("")

    if undetected_count > 0 and not interactive:
        noun = "folder" if undetected_count == 1 else "folders"
        out.append(
            f"  {paint('Note:', C.YELLOW, C.BOLD)} "
            f"{paint(f'{undetected_count} {noun} not auto-detected — re-run interactively to register.', C.YELLOW)}"
        )
        out.append("")

    extension_points = getattr(result, "extension_points", ())
    if extension_points:
        out.append(_workspace_heading("Extension points:"))
        labels = (
            "Plugin template:",
            "Prompt overrides:",
            "Task files:",
        )
        for label, path in zip(labels, extension_points, strict=False):
            out.append(
                f"    - {paint(label, C.CYAN)} {paint(str(path), C.GREEN)}"
            )
        out.append("")

    installed_runtimes = [r for r in result.detected_runtimes if r.installed]
    if result.detected_runtimes:
        out.append(_workspace_heading("Detected CLI runtimes:"))
        if installed_runtimes:
            for rt in installed_runtimes:
                out.append(
                    f"    - {paint(rt.client, C.GREEN)} "
                    f"{paint(f'({rt.path})', C.GREY)}"
                )
        else:
            out.append(
                f"    {paint('(none on PATH — see the setup blocks below)', C.GREY)}"
            )
        out.append("")

    if result.warnings:
        out.append(f"  {paint('Warnings:', C.YELLOW, C.BOLD)}")
        for w in result.warnings:
            out.append(f"    {paint(f'⚠ {w}', C.YELLOW)}")
        out.append("")

    out.append(_workspace_heading("Next shell step:"))
    out.append(_workspace_command(f"source {result.env_file}"))
    out.append("")
    out.append(_workspace_heading("Try:"))
    out.append(_workspace_command(f"orcho status --workspace {result.workspace_dir}"))
    if result.detected_projects:
        first = result.detected_projects[0].path
        out.append(_workspace_command(
            f"orcho run --project {first} --task '...' --mock"
        ))
    out.append("")

    # MCP snippet — always shown. When a config file was touched, also
    # report what happened to that file.
    server_entry = result.mcp_snippet["mcpServers"][result.mcp_server_name]
    mcp_command = str(server_entry["command"])
    workspace_dir = str(server_entry["env"]["ORCHO_WORKSPACE"])
    quoted_server = shlex.quote(result.mcp_server_name)
    quoted_workspace = shlex.quote(workspace_dir)
    quoted_command = shlex.quote(mcp_command)

    out.append(_workspace_heading("MCP client setup — choose one path:"))
    out.append(
        f"    {paint('Note:', C.YELLOW)} "
        f"{paint('for multiple workspaces, register one Orcho MCP server per workspace with a distinct name (for example orcho-demo-mcp, orcho-atas-mcp).', C.GREY)}"
    )
    if result.mcp_config_path is not None:
        verb = {
            "wrote": "Wrote",
            "merged": "Merged into",
            "no-op": "Already up to date in",
            "replaced": "Replaced server entry in",
        }.get(result.mcp_config_action, "Updated")
        out.append(
            f"    {paint(verb, C.GREEN)} "
            f"{paint('MCP config file:', C.CYAN)} "
            f"{paint(result.mcp_config_path, C.GREEN)} "
            f"{paint(f'(server: {result.mcp_server_name})', C.GREY)}"
        )
        out.append(
            f"    {paint('Use that file for JSON-based clients, or compare it with the reference shapes below.', C.GREY)}"
        )
    out.append("")
    by_client = {r.client: r for r in result.detected_runtimes}
    out.append(_workspace_subheading(
        "Terminal clients — run one command in your shell:"
    ))
    if installed_runtimes:
        out.append(
            f"    {paint('Tip:', C.GREEN)} "
            f"{paint('clients marked ✓ are installed on this machine — start with those.', C.GREY)}"
        )
    out.append("")
    out.append(_workspace_client_subheading(
        "Codex CLI / Codex app:", by_client.get("Codex CLI / Codex app")
    ))
    out.extend(_workspace_command_block([
        f"codex mcp add {quoted_server} \\",
        f"  --env ORCHO_WORKSPACE={quoted_workspace} \\",
        f"  -- {quoted_command}",
    ]))
    out.append(_workspace_done_when(
        f"`codex mcp list` shows `{result.mcp_server_name}` as enabled; "
        "restart the Codex session before using tools."
    ))
    out.append("")
    out.append(_workspace_client_subheading(
        "Claude Code:", by_client.get("Claude Code")
    ))
    out.extend(_workspace_command_block([
        f"claude mcp add {quoted_server} \\",
        f"  --env ORCHO_WORKSPACE={quoted_workspace} \\",
        f"  -- {quoted_command}",
    ]))
    out.append(_workspace_done_when(
        f"`claude mcp list` shows `{result.mcp_server_name}`; "
        "restart the Claude Code session before using tools."
    ))
    out.append("")
    out.append(_workspace_client_subheading(
        "Gemini CLI:", by_client.get("Gemini CLI")
    ))
    out.extend(_workspace_command_block([
        f"gemini mcp add --env ORCHO_WORKSPACE={quoted_workspace} \\",
        f"  {quoted_server} {quoted_command}",
    ]))
    out.append(_workspace_done_when(
        f"`gemini mcp list` shows `{result.mcp_server_name}`; "
        "restart the Gemini session before using tools."
    ))
    out.append("")
    out.append(_workspace_subheading(
        "App config snippets — copy into the app config, do not run:"
    ))
    out.append("")
    out.append(_workspace_subheading(
        "Claude app / JSON clients — mcpServers shape:"
    ))
    snippet = json.dumps(result.mcp_snippet, indent=2, ensure_ascii=False)
    out.extend(_workspace_json_block(snippet.splitlines()))
    out.append(_workspace_done_when(
        "the app config contains this server entry and the app has been restarted."
    ))
    out.append("")

    out.append(_workspace_subheading(
        "Antigravity app — User/mcp.json servers shape:"
    ))
    antigravity = {
        "servers": {
            result.mcp_server_name: {
                "type": "stdio",
                "command": mcp_command,
                "args": list(server_entry.get("args", [])),
                "env": {
                    "ORCHO_WORKSPACE": workspace_dir,
                },
            },
        },
        "inputs": [],
    }
    out.extend(_workspace_json_block(json.dumps(
        antigravity, indent=2, ensure_ascii=False,
    ).splitlines()))
    out.append(_workspace_done_when(
        "`User/mcp.json` contains this server entry and Antigravity has been restarted."
    ))
    out.append("")
    out.append(_workspace_subheading("After client restart — verify:"))
    out.append(f"    {paint('orcho_workspace_info', C.GREEN)}")
    out.append(
        f"    {paint(f'Expected workspace: {workspace_dir}', C.GREY)}"
    )
    out.append("")

    return "\n".join(out)


def _workspace_heading(text: str) -> str:
    return f"  {paint(text, C.CYAN, C.BOLD)}"


def _workspace_subheading(text: str) -> str:
    return f"  {paint(text, C.CYAN)}"


def _workspace_client_subheading(
    text: str, runtime: DetectedRuntime | None,
) -> str:
    """Subheading for a terminal client, annotated with PATH detection.

    A ✓ marks a runtime found on PATH; a runtime probed but missing is
    flagged so the user knows the block is informational only. Unknown
    clients (no probe entry) render as a plain subheading.
    """
    base = _workspace_subheading(text)
    if runtime is None:
        return base
    if runtime.installed:
        return f"{base} {paint('✓ installed', C.GREEN, C.BOLD)}"
    return f"{base} {paint(f'(not found — `{runtime.command}` not on PATH)', C.GREY)}"


def _workspace_kv(label: str, value: str) -> str:
    return f"  {paint(f'{label:<15}', C.CYAN)} {paint(value, C.GREEN)}"


def _workspace_command(command: str) -> str:
    return f"    {paint(command, C.GREEN)}"


def _workspace_command_block(lines: list[str]) -> list[str]:
    out = [f"    {paint('```bash', C.GREY)}"]
    out.extend(f"    {paint(line, C.GREEN)}" for line in lines)
    out.append(f"    {paint('```', C.GREY)}")
    return out


def _workspace_json_block(lines: list[str]) -> list[str]:
    out = [f"    {paint('```json', C.GREY)}"]
    out.extend(f"    {paint(line, C.GREY)}" for line in lines)
    out.append(f"    {paint('```', C.GREY)}")
    return out


def _workspace_done_when(text: str) -> str:
    return f"    {paint('Done when:', C.GREEN)} {paint(text, C.GREY)}"


# ─────────────────────────────────────────────────────────────────────────────
# errors
# ─────────────────────────────────────────────────────────────────────────────


def format_fine_tune(result: FineTuneResult) -> str:
    """Render the candidate verification contract from ``fine_tune_project``.

    Lists detected markers, each proposed ``verification_env`` with its
    assertions, the proposed commands, and the deferred-materialisation note.
    Stage 2 never writes, so the footer always states nothing was written.
    """
    candidate = result.candidate or {}
    verification = candidate.get("verification") or {}
    envs = candidate.get("verification_envs") or {}
    commands = verification.get("commands") or {}

    out: list[str] = [""]
    out.append(f"  fine-tune (dry-run) — {result.project}")
    markers = ", ".join(result.markers) if result.markers else "(none detected)"
    out.append(f"  markers: {markers}")
    if result.suggested_projects:
        out.append("")
        out.append("  project roots detected below this directory:")
        for project in result.suggested_projects:
            out.append(f"    - {project}")
        out.append("")
        out.append("  Run fine-tune for one detected project, for example:")
        out.append(f"    orcho workspace fine-tune {result.suggested_projects[0]}")
        out.append("")
    out.append(f"  work_mode: {candidate.get('work_mode', '')}")
    out.append(f"  default_env: {verification.get('default_env', '')}")
    out.append("")

    out.append("  verification_envs:")
    for name, spec in envs.items():
        python = spec.get("python")
        suffix = f"  (python: {python})" if python else ""
        out.append(f"    [{name}]{suffix}")
        for a in spec.get("assertions", []):
            out.append(f"      - {a}")
    if not envs:
        out.append("    (none)")
    out.append("")

    out.append("  commands:")
    for name, cmd in commands.items():
        if "worktree_bootstrap" in cmd:
            out.append(f"    {name}: {cmd.get('note', '')}")
            out.append(f"      worktree_bootstrap: {cmd.get('worktree_bootstrap')}")
            continue
        run = cmd.get("run", "")
        env_ref = cmd.get("env", "")
        out.append(f"    {name}: {run}  [env={env_ref}]")
    if not commands:
        out.append("    (none)")
    out.append("")

    out.append(f"  {result.note}")
    out.append("  No files were written.")
    out.append("")
    return "\n".join(out)


def format_verify_env(result: VerifyEnvResult) -> str:
    """Render the ``orcho verify env`` summary.

    Header line carries the env name + overall PASS/FAIL, the subject's
    checkout/project, one line per assertion (PASS/FAIL with kind + name,
    failure ``detail`` appended), and the env-receipt path.
    """
    out: list[str] = [""]
    overall = "PASS" if result.all_passed else "FAIL"
    out.append(f"  verify env [{result.env}] — {overall}")
    subject = result.subject or {}
    out.append(f"  checkout: {subject.get('checkout', '')}")
    out.append(f"  project:  {subject.get('project', '')}")
    out.append("")
    for a in result.assertions:
        mark = "PASS" if a.get("passed") else "FAIL"
        line = f"    [{mark}] {a.get('kind', '')}: {a.get('name', '')}"
        detail = a.get("detail")
        if detail and not a.get("passed"):
            line += f" — {detail}"
        out.append(line)
    if result.assertions:
        out.append("")
    if result.receipt_path is not None:
        out.append(f"  receipt: {result.receipt_path}")
    out.append("")
    return "\n".join(out)


def format_verify_list(result: VerifyListResult) -> str:
    """Render the ``orcho verify list`` summary — declared commands only.

    One line per declared command: a ``*`` marker for required commands, the
    name, its env, and the placeholder-resolved run text. Nothing is executed,
    so this is a pure projection of the contract against the run's checkout.
    """
    out: list[str] = ["", f"  verify list — {len(result.commands)} declared command(s)"]
    out.append("  (* = required;  nothing executed, no receipts written)")
    out.append("")
    for cmd in result.commands:
        marker = "*" if cmd.get("required") else " "
        name = cmd.get("name", "")
        env_ref = cmd.get("env", "")
        run = cmd.get("run_resolved", "")
        out.append(f"  {marker} {name}  [env={env_ref}]")
        out.append(f"      {run}")
    if not result.commands:
        out.append("  (none)")
    out.append("")
    return "\n".join(out)


def format_verify_run(result: VerifyRunResult) -> str:
    """Render the ``orcho verify run`` summary of official declared-receipts.

    Each command is one block: a PASS/FAIL marker (exit 0 = PASS), the env and
    exit code, the ``parity`` mode, and the persisted receipt path. For a
    ``differential`` command the block also shows ``checkout_head`` vs
    ``baseline_head`` so the reviewer can see which two subjects were compared.
    When the command depended on declared dependency repos, an ``against:`` line
    names the dependency commits it was tested against (ADR 0084). The header
    makes clear these are durable declared-receipts, distinct from the
    env-assertion receipts of ``verify env``.
    """
    out: list[str] = [""]
    overall = "PASS" if result.all_passed else "FAIL"
    out.append(f"  verify run — {overall}  (official declared command-receipts)")
    out.append("")
    for o in result.outcomes:
        mark = "PASS" if o.exit_code == 0 else "FAIL"
        exit_repr = "none" if o.exit_code is None else str(o.exit_code)
        out.append(
            f"    [{mark}] {o.command}  "
            f"[env={o.env}] [exit={exit_repr}] [parity={o.parity}]",
        )
        if o.parity == "differential":
            out.append(f"        checkout_head: {o.checkout_head or '(none)'}")
            out.append(f"        baseline_head: {o.baseline_head or '(none)'}")
        if o.dependencies:
            out.append("        against: " + " + ".join(o.dependencies))
        if o.receipt_path is not None:
            out.append(f"        receipt: {o.receipt_path}")
    if not result.outcomes:
        out.append("    (no commands run)")
    out.append("")
    return "\n".join(out)


def format_error(exc: OrchoError) -> str:
    """Render a typed SDK error for stderr."""
    return str(exc)
