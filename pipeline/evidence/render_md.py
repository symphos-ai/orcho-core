"""pipeline.evidence.render_md — Markdown rendering of evidence bundles.

Produces a deterministic, human-readable summary from the v1 bundle
dict. Used by:

* ``orcho evidence <run_id> --format md`` (CLI)
* ``write_bundle`` writes ``evidence.md`` next to ``evidence.json``
* Snapshot tests that diff against a frozen golden output

Determinism rules:

* Same bundle dict → identical markdown bytes. No timestamps beyond
  what the bundle itself records, no random ids, no hash of unsorted
  inputs.
* Lists render in source order — the bundle already orders them by
  event seq.
"""
from __future__ import annotations

from typing import Any


def render_evidence_md(bundle: dict[str, Any], *, debug: bool = False) -> str:
    """Render a v1 evidence bundle as a markdown string.

    Args:
        bundle: dict produced by :func:`pipeline.evidence.collector.collect_evidence`.
            Placeholder bundles (``schema_version="0-placeholder"``)
            render a short stub describing what's missing.

    Returns:
        Markdown text. Trailing newline included. By default, live diagnostic
        breadcrumbs are hidden from the human markdown view; pass
        ``debug=True`` to render every stored diagnostic record.
    """
    if bundle.get("schema_version") == "0-placeholder":
        return _render_placeholder(bundle)

    lines: list[str] = []
    lines.append(f"# Run evidence — `{bundle['run_id']}`")
    lines.append("")
    lines.extend(_render_header(bundle))
    lines.extend(_render_worktree(bundle.get("worktree"), bundle.get("worktree_projects")))
    lines.extend(_render_plan(bundle["plan"]))
    lines.extend(_render_phases(bundle["phases"]))
    lines.extend(_render_gates(bundle["gates"]))
    lines.extend(_render_findings(bundle.get("findings") or []))
    lines.extend(_render_handoff_advice(bundle.get("handoff_advice")))
    lines.extend(_render_commands(bundle["commands"]))
    lines.extend(_render_artifacts(bundle["artifacts"]))
    lines.extend(_render_metrics(bundle["metrics"]))
    lines.extend(_render_errors(bundle["errors"], debug=debug))
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── Sections ──────────────────────────────────────────────────────────────


def _render_header(bundle: dict[str, Any]) -> list[str]:
    lines = [
        "## Summary",
        "",
        f"- **Status:** `{bundle['status']}`",
    ]
    lines.extend(_render_status_detail(bundle))
    lines.extend([
        f"- **Task:** {bundle['task'] or '_(not recorded)_'}",
        f"- **Profile:** `{bundle['profile'] or '?'}`",
        f"- **Run dir:** `{bundle['run_dir']}`",
        f"- **Bundle generated:** {bundle['created_at']}",
        f"- **Schema:** `{bundle['schema_version']}`",
        "",
    ])
    return lines


def _render_status_detail(bundle: dict[str, Any]) -> list[str]:
    status = str(bundle.get("status") or "")
    lines: list[str] = []
    errors = bundle.get("errors") or []

    terminal_reason = _terminal_reason_detail(errors)
    if terminal_reason is not None:
        lines.append(f"- **Terminal reason:** {terminal_reason}")

    if status in {"running", "awaiting_phase_handoff", "interrupted"}:
        if status == "awaiting_phase_handoff":
            pending = _pending_handoff_detail(errors)
            if pending is not None:
                lines.append(f"- **Pending handoff:** {pending}")

        active_phases = _active_phase_details(bundle.get("phases") or [])
        if active_phases:
            label = "Active phase" if len(active_phases) == 1 else "Active phases"
            lines.append(f"- **{label}:** {', '.join(active_phases)}")
        else:
            last_phase = _last_phase_detail(bundle.get("phases") or [])
            if last_phase is not None:
                lines.append(f"- **Last phase:** {last_phase}")

    gate_attention = _gate_attention_detail(bundle.get("gates") or [])
    if gate_attention is not None:
        lines.append(f"- **Gate attention:** {gate_attention}")

    return lines


