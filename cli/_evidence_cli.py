"""Operator-first terminal rendering for ``orcho evidence``."""
from __future__ import annotations

import sys
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

from core.io.ansi import C, paint
from pipeline.evidence.finding_lifecycle import (
    ACTIVE_FINDING_STATUSES,
    FINDING_STATUS_ORDER,
    finding_status_sort_key,
)


def format_evidence_cli(bundle: Any, *, debug: bool = False) -> str:
    """Render an evidence bundle as compact CLI output."""
    body = _bundle_body(bundle)
    if body.get("schema_version") == "0-placeholder":
        return _render_placeholder(body)

    lines: list[str] = []
    sep = "─" * 72
    run_id = str(body.get("run_id") or "?")
    status = str(body.get("status") or "?")

    lines.append("")
    lines.append(_muted(sep))
    lines.append(
        f"{_label('  Evidence:')} {_good(run_id)}  "
        f"{_label('status=')}{_state(status)}"
    )
    lines.append(_muted(sep))
    lines.extend(_summary_lines(body))
    lines.extend(_plan_lines(body.get("plan") if isinstance(body.get("plan"), dict) else {}))
    lines.extend(_phase_lines(body.get("phases") or [], debug=debug))
    lines.extend(_gate_lines(body.get("gates") or [], body=body, debug=debug))
    lines.extend(_command_lines(body.get("commands") or [], body=body, debug=debug))
    lines.extend(_finding_lines(body.get("findings") or [], body=body, debug=debug))
    lines.extend(_operator_decision_lines(body.get("errors") or []))
    lines.extend(_metric_lines(body.get("metrics") or {}))
    lines.extend(_artifact_lines(body.get("artifacts") or [], debug=debug))
    lines.extend(_error_lines(body.get("errors") or [], status=status, debug=debug))
    lines.append(_muted(sep))
    lines.append("")
    return "\n".join(lines)


def _bundle_body(bundle: Any) -> dict[str, Any]:
    body = getattr(bundle, "body", bundle)
    return body if isinstance(body, dict) else {}


def _render_placeholder(body: dict[str, Any]) -> str:
    run_id = str(body.get("run_id") or "?")
    status = str(body.get("status") or "?")
    return (
        "\n"
        f"{_muted('─' * 72)}\n"
        f"{_label('  Evidence:')} {_good(run_id)}  {_label('status=')}{_state(status)}\n"
        f"  {_warn('Placeholder bundle only; full evidence was not finalized.')}\n"
        f"{_muted('─' * 72)}\n"
    )


def _summary_lines(body: dict[str, Any]) -> list[str]:
    task = _clip(body.get("task") or "(not recorded)", 112)
    profile = body.get("profile") or "?"
    run_dir = body.get("run_dir") or "?"
    reasons = _attention_reasons(body)
    attention = "yes" if reasons else "no"
    lines = [
        f"{_label('  Task:')}    {_plain(task)}",
        f"{_label('  Profile:')} {_plain(profile)}",
        f"{_label('  Run dir:')} {_muted(str(run_dir))}",
        (
            f"{_label('  Attention:')} "
            f"{_warn(attention) if reasons else _good(attention)}"
            f"{_muted(' · ' + '; '.join(reasons)) if reasons else ''}"
        ),
    ]
    release = _release_summary_line(body)
    if release:
        lines.append(release)
    return lines


