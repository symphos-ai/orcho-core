"""Full terminal sections for ``orcho evidence --view full``."""
from __future__ import annotations

import sys
from typing import Any

from core.io.ansi import C, paint


def full_plan_lines(plan: dict[str, Any], *, artifacts: list[Any]) -> list[str]:
    lines = ["", _section("Plan contract")]
    if not plan or plan.get("source") == "absent":
        return lines + [f"  {_muted('No structured plan recorded.')}"]

    goal = str(plan.get("goal") or "").strip()
    summary = str(plan.get("short_summary") or "").strip()
    planning_context = str(plan.get("planning_context") or "").strip()
    if goal:
        lines.append(f"  {_label('Goal:')} {_plain(goal)}")
    if summary:
        lines.append(f"  {_label('Summary:')} {_plain(summary)}")
    if planning_context:
        lines.append(f"  {_label('Planning context:')}")
        lines.extend(_indented_block(planning_context, indent="    "))

    subtasks = [s for s in plan.get("subtasks") or [] if isinstance(s, dict)]
    declared_count = _int(plan.get("subtask_count"))
    dag_edges = sum(len(s.get("depends_on") or []) for s in subtasks)
    if subtasks:
        dag = "yes" if dag_edges else "linear"
    elif declared_count:
        dag = "unknown"
    else:
        dag = "none"
    counts = [
        f"source={plan.get('source', '?')}",
        f"subtasks={declared_count}",
        f"dag={dag}",
        f"contract={'yes' if plan.get('has_contract') else 'no'}",
    ]
    lines.append(f"  {_muted(' · '.join(counts))}")

    lines.extend(_bullet_list_lines("Acceptance criteria", plan.get("acceptance_criteria")))
    lines.extend(_bullet_list_lines("Owned files", plan.get("owned_files")))
    lines.extend(_bullet_list_lines("Commands to run", plan.get("commands_to_run")))
    lines.extend(_bullet_list_lines("Risks", plan.get("risks")))
    lines.extend(_bullet_list_lines("Review focus", plan.get("review_focus")))

    if subtasks:
        lines.append("")
        lines.append(_section("Planned tasks"))
        for index, subtask in enumerate(subtasks, start=1):
            task_id = str(subtask.get("id") or f"task-{index}")
            goal = str(subtask.get("goal") or "").strip()
            lines.append(f"  {_plain(f'{index}. {task_id}')} {_plain(goal)}")
            for key, label in (
                ("depends_on", "depends_on"),
                ("files", "files"),
                ("owned_files", "owned_files"),
                ("done_criteria", "done"),
            ):
                values = [str(v) for v in subtask.get(key) or [] if str(v)]
                if values:
                    lines.append(f"     {_muted(f'{label}: ' + '; '.join(values))}")
            spec = str(subtask.get("spec") or "").strip()
            if spec:
                lines.append(f"     {_muted('spec:')}")
                lines.extend(_indented_block(spec, indent="       "))
    elif declared_count:
        paths = _artifact_paths(artifacts, kind="parsed_plan")
        lines.append("")
        lines.append(
            f"  {_warn('Subtask bodies were not captured in this evidence bundle.')}"
        )
        for path in paths[:3]:
            lines.append(f"  {_muted('parsed_plan:')} {_muted(path)}")
    return lines


def full_phase_lines(phases: list[Any]) -> list[str]:
    rows = [p for p in phases if isinstance(p, dict)]
    lines = ["", _section("Phase timeline")]
    if not rows:
        return lines + [f"  {_muted('No phases recorded.')}"]
    for index, phase in enumerate(rows, start=1):
        name = str(phase.get("name") or "?")
        attempt = phase.get("attempt") or "?"
        outcome = str(phase.get("outcome") or "?")
        title = str(phase.get("title") or "").strip()
        title_bit = f" {_muted(title)}" if title and title != name else ""
        lines.append(
            f"  {_muted(f'{index:>2}.')} {_phase(f'{name}#{attempt:<3}')} "
            f"{_state(f'{outcome:<12}')}{title_bit}"
        )
        started = str(phase.get("started_at") or "").strip()
        ended = str(phase.get("ended_at") or "").strip()
        if started or ended:
            start_value = started or "?"
            end_value = ended or "?"
            lines.append(f"      {_muted(f'start={start_value} end={end_value}')}")
    return lines


def implementation_receipt_lines(receipts: list[Any]) -> list[str]:
    rows = [r for r in receipts if isinstance(r, dict)]
    lines = ["", _section("Implementation receipts")]
    if not rows:
        return lines + [f"  {_muted('No per-subtask receipts recorded.')}"]
    for receipt in rows:
        subtask_id = str(receipt.get("subtask_id") or "?")
        state = str(receipt.get("state") or "?")
        runtime = str(receipt.get("runtime") or "").strip()
        model = str(receipt.get("model") or "").strip()
        agent = " / ".join(x for x in (runtime, model) if x)
        agent_bit = f" {_muted(agent)}" if agent else ""
        lines.append(f"  {_state(f'{state:<11}')} {_plain(subtask_id)}{agent_bit}")
        criteria = receipt.get("criteria_report")
        if isinstance(criteria, list) and criteria:
            met = sum(
                1
                for item in criteria
                if isinstance(item, dict) and item.get("met") is True
            )
            lines.append(f"      {_muted(f'done criteria met {met}/{len(criteria)}')}")
        error = str(receipt.get("error") or receipt.get("attestation_error") or "").strip()
        if error:
            lines.append(f"      {_warn(error)}")
    return lines


def release_detail_lines(rows: list[Any]) -> list[str]:
    releases = [r for r in rows if isinstance(r, dict)]
    lines = ["", _section("Acceptance")]
    if not releases:
        return lines + [f"  {_muted('No final acceptance summary recorded.')}"]
    for release in releases:
        verdict = str(release.get("verdict") or release.get("release_verdict") or "?")
        phase = str(release.get("phase") or "final_acceptance")
        attempt = release.get("attempt") or "?"
        summary = str(release.get("summary") or release.get("short_summary") or "").strip()
        lines.append(f"  {_state(f'{verdict:<10}')} {_phase(f'{phase}#{attempt}')}")
        if summary:
            lines.append(f"      {_plain(summary)}")
        blockers = release.get("release_blockers")
        if isinstance(blockers, list) and blockers:
            lines.append(f"      {_warn(f'release blockers: {len(blockers)}')}")
        gaps = release.get("verification_gaps")
        if isinstance(gaps, list) and gaps:
            lines.append(f"      {_warn(f'verification gaps: {len(gaps)}')}")
    return lines


def _bullet_list_lines(title: str, values: Any) -> list[str]:
    items = [str(v) for v in values or [] if str(v)]
    if not items:
        return []
    lines = ["", _section(title)]
    for item in items:
        first, *rest = item.splitlines() or [""]
        lines.append(f"  - {_plain(first)}")
        for line in rest:
            lines.append(f"    {_plain(line)}")
    return lines


def _indented_block(text: str, *, indent: str) -> list[str]:
    return [f"{indent}{_plain(line)}" for line in text.splitlines() or [""]]


def _artifact_paths(artifacts: list[Any], *, kind: str) -> list[str]:
    paths: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        if str(artifact.get("kind") or "") != kind:
            continue
        path = str(artifact.get("path") or "").strip()
        if path:
            paths.append(path)
    return paths


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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


__all__ = [
    "full_phase_lines",
    "full_plan_lines",
    "implementation_receipt_lines",
    "release_detail_lines",
]