def _terminal_reason_detail(errors: list[dict[str, Any]]) -> str | None:
    for err in reversed(errors):
        kind = err.get("kind")
        if kind not in {"run_failed", "run_halted"}:
            continue
        message = str(err.get("message") or "").strip()
        if not message:
            message = str(kind)
        return f"`{message}`"
    return None


def _pending_handoff_detail(errors: list[dict[str, Any]]) -> str | None:
    for err in reversed(errors):
        if err.get("kind") != "phase_handoff_requested":
            continue
        handoff_id = str(err.get("handoff_id") or "")
        if not handoff_id:
            continue
        bits = [f"`{handoff_id}`"]
        phase = str(err.get("phase") or "")
        trigger = str(err.get("message") or "")
        extra = []
        if phase:
            extra.append(f"phase `{phase}`")
        if trigger:
            extra.append(f"trigger `{trigger}`")
        if extra:
            bits.append(f"({', '.join(extra)})")
        return " ".join(bits)
    return None


def _active_phase_details(phases: list[dict[str, Any]]) -> list[str]:
    active = [
        p for p in phases
        if p.get("outcome") == "in_progress" and not p.get("ended_at")
    ]
    return [_phase_detail(p, include_outcome=False) for p in active]


def _last_phase_detail(phases: list[dict[str, Any]]) -> str | None:
    if not phases:
        return None
    return _phase_detail(phases[-1], include_outcome=True)


def _phase_detail(phase: dict[str, Any], *, include_outcome: bool) -> str:
    name = str(phase.get("name") or "?")
    attempt = phase.get("attempt", "?")
    title = str(phase.get("title") or "").strip()
    detail = f"`{name}` attempt {attempt}"
    if title and title != name:
        detail = f"{detail} — {title}"
    if include_outcome:
        detail = f"{detail} (`{phase.get('outcome', '?')}`)"
    return detail


def _gate_attention_detail(gates: list[dict[str, Any]]) -> str | None:
    attention = [
        g for g in gates
        if str(g.get("outcome") or "") in {"failed", "skipped", "in_progress"}
    ]
    if not attention:
        return None
    by_outcome: dict[str, list[str]] = {}
    for gate in attention:
        outcome = str(gate.get("outcome") or "unknown")
        by_outcome.setdefault(outcome, []).append(str(gate.get("name") or "?"))
    parts = []
    for outcome in sorted(by_outcome):
        names = _counted_name_list(by_outcome[outcome])
        parts.append(f"{len(by_outcome[outcome])} {outcome} ({names})")
    return "; ".join(parts)


def _counted_name_list(names: list[str]) -> str:
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    parts = []
    for name, count in counts.items():
        suffix = f" x{count}" if count > 1 else ""
        parts.append(f"`{name}`{suffix}")
    return ", ".join(parts)


def _render_worktree(
    worktree: dict[str, Any] | None,
    worktree_projects: dict[str, Any] | None,
) -> list[str]:
    lines = ["## Worktree", ""]
    if worktree is None:
        lines.extend(["_No worktree context recorded._", ""])
        return lines
    # Wire key is ``isolation`` (ADR 0033 to_dict shape).
    isolation = worktree.get("isolation", "off")
    if isolation == "off":
        reason = worktree.get("degraded_reason") or "disabled"
        lines.append(f"- **Isolation:** `off` ({reason})")
    else:
        lines.append(f"- **Isolation:** `{isolation}`")
        if worktree.get("path"):
            lines.append(f"- **Path:** `{worktree['path']}`")
        if worktree.get("branch_ref"):
            lines.append(f"- **Branch:** `{worktree['branch_ref']}`")
        if worktree.get("base_ref"):
            lines.append(f"- **Base ref:** `{worktree['base_ref']}`")
        if worktree.get("retention_until"):
            lines.append(f"- **Retention until:** {worktree['retention_until']}")
        if worktree.get("degraded_reason"):
            lines.append(f"- **Degraded:** {worktree['degraded_reason']}")
    if worktree_projects:
        lines.append("")
        lines.append("**Per-project worktrees:**")
        lines.append("")
        for alias, ctx in worktree_projects.items():
            if not isinstance(ctx, dict):
                continue
            alias_isolation = ctx.get("isolation", "off")
            alias_path = ctx.get("path", "?")
            lines.append(f"- `{alias}`: isolation=`{alias_isolation}`, path=`{alias_path}`")
    lines.append("")
    return lines


