"""Minimal review/repair handoff protocol.

Review/repair loops need one durable semantic bridge that plain prose
transcripts cannot provide: after a repair, the next reviewer must know what
the repair claims changed and what the current subject is.  This module keeps
that bridge intentionally small and text-projectable so it can serve plan
validation, code review, and file-artifact review without coupling the
protocol to git, worktrees, or on-disk artifacts.
"""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ReceiptItem:
    """One repair response to a reviewer finding or operator decision."""

    finding_id: str
    summary: str
    refs: tuple[str, ...] = ()
    verification: str | None = None


@dataclass(frozen=True)
class RepairReceipt:
    """Structured repair response consumed by the next review pass."""

    source_phase: str
    source_round: int | None
    repair_phase: str
    repair_round: int | None
    fixed: tuple[ReceiptItem, ...] = ()
    partially_fixed: tuple[ReceiptItem, ...] = ()
    waived: tuple[ReceiptItem, ...] = ()
    still_open: tuple[ReceiptItem, ...] = ()
    notes: str | None = None


def repair_receipt_to_dict(receipt: RepairReceipt) -> dict[str, Any]:
    """Return a JSON-friendly mapping for state/session persistence."""
    return asdict(receipt)


def repair_receipt_from_dict(data: dict[str, Any]) -> RepairReceipt:
    """Hydrate a :class:`RepairReceipt` from ``repair_receipt_to_dict`` data."""

    def _items(name: str) -> tuple[ReceiptItem, ...]:
        raw = data.get(name) or ()
        return tuple(
            ReceiptItem(
                finding_id=str(item.get("finding_id", "")),
                summary=str(item.get("summary", "")),
                refs=tuple(str(ref) for ref in item.get("refs", ()) or ()),
                verification=(
                    str(item["verification"])
                    if item.get("verification") is not None else None
                ),
            )
            for item in raw
            if isinstance(item, dict)
        )

    return RepairReceipt(
        source_phase=str(data.get("source_phase", "")),
        source_round=_optional_int(data.get("source_round")),
        repair_phase=str(data.get("repair_phase", "")),
        repair_round=_optional_int(data.get("repair_round")),
        fixed=_items("fixed"),
        partially_fixed=_items("partially_fixed"),
        waived=_items("waived"),
        still_open=_items("still_open"),
        notes=str(data["notes"]) if data.get("notes") is not None else None,
    )


def build_repair_receipt(
    *,
    source_phase: str,
    repair_phase: str,
    critique: str,
    repair_output: str,
    source_round: int | None = None,
    repair_round: int | None = None,
    operator_feedback: str = "",
    changed_refs: tuple[str, ...] = (),
) -> RepairReceipt:
    """Build a conservative receipt when the repair agent returns prose.

    Until repair agents emit a dedicated JSON receipt, the safest structured
    claim is intentionally modest: the repair phase responded to the current
    critique and the next reviewer must verify that response against the fresh
    subject.  We preserve operator waivers separately so the next review does
    not relitigate an accepted finding as a blocker.
    """
    fixed = (
        ReceiptItem(
            finding_id="review-feedback",
            summary=_first_meaningful_line(
                repair_output,
                fallback=f"{repair_phase} produced an updated subject.",
            ),
            refs=changed_refs,
            verification=(
                "Verify against current_review_subject; repair output was "
                "free-form and is not machine proof."
            ),
        ),
    ) if (critique.strip() or repair_output.strip()) else ()
    waived = ()
    if operator_feedback.strip():
        waived = (
            ReceiptItem(
                finding_id="operator-feedback",
                summary=operator_feedback.strip(),
                verification="Operator feedback is authoritative for this retry.",
            ),
        )
    return RepairReceipt(
        source_phase=source_phase,
        source_round=source_round,
        repair_phase=repair_phase,
        repair_round=repair_round,
        fixed=fixed,
        waived=waived,
        notes=(
            "The next reviewer must verify this receipt against the current "
            "subject and must not repeat prior findings without fresh evidence."
        ),
    )


