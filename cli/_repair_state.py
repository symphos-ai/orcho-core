"""Pure formatters / JSON projection for ``orcho repair-state``.

Both functions take the typed :class:`RunStateRepairReport` (plus a little
resolved CLI context) and return a value. They never call ``print``, never
write files, and never raise on the normal paths — the ``cmd_repair_state``
facade owns all I/O and error mapping. Output style mirrors
``cli/_formatters.py`` (2-space indent, a 60-char box rule).
"""
from __future__ import annotations

from typing import Any

from sdk import to_jsonable


def format_repair_report(
    report: Any,
    *,
    run_id: str,
    current_status: str | None,
    apply_requested: bool,
) -> str:
    """Render the human-readable repair diagnosis / outcome block.

    Always prints the current status (``'?'`` when absent), even when no
    status mutation is proposed. A refusal
    (``needs_operator_decision=True``) shows an operator-decision notice and
    never presents anything as applied.
    """
    out: list[str] = []
    sep = "─" * 60
    out.append("")
    out.append(sep)
    out.append(f"  Repair:  {run_id}")
    out.append(sep)
    out.append(f"  Status:  {current_status or '?'}")
    out.append(f"  Run dir: {report.run_dir}")
    out.append(f"  Action:  {report.action}")

    issue_codes = list(report.issue_codes)
    out.append(f"  Issues:  {', '.join(issue_codes) if issue_codes else '(none)'}")

    if report.needs_operator_decision:
        out.append("")
        out.append(
            "  This is not a self-healable torn state; an operator decision "
            "is required."
        )
        if report.repair_hint:
            out.append(f"  Hint:    {report.repair_hint}")
        out.append(sep)
        out.append("")
        return "\n".join(out)

    changes = list(report.changes)
    out.append("")
    if changes:
        out.append("  Proposed changes:")
        for change in changes:
            out.append(
                f"    {change.field}: {change.before!r} -> {change.after!r} "
                f"[{change.issue_code}]"
            )
    else:
        out.append("  Proposed changes: (none)")

    out.append("")
    if report.applied:
        out.append("  Applied: yes")
    elif apply_requested:
        out.append("  Applied: no (nothing to repair)")
    else:
        out.append("  Applied: no (dry-run)")

    if report.backup_path is not None:
        out.append(f"  Backup:  {report.backup_path}")
    if report.audit_path is not None:
        out.append(f"  Audit:   {report.audit_path}")
    if report.repair_hint:
        out.append(f"  Hint:    {report.repair_hint}")

    out.append(sep)
    out.append("")
    return "\n".join(out)


def repair_report_to_json(
    report: Any,
    *,
    run_id: str,
    apply_requested: bool,
) -> dict[str, Any]:
    """Project the report to a stable JSON dict for ``--json``.

    Base shape comes from :func:`to_jsonable` (run_dir, action, applied,
    changes, issue_codes, needs_operator_decision, backup_path, audit_path,
    repaired_at, repair_hint). ``run_id`` and ``apply_requested`` are layered
    on top. Every documented key is always present, with ``None`` rather than
    a missing key.
    """
    base = to_jsonable(report)
    payload: dict[str, Any] = {
        "run_id": run_id,
        "run_dir": base.get("run_dir"),
        "action": base.get("action"),
        "apply_requested": apply_requested,
        "applied": base.get("applied"),
        "issue_codes": base.get("issue_codes") or [],
        "changes": base.get("changes") or [],
        "needs_operator_decision": base.get("needs_operator_decision"),
        "repair_hint": base.get("repair_hint"),
        "backup_path": base.get("backup_path"),
        "audit_path": base.get("audit_path"),
        "repaired_at": base.get("repaired_at"),
    }
    return payload