def _render_plan(plan: dict[str, Any]) -> list[str]:
    lines = ["## Plan", ""]
    if plan.get("source") == "absent":
        lines.append("_No structured plan recorded — architect emitted prose only._")
        lines.append("")
        return lines
    if plan.get("goal"):
        lines.append(f"**Goal:** {plan['goal']}")
        lines.append("")
    if plan.get("short_summary"):
        lines.append(f"**Short summary:** {plan['short_summary']}")
        lines.append("")
    if plan.get("planning_context"):
        lines.append("**Planning context:**")
        lines.append("")
        lines.append(str(plan["planning_context"]))
        lines.append("")
    lines.append(f"- **Source:** `{plan['source']}`")
    lines.append(f"- **Subtasks:** {plan['subtask_count']}")
    lines.append(f"- **Has typed contract:** {'yes' if plan['has_contract'] else 'no'}")
    lines.append(f"- **Acceptance criteria:** {len(plan['acceptance_criteria'])}")
    lines.append(f"- **Owned files:** {len(plan['owned_files'])}")
    lines.append(f"- **Commands to run:** {len(plan['commands_to_run'])}")
    if plan.get("mcp_context"):
        lines.append(f"- **MCP context entries:** {len(plan['mcp_context'])}")
    lines.append("")
    return lines


def _render_phases(phases: list[dict[str, Any]]) -> list[str]:
    lines = ["## Phase timeline", ""]
    if not phases:
        lines.extend(["_No phases recorded._", ""])
        return lines
    lines.append("| # | Phase | Title | Outcome | Attempt |")
    lines.append("|---|-------|-------|---------|---------|")
    for i, p in enumerate(phases, start=1):
        title = (p.get("title") or "").replace("|", "\\|")
        lines.append(
            f"| {i} | `{p['name']}` | {title} | "
            f"`{p['outcome']}` | {p['attempt']} |"
        )
    lines.append("")
    return lines


def _render_gates(gates: list[dict[str, Any]]) -> list[str]:
    lines = ["## Quality gates", ""]
    if not gates:
        lines.extend(["_No gates ran._", ""])
        return lines
    lines.append("| Gate | Kind | Outcome | Duration |")
    lines.append("|------|------|---------|----------|")
    for g in gates:
        lines.append(
            f"| `{g['name']}` | `{g['kind']}` | `{g['outcome']}` | "
            f"{g['duration_s']:.2f}s |"
        )
    lines.append("")
    return lines


def _render_findings(findings: list[dict[str, Any]]) -> list[str]:
    """Render reviewer findings — preserves source order from the bundle.

    Bundle ordering is causal: phase order matches
    ``_FINDING_BEARING_PHASES`` in the collector, then attempt index,
    then within-attempt source order. We do not re-sort by severity
    here — the chain that produced a finding is the useful frame for
    a human reading the run.

    Empty findings list still renders the heading + a no-findings
    line so an APPROVED run reads as deliberately clean rather than
    looking like a missing section.
    """
    lines = ["## Findings", ""]
    if not findings:
        lines.extend(["_No review findings recorded._", ""])
        return lines
    for f in findings:
        title = (f.get("title") or "").replace("|", "\\|")
        header = (
            f"### `{f.get('severity', 'P3')}` "
            f"{title or '_(no title)_'}"
        )
        lines.append(header)
        lines.append("")
        finding_id = f.get("id") or ""
        meta_bits: list[str] = []
        if finding_id:
            meta_bits.append(f"**ID:** `{finding_id}`")
        meta_bits.append(f"**Phase:** `{f.get('phase', '')}`")
        meta_bits.append(f"**Attempt:** {f.get('attempt', '?')}")
        if f.get("file"):
            loc = f["file"]
            if f.get("line"):
                loc = f"{loc}:{f['line']}"
            meta_bits.append(f"**Location:** `{loc}`")
        lines.append(" · ".join(meta_bits))
        lines.append("")
        body = (f.get("body") or "").strip()
        if body:
            lines.append(body)
            lines.append("")
        required_fix = (f.get("required_fix") or "").strip()
        if required_fix:
            lines.append(f"**Required fix:** {required_fix}")
            lines.append("")
    return lines


