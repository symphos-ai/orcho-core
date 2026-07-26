"""Pure formatters for CLI output.

Every function here takes typed SDK values (dataclasses) and returns a
`str`. They never call `print`, never write files, never raise. The
goal is byte-for-byte parity with the previous handler-embedded output
so the golden-output diff in REA-3.8's DoD passes whitespace-only.
"""
from __future__ import annotations

import ast
import json
import re
import shlex
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from core.io.ansi import C, paint
from core.io.delivery_summary import project_degraded_publish
from core.observability.accounting_display import (
    ACCOUNTING_REFERENCE_NOTE,
    format_cost_reference,
    format_estimated_entries_footer,
    runtime_accounting_hint,
)
from sdk import (
    CostReport,
    DetectedRuntime,
    EvidenceBundle,
    FineTuneResult,
    OrchoError,
    PhaseBreakdown,
    PricingTable,
    ProfileCustomizeResult,
    ProjectBreakdown,
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


def _stdout_paint(text: str, *codes: str) -> str:
    """Paint stdout-bound CLI text through the shared color policy."""
    return paint(text, *codes, color=None, stream=sys.stdout)


def _status_label(text: str) -> str:
    return _stdout_paint(text, C.CYAN)


def _status_section(text: str) -> str:
    return _stdout_paint(text, C.CYAN, C.BOLD)


def _status_muted(text: str) -> str:
    return _stdout_paint(text, C.GREY)


def _status_warning(text: str) -> str:
    return _stdout_paint(text, C.YELLOW)


def _status_state(text: str) -> str:
    normalized = text.strip().lower()
    if normalized in {
        "approved",
        "committed",
        "done",
        "pass",
        "passed",
        "ship_ready",
        "success",
        "succeeded",
    }:
        return _stdout_paint(text, C.GREEN)
    if normalized in {
        "blocked",
        "failed",
        "halted",
        "incomplete",
        "patch_invalid",
        "patch_missing",
        "rejected",
    }:
        return _stdout_paint(text, C.RED)
    if normalized.startswith("awaiting") or normalized in {
        "in_progress",
        "paused",
        "pending",
        "running",
        "skipped",
    }:
        return _stdout_paint(text, C.YELLOW)
    return _stdout_paint(text, C.WHITE)


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


def _clip_status_text(value: object, max_len: int) -> str:
    text = str(value or "?").replace("\n", " ").strip() or "?"
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _float_metric(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _int_metric(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _metrics_cost_estimated(raw_metrics: dict[str, Any]) -> bool:
    top_level = raw_metrics.get("cost_estimated")
    if isinstance(top_level, bool):
        return top_level
    phases = raw_metrics.get("phases")
    if not isinstance(phases, dict):
        return False
    return any(
        bool(phase.get("cost_estimated"))
        for phase in phases.values()
        if isinstance(phase, dict)
        and _float_metric(phase.get("cost_usd_equivalent")) > 0.0
    )


def _append_status_usage(out: list[str], status: RunStatus) -> None:
    if not status.raw_metrics:
        return
    out.append("")
    tokens_text = (
        f"{status.total_tokens:,} "
        f"(in={status.total_tokens_in:,} out={status.total_tokens_out:,})"
    )
    out.append(
        f"{_status_label('  Tokens:')}  "
        f"{_stdout_paint(tokens_text, C.WHITE)}"
    )
    cost = _float_metric(status.raw_metrics.get("total_cost_usd_equivalent"))
    if cost > 0.0:
        estimated = _metrics_cost_estimated(status.raw_metrics)
        out.append(
            f"{_status_label('  Cost ref:')} "
            f"{_cost_reference_text(cost, estimated=estimated)}"
        )
    out.append(
        f"{_status_label('  Time:')}    "
        f"{_stdout_paint(f'{status.total_duration_s:.1f}s', C.WHITE)}"
    )
    if status.total_rounds:
        out.append(
            f"{_status_label('  Rounds:')}  "
            f"{_stdout_paint(str(status.total_rounds), C.WHITE)}"
        )
    if status.total_retries:
        out.append(
            f"{_status_label('  Retries:')} "
            f"{_status_warning(str(status.total_retries))}"
        )


def _append_status_phases(
    out: list[str],
    *,
    status: RunStatus,
    meta: object,
) -> None:
    raw_phases = status.raw_metrics.get("phases") if status.raw_metrics else None
    if isinstance(raw_phases, dict) and raw_phases:
        phase_items = [
            (name, data)
            for name, data in raw_phases.items()
            if isinstance(data, dict)
        ]
        if phase_items:
            show_cost = any(
                _float_metric(data.get("cost_usd_equivalent")) > 0.0
                for _, data in phase_items
            )
            out.append("")
            out.append(_status_section("  Phases:"))
            for name, data in phase_items:
                attempts = _int_metric(data.get("attempts"))
                attempts_text = f"attempts={attempts}" if attempts else "attempts=?"
                tokens = _int_metric(data.get("total_tokens"))
                duration = _float_metric(data.get("duration_s"))
                model = _clip_status_text(data.get("model"), 26)
                phase_cell = _stdout_paint(f"{name:<18}", C.CYAN)
                attempts_cell = _status_muted(f"{attempts_text:<11}")
                tokens_cell = _stdout_paint(f"{tokens:>11,} tok", C.WHITE)
                duration_cell = _stdout_paint(f"{duration:>8.1f}s", C.BLUE)
                model_cell = _status_muted(f"{model:<26}")
                line = (
                    f"    {phase_cell} {attempts_cell} "
                    f"{tokens_cell} {duration_cell}  {model_cell}"
                )
                if show_cost:
                    cost = _float_metric(data.get("cost_usd_equivalent"))
                    cost_text = (
                        _cost_reference_text(
                            cost,
                            estimated=bool(data.get("cost_estimated")),
                        )
                        if cost > 0.0
                        else ""
                    )
                    line = f"{line}  {cost_text}"
                out.append(line.rstrip())
            return

    meta_phases = getattr(meta, "phases", ())
    if meta_phases:
        out.append("")
        out.append(
            f"{_status_section('  Phases completed:')} "
            f"{_stdout_paint(', '.join(meta_phases), C.WHITE)}"
        )


def _append_status_gates(out: list[str], status: RunStatus, *, verbose: bool) -> None:
    gates = status.quality_gates
    if not gates:
        return

    counts: dict[str, int] = {}
    for gate in gates:
        outcome = str(gate.outcome or "unknown")
        counts[outcome] = counts.get(outcome, 0) + 1

    out.append("")
    out.append(_status_section("  Gates:"))
    out.append(
        "    "
        + " · ".join(
            f"{_status_state(outcome)} {_status_muted(f'x{count}')}"
            for outcome, count in counts.items()
        )
    )

    attention = [
        gate for gate in gates
        if str(gate.outcome or "") in {"failed", "skipped", "in_progress"}
    ]
    rows = gates if verbose else attention[:6]
    for gate in rows:
        name = _clip_status_text(gate.name, 22)
        outcome = _clip_status_text(gate.outcome, 16)
        duration = gate.duration_s
        duration_text = "-" if duration is None else f"{duration:.2f}s"
        kind = f" {_status_muted(str(gate.kind))}" if gate.kind else ""
        out.append(
            f"    {_stdout_paint(f'{name:<22}', C.CYAN)} "
            f"{_status_state(f'{outcome:<16}')} "
            f"{_status_muted(f'{duration_text:>8}')}{kind}"
        )
    remaining = len(attention) - len(rows)
    if remaining > 0:
        out.append(
            f"    {_status_muted(f'... {remaining} more attention gates; use --verbose')}"
        )


def _append_status_delivery(
    out: list[str], raw_meta: dict[str, Any], *, publish_gate: object | None,
) -> None:
    delivery = raw_meta.get("commit_delivery")
    if not isinstance(delivery, dict):
        return
    status_text = str(delivery.get("status") or "").strip()
    action_text = str(delivery.get("action") or "").strip()
    verdict = str(delivery.get("release_verdict") or "").strip()
    delivery_branch = str(delivery.get("delivery_branch") or "").strip()
    pr_url = str(delivery.get("pr_url") or "").strip()
    verification_missing = delivery.get("verification_missing")
    summary = str(delivery.get("release_summary") or "").strip()
    if not any(
        (
            status_text,
            action_text,
            verdict,
            delivery_branch,
            pr_url,
            verification_missing,
            summary,
        )
    ):
        return

    out.append("")
    out.append(_status_section("  Delivery:"))
    if status_text or action_text:
        suffix = f" ({action_text})" if action_text and action_text != status_text else ""
        out.append(
            f"{_status_label('    Status:')} "
            f"{_status_state(status_text or '?')}"
            f"{_status_muted(suffix) if suffix else ''}"
        )
    if verdict:
        out.append(
            f"{_status_label('    Release:')} "
            f"{_status_state(verdict)}"
        )
    if summary:
        out.append(
            f"{_status_label('    Summary:')} "
            f"{_stdout_paint(_clip_status_text(summary, 140), C.WHITE)}"
        )
    if isinstance(verification_missing, list) and verification_missing:
        missing = ", ".join(str(item) for item in verification_missing)
        out.append(
            f"{_status_label('    Verification missing:')} "
            f"{_status_warning(missing)}"
        )
    if delivery_branch:
        out.append(
            f"{_status_label('    Branch:')} "
            f"{_stdout_paint(delivery_branch, C.WHITE)}"
        )
    degraded = project_degraded_publish(delivery, publish_gate=publish_gate)
    if degraded is not None:
        out.append(
            f"{_status_label('    Ready:')} "
            f"{_status_warning(f'{degraded.ready_text} — reason: {degraded.reason}')}"
        )
    if pr_url:
        out.append(f"{_status_label('    PR:')} {_stdout_paint(pr_url, C.GREEN)}")


def _append_status_paths(
    out: list[str],
    status: RunStatus,
    raw_meta: dict[str, Any],
) -> None:
    out.append("")
    out.append(_status_section("  Paths:"))
    project = raw_meta.get("project")
    if project:
        out.append(
            f"{_status_label('    Source:')}   {_status_muted(str(project))}"
        )
    worktree = raw_meta.get("worktree")
    if isinstance(worktree, dict) and worktree.get("path"):
        out.append(
            f"{_status_label('    Worktree:')} {_status_muted(str(worktree['path']))}"
        )
    parent_run_id = raw_meta.get("parent_run_id")
    if parent_run_id:
        out.append(
            f"{_status_label('    Parent:')}   {_stdout_paint(str(parent_run_id), C.WHITE)}"
        )
    out.append(
        f"{_status_label('    Run dir:')}  {_status_muted(str(status.run_ref.run_dir))}"
    )


def format_status(
    status: RunStatus,
    *,
    verbose: bool = False,
    publish_gate: object | None = None,
) -> str:
    """Render a human-readable status snapshot for one run."""
    out: list[str] = []
    sep = "─" * 60
    out.append("")
    out.append(_status_muted(sep))
    out.append(
        f"{_status_label('  Run:')}     "
        f"{_stdout_paint(status.run_ref.run_id, C.GREEN, C.BOLD)}"
    )
    out.append(_status_muted(sep))

    meta = status.meta
    if meta is not None:
        if meta.projects:
            out.append(
                f"{_status_label('  Project:')} "
                f"{_stdout_paint('[cross]', C.MAGENTA)} "
                f"{_stdout_paint(', '.join(meta.projects), C.WHITE)}"
            )
        else:
            out.append(
                f"{_status_label('  Project:')} "
                f"{_stdout_paint(Path(meta.project or '?').name, C.WHITE)}"
            )
        out.append(
            f"{_status_label('  Task:')}    "
            f"{_stdout_paint((meta.task or '?')[:80], C.WHITE)}"
        )
        out.append(
            f"{_status_label('  Status:')}  "
            f"{_status_state(meta.status or '?')}"
        )
        # Pending phase-handoff id: only present while the run awaits an
        # operator decision. Names the exact id the operator must pass to
        # ``phase_handoff_decide`` / ``orcho run --resume`` so the current
        # round (e.g. ``review_changes:repair_round:2``) is unambiguous.
        pending_handoff = _pending_handoff_id(status)
        if pending_handoff is not None:
            out.append(
                f"{_status_label('  Pending handoff:')} "
                f"{_status_warning(pending_handoff)}"
            )
        out.append(
            f"{_status_label('  Profile:')} "
            f"{_stdout_paint(meta.profile or '?', C.WHITE)}"
        )
        out.append(
            f"{_status_label('  Time:')}    "
            f"{_status_muted(meta.timestamp or '?')}"
        )

    _append_status_usage(out, status)
    _append_status_phases(out, status=status, meta=meta)
    _append_status_gates(out, status, verbose=verbose)

    if status.sub_projects:
        out.append("")
        out.append(_status_section("  Projects:"))
        for sp in status.sub_projects:
            out.append(
                f"    {_stdout_paint(f'[{sp.name}]', C.CYAN)}  "
                f"{_status_label('status=')}{_status_state(sp.status or '?')}"
            )

    _append_status_delivery(out, status.raw_meta, publish_gate=publish_gate)
    _append_status_paths(out, status, status.raw_meta)

    if verbose and status.raw_meta:
        out.append("")
        out.append("  Detailed Meta:")
        out.append(f"  {'-' * 14}")
        meta_text = json.dumps(status.raw_meta, indent=2, ensure_ascii=False)
        for line in meta_text.split("\n"):
            out.append(f"    {line}")

    out.append(_status_muted(sep))
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
    out.append(_status_section(f"  Run history · last {len(rows)} shown"))
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
        out.append(
            f"  {r.run_id:<22} {_status_state(f'{status:<20}')} "
            f"{project:<22} {task}"
        )
    out.append("")
    out.append(_status_muted(
        "  Next: orcho status <run-id> · orcho evidence <run-id> · "
        "orcho diff <run-id> --preview"
    ))
    out.append("")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# metrics
# ─────────────────────────────────────────────────────────────────────────────


def format_metrics_run(m: RunMetrics) -> str:
    """Render one run's metrics detail."""
    out: list[str] = []
    sep = "─" * 60
    out.append("")
    out.append(_status_muted(sep))
    out.append(
        f"{_status_label('  Metrics:')} "
        f"{_stdout_paint(m.run_id, C.GREEN, C.BOLD)}"
    )
    out.append(_status_muted(sep))
    out.append(
        f"{_status_label('  Tokens:')}  "
        f"{_stdout_paint(f'{m.total_tokens:,} ', C.WHITE)}"
        f"{_status_muted(f'(in={m.total_tokens_in:,} out={m.total_tokens_out:,})')}"
    )
    if m.total_cost_usd_equivalent > 0.0:
        out.append(
            f"{_status_label('  Cost ref:')} "
            f"{_cost_reference_text(
                m.total_cost_usd_equivalent,
                estimated=bool(m.raw.get('cost_estimated')),
            )}"
        )
    out.append(
        f"{_status_label('  Time:')}    "
        f"{_stdout_paint(f'{m.total_duration_s:.1f}s', C.WHITE)}"
    )
    if m.total_rounds:
        out.append(
            f"{_status_label('  Rounds:')}  "
            f"{_stdout_paint(str(m.total_rounds), C.WHITE)}"
        )
    if m.total_retries:
        out.append(
            f"{_status_label('  Retries:')} "
            f"{_status_warning(str(m.total_retries))}"
        )

    if m.phases:
        show_cost = any(
            _float_metric(p.get("cost_usd_equivalent")) > 0.0
            for p in m.phases.values()
            if isinstance(p, dict)
        )
        out.append("")
        if show_cost:
            out.append(
                f"  {'Phase':<16} {'Model':<26} {'In':>10} {'Out':>10} "
                f"{'Total':>11} {'Time':>9}  {'Cost ref':>24}"
            )
            out.append(_status_muted(f"  {'─' * 116}"))
        else:
            out.append(
                f"  {'Phase':<16} {'Model':<26} {'In':>10} {'Out':>10} "
                f"{'Total':>11} {'Time':>9}"
            )
            out.append(_status_muted(f"  {'─' * 90}"))
        for phase, p in m.phases.items():
            if not isinstance(p, dict):
                continue
            tokens_in = _int_metric(p.get("tokens_in"))
            tokens_out = _int_metric(p.get("tokens_out"))
            total_tokens = _int_metric(p.get("total_tokens"))
            duration_s = _float_metric(p.get("duration_s"))
            line = (
                f"  {_stdout_paint(_clip_metrics_cell(phase, 16), C.CYAN)} "
                f"{_status_muted(_clip_metrics_cell(str(p.get('model', '')), 26))} "
                f"{_stdout_paint(f'{tokens_in:>10,}', C.WHITE)} "
                f"{_stdout_paint(f'{tokens_out:>10,}', C.WHITE)} "
                f"{_stdout_paint(f'{total_tokens:>11,}', C.WHITE)} "
                f"{_stdout_paint(f'{duration_s:>8.1f}s', C.BLUE)}"
            )
            if show_cost:
                line = f"{line}  {_metrics_cost_cell(p, width=24, key='cost_usd_equivalent')}"
            out.append(line)
    out.append(_status_muted(sep))
    out.append("")
    return "\n".join(out)


def format_metrics_history(rows: list[RunMetrics], *, runs_dir: Path | None = None) -> str:
    """Render the metrics-history block."""
    if not rows:
        if runs_dir is not None:
            return f"No runs with metrics found in {runs_dir}"
        return "No runs with metrics found."

    show_cost = any(m.total_cost_usd_equivalent > 0.0 for m in rows)
    run_col = 24
    project_col = 18
    token_col = 13
    cost_col = 24
    time_col = 9
    round_col = 3
    separator_width = 126 if show_cost else 100

    lines: list[str] = []
    lines.append("")
    lines.append(_cost_title(f"  Metrics history · last {len(rows)} runs"))
    lines.append("")

    total_tok = sum(m.total_tokens for m in rows)
    total_dur = sum(m.total_duration_s for m in rows)
    top_tokens = max(rows, key=lambda m: m.total_tokens)
    top_time = max(rows, key=lambda m: m.total_duration_s)
    top_bits = [
        f"tokens {top_tokens.run_id} ({top_tokens.total_tokens:,})",
        f"time {top_time.run_id} ({top_time.total_duration_s:.1f}s)",
    ]
    if show_cost:
        total_cost = sum(m.total_cost_usd_equivalent for m in rows)
        any_estimated = any(bool(m.raw.get("cost_estimated")) for m in rows)
        top_cost = max(rows, key=lambda m: m.total_cost_usd_equivalent)
        top_cost_text = format_cost_reference(
            top_cost.total_cost_usd_equivalent,
            estimated=bool(top_cost.raw.get("cost_estimated")),
        )
        top_bits.append(f"cost {top_cost.run_id} ({top_cost_text})")
        lines.append(
            f"  {_status_label('Total:')} "
            f"{_stdout_paint(f'{total_tok:,} tok', C.WHITE)} "
            f"{_status_muted('|')} "
            f"{_stdout_paint(f'{total_dur:.1f}s', C.WHITE)} "
            f"{_status_muted('|')} "
            f"{_cost_reference_text(total_cost, estimated=any_estimated)} "
            f"{_status_muted(f'across {len(rows)} runs')}"
        )
    else:
        lines.append(
            f"  {_status_label('Total:')} "
            f"{_stdout_paint(f'{total_tok:,} tok', C.WHITE)} "
            f"{_status_muted('|')} "
            f"{_stdout_paint(f'{total_dur:.1f}s', C.WHITE)} "
            f"{_status_muted(f'across {len(rows)} runs')}"
        )
    lines.append(f"  {_status_label('Top:')} {_status_muted(' · '.join(top_bits))}")
    lines.append("")
    header = (
        f"  {'Run ID':<{run_col}} {'Project':<{project_col}} "
        f"{'Tokens':>{token_col}} "
    )
    if show_cost:
        header += f"{'Cost ref':>{cost_col}} "
    header += f"{'Time':>{time_col}} {'Rnd':>{round_col}}  Task"
    lines.append(_status_muted(header))
    lines.append(_status_muted("  " + "─" * separator_width))
    for m in rows:
        meta = _run_metrics_meta(m)
        project_raw = meta.get("project")
        project = Path(project_raw).name if project_raw else "?"
        task = str(meta.get("task") or "?")
        run_cell = _stdout_paint(_clip_metrics_cell(m.run_id, run_col), C.GREEN)
        project_color = C.GREY if project.startswith("demo") else C.WHITE
        project_cell = _stdout_paint(
            _clip_metrics_cell(project, project_col),
            project_color,
        )
        token_cell = _stdout_paint(f"{m.total_tokens:>{token_col},}", C.WHITE)
        time_cell = _stdout_paint(f"{m.total_duration_s:>{time_col - 1}.1f}s", C.BLUE)
        round_cell = _status_muted(f"{m.total_rounds:>{round_col}}")
        task_cell = _status_muted(_clip_metrics_cell(task, 42))
        line = f"  {run_cell} {project_cell} {token_cell} "
        if show_cost:
            line += f"{_metrics_cost_cell(m.raw, width=cost_col)} "
        line += f"{time_cell} {round_cell}  {task_cell}"
        lines.append(line.rstrip())

    lines.append("")
    return "\n".join(lines)


def _clip_metrics_cell(value: object, width: int) -> str:
    text = str(value or "?").replace("\n", " ").strip() or "?"
    if len(text) > width:
        text = text[: max(width - 1, 0)].rstrip() + "…"
    return f"{text:<{width}}"


def _run_metrics_meta(metrics: RunMetrics) -> dict[str, Any]:
    raw_meta = metrics.raw.get("meta") if isinstance(metrics.raw, dict) else None
    if isinstance(raw_meta, dict):
        return raw_meta
    try:
        meta_text = (metrics.run_dir / "meta.json").read_text(encoding="utf-8")
        loaded = json.loads(meta_text)
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _metrics_cost_cell(
    raw_metrics: dict[str, Any],
    *,
    width: int,
    key: str = "total_cost_usd_equivalent",
) -> str:
    cost = _float_metric(raw_metrics.get(key))
    if cost <= 0.0:
        return _status_muted(f"{'—':>{width}}")
    rendered = format_cost_reference(
        cost,
        estimated=bool(raw_metrics.get("cost_estimated")),
    )
    color = C.YELLOW if raw_metrics.get("cost_estimated") else C.GREEN
    return _stdout_paint(f"{rendered:>{width}}", color)


# ─────────────────────────────────────────────────────────────────────────────
# cost
# ─────────────────────────────────────────────────────────────────────────────


def _cost_title(text: str) -> str:
    return _stdout_paint(text, C.BOLD, C.CYAN)


def _cost_reference_text(
    cost: float,
    *,
    estimated: bool = False,
) -> str:
    rendered = format_cost_reference(cost, estimated=estimated)
    color = C.YELLOW if estimated else C.GREEN
    return _stdout_paint(rendered, color)


def _cost_muted(text: str) -> str:
    return _stdout_paint(text, C.GREY)


def _cost_warning(text: str) -> str:
    return _stdout_paint(text, C.YELLOW)


_PHASE_ROLE_MAP = {
    "plan": "systems_architect",
    "cross_plan": "systems_architect",
    "validate_plan": "plan_reviewer",
    "cross_validate_plan": "plan_reviewer",
    "implement": "implementation_engineer",
    "repair_changes": "implementation_engineer",
    "review_changes": "code_reviewer",
    "contract_check": "code_reviewer",
    "final_acceptance": "release_manager",
    "cross_final_acceptance": "release_manager",
    "correction_triage": "release_manager",
    "handoff_advice": "release_manager",
}

_PHASE_TASK_MAP = {
    "review_changes": "code_review",
}


def _phase_role_name(phase_name: str) -> str:
    return _PHASE_ROLE_MAP.get(phase_name, "other")


def _phase_task_name(phase_name: str) -> str:
    return _PHASE_TASK_MAP.get(phase_name, phase_name)


def _derived_cost_breakdown(
    rows: tuple[PhaseBreakdown, ...],
    *,
    kind: str,
    name_for_phase,
) -> tuple[PhaseBreakdown, ...]:
    costs: dict[str, float] = {}
    tokens: dict[str, int] = {}
    runs: dict[str, int] = {}
    tokens_exact: dict[str, bool] = {}
    cost_estimated: dict[str, bool] = {}

    for row in rows:
        name = str(name_for_phase(row.name) or "other")
        costs[name] = costs.get(name, 0.0) + row.cost
        tokens[name] = tokens.get(name, 0) + row.tokens
        runs[name] = runs.get(name, 0) + row.runs
        tokens_exact[name] = tokens_exact.get(name, True) and row.tokens_exact
        cost_estimated[name] = cost_estimated.get(name, False) or row.cost_estimated

    return tuple(
        PhaseBreakdown(
            name=name,
            cost=costs[name],
            tokens=tokens[name],
            runs=runs[name],
            tokens_exact=tokens_exact[name],
            cost_estimated=cost_estimated[name],
            kind=kind,
        )
        for name in sorted(
            costs,
            key=lambda key: (costs[key], tokens[key]),
            reverse=True,
        )
    )


def _append_cost_breakdown_section(
    out: list[str],
    *,
    title: str,
    rows: tuple[PhaseBreakdown | ProjectBreakdown, ...],
    total: float,
    note: str,
) -> None:
    """Append a phase-shaped cost breakdown section."""
    if not rows:
        return
    out.append("")
    out.append(_cost_title(title))
    name_width = max(14, *(len(ph.name) for ph in rows))
    for ph in rows:
        tok_marker = " " if ph.tokens_exact else "~"
        cost_str = (
            _cost_reference_text(ph.cost, estimated=ph.cost_estimated)
            if ph.cost > 0
            else _cost_muted("  (no $)")
        )
        pct_str = (
            f"({ph.cost / total * 100.0:>4.1f}%)"
            if total and ph.cost > 0
            else "        "
        )
        out.append(
            f"    {ph.name:<{name_width}} {cost_str}  {pct_str}   "
            f"×{ph.runs}   {tok_marker}{ph.tokens:>9,} tok"
        )
    if total:
        out.append(_cost_muted(note))


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

    # Breakdown percentages are share-of-breakdown: each row's cost over the
    # sum of the rows in its own breakdown, never over report.total_cost. The
    # phase and agent sums can legitimately exceed total_cost (double counting
    # across views), so total_cost would make the percentages look like a
    # broken pie. An empty/zero breakdown suppresses the column entirely.
    phase_rows = tuple(
        ph for ph in report.phase_breakdown if ph.kind != "sub_pipeline"
    )
    phase_total = sum(ph.cost for ph in phase_rows)
    project_total = sum(project.cost for project in report.project_breakdown)
    agent_total = sum(ag.cost for ag in report.agent_breakdown)
    role_rows = _derived_cost_breakdown(
        phase_rows,
        kind="derived_role",
        name_for_phase=_phase_role_name,
    )
    task_rows = _derived_cost_breakdown(
        phase_rows,
        kind="derived_task",
        name_for_phase=_phase_task_name,
    )
    role_total = sum(ph.cost for ph in role_rows)
    task_total = sum(ph.cost for ph in task_rows)

    out.append("")
    out.append(
        _cost_title(
            f"  Cost report · window={window_label} · {report.total_runs} runs · "
            f"workspace={report.runs_dir}"
        )
    )
    out.append(_cost_muted(f"  {'─' * 70}"))

    # Top-N runs by cost reference.
    if report.top_runs:
        out.append("")
        out.append(_cost_title(f"  Top {len(report.top_runs)} runs by cost reference:"))
        for r in report.top_runs:
            cost_str = (
                _cost_reference_text(r.cost, estimated=r.cost_estimated)
                if r.cost > 0
                else _cost_muted("    -- ")
            )
            tags: list[str] = []
            if r.rounds > 1:
                tags.append(f"rounds×{r.rounds}")
            if r.retries:
                tags.append(f"retries×{r.retries}")
            tag_str = _cost_warning(f"  ⚠ {', '.join(tags)}") if tags else ""
            out.append(
                f"    {r.run_id}  {cost_str}  {r.task:<50}{tag_str}"
            )

    _append_cost_breakdown_section(
        out,
        title="  By workspace project (project runs + cross-project slices):",
        rows=report.project_breakdown,
        total=project_total,
        note=(
            "    ↳ Project rows are matched by workspace-local project path; "
            "cross-level orchestration stays in phase rows."
        ),
    )

    _append_cost_breakdown_section(
        out,
        title="  By phase (sum across runs):",
        rows=phase_rows,
        total=phase_total,
        note=(
            "    ↳ % = share of the phase breakdown (sum of the rows), "
            "not of the window total."
        ),
    )

    _append_cost_breakdown_section(
        out,
        title="  By role (derived from phase map):",
        rows=role_rows,
        total=role_total,
        note=(
            "    ↳ Roles are derived from phase names; cross-project slices "
            "are counted in the workspace project section."
        ),
    )

    _append_cost_breakdown_section(
        out,
        title="  By task (derived from phase map):",
        rows=task_rows,
        total=task_total,
        note=(
            "    ↳ Tasks are derived from phase names; cross-project slices "
            "are counted in the workspace project section."
        ),
    )

    # By runtime/provider.
    if report.agent_breakdown:
        any_estimated = any(not a.tokens_exact for a in report.agent_breakdown)
        out.append("")
        out.append(_cost_title("  By runtime/provider (sum across phases):"))
        for ag in report.agent_breakdown:
            pct = (ag.cost / agent_total * 100.0) if agent_total else 0.0
            tok_marker = " " if ag.tokens_exact else "~"
            cost_str = (
                _cost_reference_text(ag.cost, estimated=ag.cost_estimated)
                if ag.cost > 0
                else _cost_muted("  (no $)")
            )
            pct_str = f"({pct:>4.1f}%)" if ag.cost > 0 else "        "
            hint = runtime_accounting_hint(ag.provider)
            hint_str = f"  {_cost_muted(hint)}" if hint else ""
            out.append(
                f"    {ag.provider:<10} {cost_str}  {pct_str}   "
                f"×{ag.runs}   {tok_marker}{ag.tokens:>9,} tok{hint_str}"
            )
        if any_estimated:
            out.append(
                _cost_muted(
                    "    ↳ ``~`` = token count includes at least one estimated "
                    "entry (provider didn't surface usage)."
                )
            )

    # Top-phase note.
    if has_cost and phase_rows:
        top = phase_rows[0]
        top_pct = (top.cost / phase_total * 100.0) if phase_total else 0.0
        out.append("")
        out.append(
            f"  ↳ Top phase: ``{top.name}`` at {top_pct:.0f}% of the phase-breakdown "
            f"cost this window. Lower ``phases.{top.name}.effort`` to shrink it."
        )

    # Totals.
    out.append("")
    out.append("  Totals:")
    if has_cost:
        out.append(
            "    Cost reference  "
            f"{_cost_reference_text(report.total_cost, estimated=report.any_estimated)}"
        )
    else:
        out.append(
            "    Cost reference  "
            f"{_cost_muted('— (no run reported cost; token-only, mock, or old runs?)')}"
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
        out.append(_cost_muted(f"  ↳ {ACCOUNTING_REFERENCE_NOTE}"))

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
                f" — {_cost_warning(f'⚠ {age} days old')}; "
                f"``orcho pricing refresh`` to update."
            )
        out.append(format_estimated_entries_footer(n, src, age_warn))
    out.append("")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# pricing
# ─────────────────────────────────────────────────────────────────────────────


def format_pricing(table: PricingTable) -> str:
    """Reproduce `cmd_pricing_show` (cli/orcho.py:428-467)."""
    out: list[str] = []
    out.append("")
    out.append(_cost_title("  Pricing reference · OpenAI/Codex estimates"))
    out.append(_cost_muted("  " + "─" * 64))
    out.append(_cost_muted(
        "  Reference only: used for estimated-api cost, not as a billing receipt."
    ))
    out.append("")
    out.append("  Sources")
    if table.user_snapshot_date:
        out.append(
            f"    local rates:    ~/.orcho/pricing.local.toml "
            f"({table.user_snapshot_date})"
        )
    else:
        out.append("    local rates:    ~/.orcho/pricing.local.toml (not present)")
    if table.bundled_snapshot_date:
        out.append(f"    bundled rates:  {table.bundled_snapshot_date}")
    else:
        out.append("    bundled rates:  none (Orcho ships no hardcoded rates)")
    if table.snapshot_age_days is not None:
        age = f"{table.snapshot_age_days} days"
        if table.snapshot_age_days > 30:
            age = _cost_warning(f"{age}  ⚠ stale")
        out.append(f"    age:            {age}")
    out.append("")

    if not table.entries:
        out.append("  No models priced yet.")
        out.append("  Populate via:  orcho pricing refresh --provider openai")
        out.append("  Or hand-edit:  ~/.orcho/pricing.local.toml")
        out.append("")
        return "\n".join(out)

    out.append("  Rates")
    out.append(f"    {'model':<28} {'in $/1M':>10} {'out $/1M':>10}  source")
    out.append(_cost_muted(f"    {'─' * 64}"))
    for e in table.entries:
        in_str = (
            f"{e.input_per_million:>10.2f}"
            if e.input_per_million is not None
            else f"{'-':>10}"
        )
        out_str = (
            f"{e.output_per_million:>10.2f}"
            if e.output_per_million is not None
            else f"{'-':>10}"
        )
        out.append(
            f"    {e.model:<28} {in_str} {out_str}  {e.source}"
        )
    out.append("")
    out.append("  How Orcho uses this")
    out.append(
        "    OpenAI/Codex token-only phases can be shown as estimated-api cost."
    )
    out.append(
        "    Runtimes that report native cost use their own reported values."
    )
    out.append(
        "    Other runtimes show cost only when Orcho has parsed cost or a "
        "matching rate card."
    )
    out.append("")
    out.append(
        _cost_muted(
            "  Next: orcho pricing refresh · edit ~/.orcho/pricing.local.toml"
        )
    )
    out.append(
        _cost_muted(
            "  Verify current rates before relying on estimates:\n"
            "    https://developers.openai.com/api/docs/pricing"
        )
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
    compact: bool = False,
) -> str:
    """List view: ``orcho prompts`` / ``orcho prompts --list``.

    `winners` maps prompt name → resolved level (``"core"`` /
    ``"workspace"`` / ``"project"`` / ``"unknown"``).
    """
    def grouped_prompt_names() -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {
            "formats": [],
            "roles": [],
            "tasks": [],
            "other": [],
        }
        for name in sorted(names):
            prefix = name.split("/", 1)[0] if "/" in name else "other"
            key = prefix if prefix in groups else "other"
            groups[key].append(name)
        return {key: value for key, value in groups.items() if value}

    def group_label(group: str) -> str:
        return {
            "formats": "Formats",
            "roles": "Roles",
            "tasks": "Tasks",
            "other": "Other",
        }.get(group, group.title())

    groups = grouped_prompt_names()
    out: list[str] = []
    out.append("")
    out.append(_status_section(f"  Prompt catalog · {len(names)} available"))
    if project_dir:
        out.append(f"  Project: {project_dir}")
    out.append("")

    if compact:
        out.append("  Groups")
        for group, entries in groups.items():
            short_names = [
                name.split("/", 1)[1] if "/" in name else name
                for name in entries
            ]
            sample = ", ".join(short_names[:3])
            if len(short_names) > 3:
                sample = f"{sample}, +{len(short_names) - 3} more"
            out.append(f"    {group_label(group):<8} {len(entries):>2}  {sample}")
        out.append("")
        out.append(_status_muted(
            "  Next: orcho prompts --list · orcho prompts tasks/plan · "
            "orcho prompts tasks/plan --verbose"
        ))
    else:
        for group, entries in groups.items():
            out.append(f"  {group_label(group)} ({len(entries)})")
            for name in entries:
                winner = winners.get(name, "unknown")
                out.append(f"    {name:<36} [{winner}]")
            out.append("")
        out.append(_status_muted(
            "  Next: orcho prompts <name> · orcho prompts <name> --verbose"
        ))
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
            "Agent rules template:",
            "Claude shim template:",
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
        missing_runtimes = getattr(result, "missing_runtimes", ())
        runtime_override = getattr(result, "runtime_override", None)
        if runtime_override:
            switched = ", ".join(f"'{name}'" for name in missing_runtimes)
            note = (
                f"phases configured for {switched} were switched to "
                f"'{runtime_override}' in the workspace config."
            )
            out.append(
                f"    {paint('Note:', C.GREEN)} {paint(note, C.GREY)}"
            )
        elif missing_runtimes:
            names = ", ".join(f"'{name}'" for name in missing_runtimes)
            warning = (
                f"configured runtime(s) {names} not found on PATH — install "
                "them, or re-run `orcho workspace init` interactively to "
                "switch the workspace config to an installed runtime."
            )
            out.append(
                f"    {paint('Warning:', C.YELLOW, C.BOLD)} "
                f"{paint(warning, C.YELLOW)}"
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


def format_verify_overview() -> str:
    """Render the bare ``orcho verify`` landing view."""
    out: list[str] = [""]
    out.append(_status_section("  Verify · declared receipts for a run"))
    out.append("")
    out.append("  What do you need?")
    out.append("    env   Check the declared verification environment and write an env receipt")
    out.append("    list  Show declared commands without running them")
    out.append("    run   Execute declared commands and write command receipts")
    out.append("")
    out.append("  Common commands")
    out.append("    orcho verify env")
    out.append("    orcho verify list")
    out.append("    orcho verify run --required")
    out.append("    orcho verify run lint")
    out.append("")
    out.append(_status_muted(
        "  Tip: add --run-id <id> and --project <path> to verify a specific run."
    ))
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
    out.append(f"  source:   {subject.get('source', '')}")
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
    def render_run_command(value: object) -> str:
        if isinstance(value, (list, tuple)):
            return shlex.join(str(part) for part in value)
        text = str(value or "")
        if text[:1] in {"[", "("}:
            try:
                parsed = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                return text
            if isinstance(parsed, (list, tuple)):
                return shlex.join(str(part) for part in parsed)
        return text

    out: list[str] = [
        "",
        _status_section(f"  verify list · {len(result.commands)} declared command(s)"),
    ]
    out.append(_status_muted("  Preview only: nothing executed, no receipts written."))
    out.append(f"  checkout: {result.subject_checkout}")
    out.append(f"  source:   {result.subject_source}")
    out.append(_status_muted("  * = required"))
    out.append("")
    for cmd in result.commands:
        marker = "*" if cmd.get("required") else " "
        name = cmd.get("name", "")
        env_ref = cmd.get("env", "")
        run = render_run_command(cmd.get("run_resolved", ""))
        out.append(f"  {marker} {name}  [env={env_ref}]")
        out.append(f"      $ {run}")
    if not result.commands:
        out.append("  (none)")
    out.append("")
    out.append(_status_muted(
        "  Next: orcho verify run --required · orcho verify run <name>"
    ))
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
    out.append(f"  checkout: {result.subject_checkout}")
    out.append(f"  source:   {result.subject_source}")
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