def render_repair_receipt(receipt: RepairReceipt | dict[str, Any] | None) -> str:
    """Render a compact model-facing repair receipt."""
    if receipt is None:
        return ""
    if isinstance(receipt, dict):
        receipt = repair_receipt_from_dict(receipt)
    lines = [
        "## Repair Receipt",
        "",
        f"Source phase: {receipt.source_phase}"
        + _round_suffix(receipt.source_round),
        f"Repair phase: {receipt.repair_phase}"
        + _round_suffix(receipt.repair_round),
        "",
        "Use this as the repairer's claim, not as proof. Verify it against "
        "the current review subject below.",
        "Do not repeat a prior finding unless the current subject still "
        "provides fresh evidence for it. Keep operator-waived items separate "
        "from blocking findings.",
    ]
    for title, items in (
        ("Fixed", receipt.fixed),
        ("Partially fixed", receipt.partially_fixed),
        ("Waived / operator accepted", receipt.waived),
        ("Still open", receipt.still_open),
    ):
        if not items:
            continue
        lines.extend(("", f"### {title}"))
        for item in items:
            lines.append(f"- {item.finding_id}: {item.summary}")
            if item.refs:
                lines.append(f"  Refs: {', '.join(item.refs)}")
            if item.verification:
                lines.append(f"  Verification: {item.verification}")
    if receipt.notes:
        lines.extend(("", f"Notes: {receipt.notes}"))
    return "\n".join(lines).strip()


def render_current_plan_subject(parsed_plan: Any) -> str:
    """Render a compact fresh-plan subject marker.

    ``validate_plan`` already sends the full typed plan as separate
    ``plan_contract`` and ``plan_tasks`` prompt parts.  The re-review
    subject part is still load-bearing (it tells the reviewer what the
    receipt must be checked against), but it should not duplicate the
    full plan body on every resumed delta round.
    """
    if parsed_plan is None:
        return ""
    from pipeline.plan_contract import render_plan_contract
    from pipeline.plan_markdown import render_validate_plan_tasks

    full_subject = "\n\n".join(
        part.strip()
        for part in (
            render_plan_contract(parsed_plan).strip(),
            render_validate_plan_tasks(parsed_plan).strip(),
        )
        if part and part.strip()
    )
    if not full_subject:
        return ""
    subject_hash = hashlib.sha256(full_subject.encode("utf-8")).hexdigest()[:16]
    return "\n".join(
        (
            "## Current Plan Subject",
            "",
            "Subject type: current typed plan.",
            "Full subject: the `plan_contract:typed_plan` and "
            "`plan_tasks:execution_plan` parts in this prompt.",
            f"Subject hash: sha256:{subject_hash}",
            "",
            "Verify the repair receipt against those selected plan parts. "
            "Do not repeat an old finding unless the current plan parts "
            "still provide fresh evidence for it.",
        )
    )


def render_current_change_subject(project_dir: str) -> str:
    """Render a small fresh code/file subject for post-repair review.

    Git data is a backend detail here, not the protocol.  Non-git projects
    degrade to a clear note so the reviewer still knows that it must inspect
    the current project state rather than trust old session memory.
    """
    path = Path(project_dir)
    status = _git(["status", "--short", "-uall"], path)
    stat = _git(["diff", "--stat"], path)
    lines = [
        "## Current Review Subject",
        "",
        f"Project directory: {project_dir}",
        "",
        "### git status --short -uall",
        status or "(clean or unavailable)",
        "",
        "### git diff --stat",
        stat or "(no tracked diff or unavailable)",
        "",
        "If this subject is insufficient, read the referenced files directly "
        "before repeating an old finding.",
    ]
    return "\n".join(lines).strip()


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _round_suffix(round_n: int | None) -> str:
    return "" if round_n is None else f" round {round_n}"


def _first_meaningful_line(text: str, *, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:400]
    return fallback


def _with_hash(body: str) -> str:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    return f"{body}\n\nSubject hash: sha256:{digest}"


def _git(args: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


__all__ = [
    "ReceiptItem",
    "RepairReceipt",
    "build_repair_receipt",
    "render_current_change_subject",
    "render_current_plan_subject",
    "render_repair_receipt",
    "repair_receipt_from_dict",
    "repair_receipt_to_dict",
]