def _render_handoff_advice(advice: dict[str, Any] | None) -> list[str]:
    """Render the handoff-advice digest — ONLY when advice was actually given.

    Unlike the always-present ``## Findings`` section, this one is omitted
    entirely when the bundle carries no ``handoff_advice`` key (no Stage 0/1
    advice surface): a run that never paused for advice should read as such,
    not show an empty "Agent advice" heading. Covers both the human-driven
    (``agent_advice``) and CI-policy (``ci_agent``) sources via the
    per-call ``feedback_source`` and the classified ``outcome``.
    """
    calls = (advice or {}).get("calls") if isinstance(advice, dict) else None
    if not calls:
        return []
    summary = advice.get("summary") if isinstance(advice.get("summary"), dict) else {}

    lines = ["## Agent advice", ""]
    lines.append(
        f"- **Calls:** {summary.get('calls', len(calls))} "
        f"(applied retries: {summary.get('applied_retries', 0)})"
    )
    lines.append(
        f"- **Outcomes:** resolved={summary.get('resolved_retries', 0)}, "
        f"repeated={summary.get('repeated', 0)}, "
        f"stopped={summary.get('stopped', 0)}, "
        f"unknown={summary.get('unknown', 0)}"
    )
    usage = summary.get("usage")
    if isinstance(usage, dict) and usage:
        bits = [
            f"in={usage['tokens_in']:,}" if "tokens_in" in usage else "",
            f"out={usage['tokens_out']:,}" if "tokens_out" in usage else "",
        ]
        if "cost_usd_equivalent" in usage:
            bits.append(f"cost=${usage['cost_usd_equivalent']:.4f}")
        joined = ", ".join(b for b in bits if b)
        if joined:
            lines.append(f"- **Advice usage:** {joined}")
    lines.append("")
    lines.append(
        "| Phase | Source | Recommended | Applied | Confidence | Outcome |"
    )
    lines.append("|-------|--------|-------------|---------|------------|---------|")
    for c in calls:
        source = c.get("feedback_source") or "—"
        applied = c.get("applied_action") or "—"
        confidence = c.get("confidence") or "—"
        lines.append(
            f"| `{c.get('phase', '')}` | `{source}` | "
            f"`{c.get('recommended_action', '')}` | `{applied}` | "
            f"`{confidence}` | `{c.get('outcome', '')}` |"
        )
    lines.append("")
    return lines


def _render_commands(commands: list[dict[str, Any]]) -> list[str]:
    lines = ["## Commands", ""]
    if not commands:
        lines.extend([
            "_No shell commands were recorded in the evidence event stream._",
            "",
        ])
        return lines
    lines.append("| # | Command | Exit | Outcome | Duration |")
    lines.append("|---|---------|------|---------|----------|")
    for i, c in enumerate(commands, start=1):
        argv = (c.get("argv_summary") or "").replace("|", "\\|")
        lines.append(
            f"| {i} | `{argv}` | {c.get('exit_code')} | "
            f"`{c['outcome']}` | {c['duration_s']:.2f}s |"
        )
    lines.append("")
    return lines


def _render_artifacts(artifacts: list[dict[str, Any]]) -> list[str]:
    lines = ["## Artifacts", ""]
    if not artifacts:
        lines.extend(["_No artifacts recorded._", ""])
        return lines
    show_apply_check = any(isinstance(a.get("apply_check"), dict) for a in artifacts)
    if show_apply_check:
        lines.append("| Kind | Path | Size | Apply check |")
        lines.append("|------|------|------|-------------|")
    else:
        lines.append("| Kind | Path | Size |")
        lines.append("|------|------|------|")
    for a in artifacts:
        base = f"| `{a['kind']}` | `{a['path']}` | {a['size_bytes']} B |"
        if show_apply_check:
            base += f" {_format_apply_check(a.get('apply_check'))} |"
        lines.append(base)
    lines.append("")
    return lines


def _format_apply_check(value: Any) -> str:
    if not isinstance(value, dict):
        return "-"
    status = str(value.get("status", "unknown"))
    reason = str(value.get("reason", "")).strip()
    if reason:
        return f"`{status}` {reason}"
    return f"`{status}`"