def _attention_reasons(body: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    gates = body.get("gates") if isinstance(body.get("gates"), list) else []
    gate_counts = _counts(
        str(g.get("outcome") or "unknown")
        for g in gates
        if isinstance(g, dict)
        and str(g.get("outcome") or "") in {"failed", "skipped", "in_progress"}
    )
    for outcome, count in gate_counts.items():
        reasons.append(f"{count} gate {outcome}")
    errors = body.get("errors") if isinstance(body.get("errors"), list) else []
    visible_errors = [
        e for e in errors
        if isinstance(e, dict)
        and e.get("kind") not in {"phase_handoff_requested", "phase_handoff_waiver"}
    ]
    if visible_errors:
        reasons.append(f"{len(visible_errors)} error breadcrumb")
    waivers = _waivers(errors)
    if waivers:
        reasons.append(f"{len(waivers)} operator waiver")
    findings = body.get("findings") if isinstance(body.get("findings"), list) else []
    active = _active_findings(findings, waivers)
    if active:
        reasons.append(f"{len(active)} active finding")
    return reasons


def _release_summary_line(body: dict[str, Any]) -> str:
    rows = body.get("release_summary")
    if not isinstance(rows, list) or not rows:
        return ""
    latest = next((r for r in reversed(rows) if isinstance(r, dict)), None)
    if latest is None:
        return ""
    verdict = str(latest.get("verdict") or latest.get("release_verdict") or "?")
    summary = _clip(latest.get("summary") or latest.get("short_summary") or "", 96)
    suffix = f" {_muted(summary)}" if summary else ""
    return f"{_label('  Release:')} {_state(verdict)}{suffix}"


def _plan_lines(plan: dict[str, Any]) -> list[str]:
    lines = ["", _section("Plan")]
    if not plan:
        return lines + [f"  {_muted('No structured plan recorded.')}"]
    summary = plan.get("short_summary") or plan.get("goal") or ""
    if summary:
        lines.append(f"  {_plain(_clip(summary, 118))}")
    counts = [
        f"source={plan.get('source', '?')}",
        f"subtasks={plan.get('subtask_count', 0)}",
        f"contract={'yes' if plan.get('has_contract') else 'no'}",
        f"acceptance={len(plan.get('acceptance_criteria') or [])}",
        f"owned_files={len(plan.get('owned_files') or [])}",
        f"planned_commands={len(plan.get('commands_to_run') or [])}",
    ]
    lines.append(f"  {_muted(' · '.join(counts))}")
    return lines


def _phase_lines(phases: list[Any], *, debug: bool) -> list[str]:
    lines = ["", _section("Phases")]
    dict_phases = [p for p in phases if isinstance(p, dict)]
    if not dict_phases:
        return lines + [f"  {_muted('No phases recorded.')}"]
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for phase in dict_phases:
        grouped.setdefault(str(phase.get("name") or "?"), []).append(phase)
    rows = list(grouped.items()) if debug else list(grouped.items())[:8]
    for name, attempts in rows:
        meaningful = [
            attempt for attempt in attempts
            if not str(attempt.get("outcome") or "").startswith("skipped")
        ]
        display_attempts = meaningful or attempts
        last = display_attempts[-1]
        skipped_count = len(attempts) - len(meaningful)
        outcome = str(last.get("outcome") or "?")
        attempt_count = len(display_attempts)
        title = _clip(last.get("title") or "", 44)
        title_bit = f" {_muted(title)}" if title and title != name else ""
        skipped_bit = (
            f" {_muted(f'+{skipped_count} skipped/resumed')}"
            if skipped_count else ""
        )
        lines.append(
            f"  {_phase(f'{name:<22}')} attempts={attempt_count:<2} "
            f"{_state(outcome)}{skipped_bit}{title_bit}"
        )
    remaining = len(grouped) - len(rows)
    if remaining > 0:
        lines.append(f"  {_muted(f'... {remaining} more phases; rerun with --debug')}")
    return lines


def _gate_lines(gates: list[Any], *, body: dict[str, Any], debug: bool) -> list[str]:
    lines = ["", _section("Quality gates")]
    dict_gates = [g for g in gates if isinstance(g, dict)]
    if not dict_gates:
        return lines + [f"  {_muted('No gate events recorded.')}"]
    counts = _counts(str(g.get("outcome") or "unknown") for g in dict_gates)
    lines.append("  " + " · ".join(
        f"{_state(outcome)} x{count}" for outcome, count in counts.items()
    ))
    attention = [
        g for g in dict_gates
        if str(g.get("outcome") or "") in {"failed", "skipped", "in_progress"}
    ]
    rows = dict_gates if debug else attention[:6]
    if rows:
        for gate in rows:
            name = str(gate.get("name") or "?")
            outcome = str(gate.get("outcome") or "?")
            duration = _float(gate.get("duration_s"))
            lines.append(
                f"  {_plain(f'{name:<22}')} {_state(f'{outcome:<16}')} "
                f"{_muted(f'{duration:.2f}s')}"
            )
    planned = len((body.get("plan") or {}).get("commands_to_run") or [])
    commands = len(body.get("commands") or [])
    if commands == 0 and planned:
        lines.append(
            f"  {_warn('No command events recorded')} "
            f"{_muted(f'({planned} planned commands in the plan)')}"
        )
    return lines


def _command_lines(commands: list[Any], *, body: dict[str, Any], debug: bool) -> list[str]:
    lines = ["", _section("Commands")]
    dict_commands = [c for c in commands if isinstance(c, dict)]
    planned = len((body.get("plan") or {}).get("commands_to_run") or [])
    if not dict_commands:
        suffix = f"; {planned} planned" if planned else ""
        return lines + [f"  {_muted('Recorded: none' + suffix)}"]
    lines.append(f"  {_plain(f'Recorded: {len(dict_commands)}')}")
    rows = dict_commands if debug else dict_commands[:5]
    for command in rows:
        argv = _clip(command.get("argv_summary") or "?", 70)
        outcome = str(command.get("outcome") or "?")
        exit_code = command.get("exit_code", "?")
        lines.append(
            f"  {_state(f'{outcome:<12}')} exit={exit_code!s:<4} {_muted(argv)}"
        )
    if len(dict_commands) > len(rows):
        lines.append(f"  {_muted(f'... {len(dict_commands) - len(rows)} more commands')}")
    return lines


def _finding_lines(findings: list[Any], *, body: dict[str, Any], debug: bool) -> list[str]:
    dict_findings = [f for f in findings if isinstance(f, dict)]
    waivers = _waivers(body.get("errors") or [])
    lines = ["", _section("Findings")]
    if not dict_findings:
        return lines + [f"  {_good('No review findings recorded.')}"]

    active = _active_findings(dict_findings, waivers)
    summary = _finding_status_summary(dict_findings, waivers)
    lines.append(f"  {_warn(summary) if active else _good(summary)}")
    rows = sorted(
        dict_findings,
        key=lambda finding: finding_status_sort_key(_finding_with_fallback_status(finding, waivers)),
    )
    rows = rows if debug else rows[:8]
    for finding in rows:
        display = _finding_with_fallback_status(finding, waivers)
        status = _finding_status_label(str(display.get("status") or "open"))
        severity = str(finding.get("severity") or "P?")
        phase = str(finding.get("phase") or "?")
        attempt = finding.get("attempt") or "?"
        title = _clip(finding.get("title") or "(no title)", 68)
        lines.append(
            f"  {_state(f'{status:<8}')} {_state(f'{severity:<3}')} "
            f"{_muted(f'{phase}#{attempt:<3}')} {_plain(title)}"
        )
        reason = str(display.get("status_reason") or "").strip()
        if debug and reason:
            lines.append(f"             {_muted(reason)}")
    if len(dict_findings) > len(rows):
        lines.append(
            f"  {_muted(f'... {len(dict_findings) - len(rows)} more findings; rerun with --debug')}"
        )
    return lines


def _operator_decision_lines(errors: list[Any]) -> list[str]:
    waivers = _waivers(errors)
    if not waivers:
        return []
    lines = ["", _section("Operator decisions")]
    for waiver in waivers:
        phase = waiver.get("phase") or "?"
        message = _clip(waiver.get("message") or waiver.get("waiver_text") or "", 96)
        findings = waiver.get("findings")
        count = len(findings) if isinstance(findings, list) else 0
        lines.append(
            f"  {_warn('waiver')} phase={_phase(str(phase))} "
            f"findings={count} {_muted(message)}"
        )
    return lines


def _metric_lines(metrics: dict[str, Any]) -> list[str]:
    lines = ["", _section("Metrics")]
    tokens = _int(metrics.get("total_tokens"))
    tokens_in = _int(metrics.get("total_tokens_in"))
    tokens_out = _int(metrics.get("total_tokens_out"))
    duration = _float(metrics.get("total_duration_s"))
    lines.append(
        f"  {_plain(f'{tokens:,} tok')} "
        f"{_muted(f'(in={tokens_in:,} out={tokens_out:,})')} "
        f"{_plain(f'{duration:.1f}s')}"
    )
    extras = []
    if "total_rounds" in metrics:
        extras.append(f"rounds={_int(metrics.get('total_rounds'))}")
    if "total_retries" in metrics:
        extras.append(f"retries={_int(metrics.get('total_retries'))}")
    if extras:
        lines.append(f"  {_muted(' · '.join(extras))}")
    return lines


def _artifact_lines(artifacts: list[Any], *, debug: bool) -> list[str]:
    dict_artifacts = [a for a in artifacts if isinstance(a, dict)]
    lines = ["", _section("Artifacts")]
    if not dict_artifacts:
        return lines + [f"  {_muted('No artifacts recorded.')}"]
    counts = _counts(str(a.get("kind") or "unknown") for a in dict_artifacts)
    lines.append("  " + _muted(" · ".join(f"{kind} x{count}" for kind, count in counts.items())))
    rows = dict_artifacts if debug else [
        a for a in dict_artifacts
        if str(a.get("kind") or "") in {"diff", "evidence", "evidence_md", "parsed_plan"}
    ][:6]
    for artifact in rows:
        kind = str(artifact.get("kind") or "?")
        path = _clip(artifact.get("path") or "?", 98)
        apply_check = artifact.get("apply_check")
        suffix = ""
        if isinstance(apply_check, dict):
            suffix = f" {_state(str(apply_check.get('status') or '?'))}"
        lines.append(f"  {_plain(f'{kind:<12}')} {_muted(path)}{suffix}")
    return lines


def _error_lines(errors: list[Any], *, status: str, debug: bool) -> list[str]:
    dict_errors = [e for e in errors if isinstance(e, dict)]
    visible = [
        e for e in dict_errors
        if e.get("kind") not in {"phase_handoff_waiver"}
        and (debug or e.get("kind") != "phase_handoff_requested" or status == "awaiting_phase_handoff")
    ]
    if not visible:
        return []
    lines = ["", _section("Diagnostics")]
    rows = visible if debug else visible[:6]
    for error in rows:
        kind = str(error.get("kind") or "error")
        message = _clip(error.get("message") or "", 100)
        lines.append(f"  {_state(f'{kind:<22}')} {_muted(message)}")
    if len(visible) > len(rows):
        lines.append(f"  {_muted(f'... {len(visible) - len(rows)} more diagnostics')}")
    return lines


def _active_findings(findings: Iterable[dict[str, Any]], waivers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        finding for finding in findings
        if str(_finding_with_fallback_status(finding, waivers).get("status") or "open")
        in ACTIVE_FINDING_STATUSES
    ]


def _finding_status_summary(
    findings: list[dict[str, Any]],
    waivers: list[dict[str, Any]],
) -> str:
    counts: OrderedDict[str, list[dict[str, Any]]] = OrderedDict(
        (status, []) for status in FINDING_STATUS_ORDER
    )
    for finding in findings:
        display = _finding_with_fallback_status(finding, waivers)
        status = str(display.get("status") or "open")
        counts.setdefault(status, []).append(display)
    bits = []
    for status, entries in counts.items():
        if not entries:
            continue
        severity = _severity_summary(entries)
        suffix = f" ({severity})" if severity else ""
        bits.append(f"{_finding_status_summary_label(status)} x{len(entries)}{suffix}")
    open_count = sum(
        len(counts.get(status, []))
        for status in ACTIVE_FINDING_STATUSES
    )
    prefix = f"active x{open_count}"
    return f"{prefix} · {' · '.join(bits)}" if bits else prefix


def _severity_summary(findings: Iterable[dict[str, Any]]) -> str:
    counts = _counts(str(f.get("severity") or "P?") for f in findings)
    return " ".join(f"{severity}x{count}" for severity, count in counts.items())


def _finding_with_fallback_status(
    finding: dict[str, Any],
    waivers: list[dict[str, Any]],
) -> dict[str, Any]:
    if finding.get("status"):
        return finding
    display = dict(finding)
    display["status"] = "waived" if _is_waived_finding(finding, waivers) else "open"
    return display


def _is_waived_finding(
    finding: dict[str, Any],
    waivers: list[dict[str, Any]],
) -> bool:
    waived_ids: set[str] = set()
    for waiver in waivers:
        waiver_findings = waiver.get("findings")
        if not isinstance(waiver_findings, list):
            continue
        for finding in waiver_findings:
            if isinstance(finding, dict) and finding.get("id"):
                waived_ids.add(str(finding["id"]))
    finding_id = str(finding.get("id") or "")
    return bool(finding_id and finding_id in waived_ids)


def _finding_status_label(status: str) -> str:
    return {
        "final_rejected": "REJECTED",
        "open": "OPEN",
        "waived": "WAIVED",
        "fixed": "FIXED",
        "accepted": "ACCEPTED",
    }.get(status, status.upper())


def _finding_status_summary_label(status: str) -> str:
    return {
        "final_rejected": "final-rejected",
        "open": "open",
        "waived": "waived",
        "fixed": "fixed",
        "accepted": "accepted",
    }.get(status, status.replace("_", "-"))


def _waivers(errors: Any) -> list[dict[str, Any]]:
    if not isinstance(errors, list):
        return []
    return [
        e for e in errors
        if isinstance(e, dict) and e.get("kind") == "phase_handoff_waiver"
    ]


def _counts(values: Iterable[str]) -> OrderedDict[str, int]:
    counts: OrderedDict[str, int] = OrderedDict()
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _clip(value: object, limit: int) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _paint(text: str, *codes: str) -> str:
    return paint(text, *codes, color=None, stream=sys.stdout)


def _label(text: str) -> str:
    return _paint(text, C.CYAN)


def _section(text: str) -> str:
    return _paint(f"  {text}:", C.CYAN, C.BOLD)


def _plain(text: object) -> str:
    return _paint(str(text), C.WHITE)


def _muted(text: object) -> str:
    return _paint(str(text), C.GREY)


def _good(text: object) -> str:
    return _paint(str(text), C.GREEN)


def _warn(text: object) -> str:
    return _paint(str(text), C.YELLOW)


def _bad(text: object) -> str:
    return _paint(str(text), C.RED)


def _phase(text: str) -> str:
    return _paint(text, C.CYAN)


def _state(text: str) -> str:
    normalized = text.strip().lower()
    if normalized in {
        "accepted",
        "done",
        "fixed",
        "ok",
        "pass",
        "passed",
        "approved",
        "resolved",
        "success",
    }:
        return _good(text)
    if normalized in {"failed", "fail", "final", "halted", "open", "rejected", "error"}:
        return _bad(text)
    if (
        normalized.startswith("p0")
        or normalized.startswith("p1")
        or normalized in {"skipped", "in_progress", "waiver", "pending"}
        or "waiver" in normalized
    ):
        return _warn(text)
    if normalized.startswith("phase_handoff"):
        return _warn(text)
    return _plain(text)


__all__ = ["format_evidence_cli"]
