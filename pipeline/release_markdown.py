"""
pipeline/release_markdown.py — Deterministic markdown rendering for
parsed release-gate output.

The release gate parses model JSON into :class:`ParsedRelease`; Orcho
renders human-readable markdown from it for logs, session output,
evidence, and repair_changes context. One-way transform — never
parsed back; the JSON contract is the only machine ground truth.
"""
from __future__ import annotations

from pipeline.release_parser import ParsedRelease, ReleaseBlocker, VerificationGap


def render_release_markdown(
    release: ParsedRelease,
    *,
    title: str = "Release gate",
    language: str | None = None,
) -> str:
    """Render a :class:`ParsedRelease` as stable, human-readable markdown."""
    labels = _labels(language)
    lines: list[str] = [f"# {title}", ""]
    lines.append(f"**{labels['verdict']}:** {release.verdict}")
    lines.append("")
    ship_ready = labels["yes"] if release.ship_ready else labels["no"]
    lines.append(f"**{labels['ship_ready']}:** {ship_ready}")
    lines.append("")
    lines.append(f"**{labels['short_summary']}:** **{release.short_summary}**")

    if release.release_blockers:
        lines.append("")
        lines.append(f"## {labels['release_blockers']}")
        for blocker in release.release_blockers:
            lines.append("")
            lines.extend(_render_blocker(blocker, labels=labels))

    if release.verification_gaps:
        lines.append("")
        lines.append(f"## {labels['verification_gaps']}")
        for gap in release.verification_gaps:
            lines.append("")
            lines.extend(_render_gap(gap, labels=labels))

    lines.append("")
    lines.append(f"## {labels['contract_status']}")
    lines.append("")
    cs = release.contract_status
    lines.append(f"- {labels['task_contract']}: {cs.task_contract}")
    lines.append(f"- {labels['interfaces']}:    {cs.interfaces}")
    lines.append(f"- {labels['persistence']}:   {cs.persistence}")
    lines.append(f"- {labels['tests']}:         {cs.tests}")

    return "\n".join(lines).rstrip() + "\n"


def _render_blocker(blocker: ReleaseBlocker, *, labels: dict[str, str]) -> list[str]:
    out: list[str] = [
        f"### {blocker.id} [{blocker.severity}] {blocker.title}",
    ]
    if blocker.file:
        location = blocker.file
        if blocker.line is not None:
            location = f"{blocker.file}:{blocker.line}"
        out.append("")
        out.append(f"{labels['file']}: `{location}`")
    out.append("")
    out.append(blocker.body)
    out.append("")
    out.append(f"**{labels['required_fix']}:** {blocker.required_fix}")
    out.append("")
    out.append(f"**{labels['why_blocks_release']}:** {blocker.why_blocks_release}")
    return out


def _render_gap(gap: VerificationGap, *, labels: dict[str, str]) -> list[str]:
    return [
        f"- **{labels['risk']}:** {gap.risk}",
        f"  **{labels['missing_evidence']}:** {gap.missing_evidence}",
        f"  **{labels['required_check']}:** {gap.required_check}",
    ]


def _is_russian(language: str | None) -> bool:
    normalized = (language or "").strip().lower()
    return normalized.startswith(("ru", "rus", "russian", "рус"))


def _labels(language: str | None) -> dict[str, str]:
    if _is_russian(language):
        return {
            "verdict": "Вердикт",
            "ship_ready": "Готово к релизу",
            "yes": "да",
            "no": "нет",
            "short_summary": "Кратко",
            "release_blockers": "Блокеры релиза",
            "verification_gaps": "Пробелы в проверке",
            "contract_status": "Статус контракта",
            "task_contract": "Контракт задачи",
            "interfaces": "Интерфейсы",
            "persistence": "Хранение",
            "tests": "Тесты",
            "file": "Файл",
            "required_fix": "Что исправить",
            "why_blocks_release": "Почему это блокирует релиз",
            "risk": "Риск",
            "missing_evidence": "Недостающее подтверждение",
            "required_check": "Нужная проверка",
        }
    return {
        "verdict": "Verdict",
        "ship_ready": "Ship-ready",
        "yes": "yes",
        "no": "no",
        "short_summary": "Short summary",
        "release_blockers": "Release blockers",
        "verification_gaps": "Verification gaps",
        "contract_status": "Contract status",
        "task_contract": "Task contract",
        "interfaces": "Interfaces",
        "persistence": "Persistence",
        "tests": "Tests",
        "file": "File",
        "required_fix": "Required fix",
        "why_blocks_release": "Why this blocks release",
        "risk": "Risk",
        "missing_evidence": "Missing evidence",
        "required_check": "Required check",
    }