def _render_metrics(metrics: dict[str, Any]) -> list[str]:
    lines = ["## Metrics", ""]
    lines.append(
        f"- **Tokens:** {metrics['total_tokens']:,} "
        f"(in={metrics['total_tokens_in']:,}, "
        f"out={metrics['total_tokens_out']:,})"
    )
    lines.append(f"- **Duration:** {metrics['total_duration_s']:.2f}s")
    lines.append(f"- **Repair rounds:** {metrics['total_rounds']}")
    if "total_retries" in metrics:
        lines.append(f"- **Retries:** {metrics['total_retries']}")
    lines.append("")
    return lines


def _render_errors(errors: list[dict[str, Any]], *, debug: bool = False) -> list[str]:
    lines = ["## Errors", ""]
    visible_errors, hidden_live_stalls = _visible_errors(errors, debug=debug)
    if not visible_errors:
        if hidden_live_stalls:
            lines.extend([
                "_No operator-facing errors recorded._",
                _hidden_live_stalls_line(hidden_live_stalls),
                "",
            ])
        else:
            lines.extend(["_No errors recorded._", ""])
        return lines
    for err in visible_errors:
        kind = err.get("kind", "error")
        msg = (err.get("message") or "").replace("\n", " ")
        if kind == "command_stalled":
            lines.append(_format_command_stalled_error(err))
            continue
        if kind == "phase_handoff_waiver":
            # Verdict-exception: a REJECTED verdict the operator accepted
            # with a durable waiver, injected into downstream review gates.
            phase = err.get("phase") or "?"
            lines.append(
                f"- `{kind}` (operator waiver on `{phase}`, verdict left "
                f"REJECTED): {msg}"
            )
            findings = err.get("findings")
            if isinstance(findings, list) and findings:
                lines.append(f"  - waived findings: {len(findings)}")
            continue
        lines.append(f"- `{kind}`: {msg}")
    if hidden_live_stalls:
        lines.append(_hidden_live_stalls_line(hidden_live_stalls))
    lines.append("")
    return lines


def _hidden_live_stalls_line(count: int) -> str:
    suffix = "event" if count == 1 else "events"
    return (
        f"- `command_stalled`: {count} live diagnostic {suffix} hidden; "
        "rerun with `--debug` for details"
    )


def _visible_errors(
    errors: list[dict[str, Any]], *, debug: bool,
) -> tuple[list[dict[str, Any]], int]:
    if debug:
        return errors, 0
    visible: list[dict[str, Any]] = []
    hidden_live_stalls = 0
    for err in errors:
        if err.get("kind") == "command_stalled" and err.get("terminal") is not True:
            hidden_live_stalls += 1
            continue
        visible.append(err)
    return visible, hidden_live_stalls


def _format_command_stalled_error(err: dict[str, Any]) -> str:
    mode = "terminal" if err.get("terminal") is True else "live"
    phase = str(err.get("phase") or "?")
    elapsed = err.get("elapsed_s")
    reason = str(err.get("reason") or "").strip() or "no reason recorded"
    prefix = f"- `command_stalled` ({mode}, phase `{phase}`"
    if isinstance(elapsed, (int, float)):
        prefix = f"{prefix}, {elapsed:.2f}s"
    prefix = f"{prefix}): {reason}"
    preview = _compact_inline(str(err.get("command_preview") or ""), limit=160)
    if preview:
        prefix = f"{prefix} — `{preview}`"
    return prefix


def _compact_inline(value: str, *, limit: int) -> str:
    text = " ".join(value.strip().split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _render_placeholder(bundle: dict[str, Any]) -> str:
    rid = bundle.get("run_id", "?")
    status = bundle.get("status", "?")
    return (
        f"# Run evidence — `{rid}` (placeholder)\n"
        "\n"
        "_The run did not finalize a full evidence bundle. Only the "
        "REA-0 placeholder is present. Re-run `orcho evidence` after "
        "the pipeline completes to generate the v1 bundle._\n"
        "\n"
        f"- **Status:** `{status}`\n"
        f"- **Bundle path:** `{bundle.get('run_dir', '?')}`\n"
    )
