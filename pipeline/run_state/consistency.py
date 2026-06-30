"""Diagnostic consistency checker for the run-state layer (Stage 0 brain).

:func:`validate_run_state` compares the event-derived projection (the
"brain", from :func:`pipeline.run_state.projector.project_run_dir`) against
the durable "body" state on disk — ``meta.json`` and the
``phase_handoff_decisions/`` artifacts — and reports structured
:class:`RunStateIssue` rows. It is strictly READ-ONLY: it never writes,
never repairs, and never calls ``phase_handoff_decide``. ``repair_hint`` on
each issue describes what a future repair *would* do; Stage 0 does none of
it.

``meta.json`` is read here (locally, tolerantly) only as the comparison
target — the projection itself comes purely from the event stream, so the
checks are not tautological. The critical ``meta_handoff_without_event``
check keys on ``projected.seen_handoff_ids`` (the full history of
``phase.handoff_requested`` ids) rather than ``active_handoff_id`` so the
"handoff event happened, active pointer later cleared on halt" case does
NOT produce a false issue (requirement F2).

Severity convention:

- ``"error"`` — a contradiction that breaks resume / terminality
  reasoning (``halt_decision_without_halted_meta``,
  ``meta_handoff_without_event``).
- ``"warning"`` — a recoverable desync that a repair could heal without
  data loss (``interrupted_with_active_handoff``,
  ``terminal_with_stale_handoff``).
- ``"info"`` — an expected pending state, surfaced for visibility but not a
  fault (``active_handoff_without_decision``: the run is simply waiting for
  an operator decision).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.run_state.projector import project_run_dir
from pipeline.run_state.types import (
    RunStateIssue,
    RunStateSnapshot,
    RunStateValidationReport,
)

_DECISIONS_DIRNAME = "phase_handoff_decisions"
_TERMINAL_META_STATUSES = frozenset({"halted", "done"})


def validate_run_state(run_dir: Path | str) -> RunStateValidationReport:
    """Diagnose run-state inconsistencies for ``run_dir`` (read-only).

    Projects state from ``events.jsonl``, reads ``meta.json`` and the
    decision artifacts as the comparison target, and returns a
    :class:`RunStateValidationReport`. ``ok`` is True when no issue has
    severity ``"error"``. Missing ``meta.json`` or decisions directory
    degrades tolerantly (no exception).
    """
    path = Path(run_dir)
    snapshot = project_run_dir(path)

    meta = _read_meta(path)
    meta_status = meta.get("status") if isinstance(meta, dict) else None
    active_handoff = meta.get("phase_handoff") if isinstance(meta, dict) else None
    meta_handoff_id = (
        active_handoff.get("id") if isinstance(active_handoff, dict) else None
    )

    decisions = _read_decisions(path)

    issues = _diagnose(
        snapshot=snapshot,
        meta_status=meta_status,
        active_handoff=active_handoff,
        meta_handoff_id=meta_handoff_id,
        decisions=decisions,
    )

    ok = not any(issue.severity == "error" for issue in issues)
    return RunStateValidationReport(
        run_dir=path,
        projected=snapshot,
        meta_status=meta_status if isinstance(meta_status, str) else None,
        ok=ok,
        issues=tuple(issues),
    )


def _diagnose(
    *,
    snapshot: RunStateSnapshot,
    meta_status: Any,
    active_handoff: Any,
    meta_handoff_id: Any,
    decisions: list[dict[str, Any]],
) -> list[RunStateIssue]:
    """Build the issue list for the five inconsistency classes."""
    issues: list[RunStateIssue] = []
    has_active_handoff = isinstance(active_handoff, dict)
    decided_ids = {
        d["handoff_id"]
        for d in decisions
        if isinstance(d.get("handoff_id"), str)
    }
    halt_decided_ids = {
        d["handoff_id"]
        for d in decisions
        if d.get("action") == "halt" and isinstance(d.get("handoff_id"), str)
    }

    # 1. interrupted run that still carries an active handoff pointer.
    if (
        has_active_handoff
        and (meta_status == "interrupted" or snapshot.status.value == "interrupted")
    ):
        issues.append(
            RunStateIssue(
                code="interrupted_with_active_handoff",
                severity="warning",
                message=(
                    "run is interrupted but meta.phase_handoff still carries "
                    f"an active handoff ({meta_handoff_id!r})"
                ),
                repair_hint=(
                    "a torn handoff is decidable: resolve it via the handoff "
                    "decide API (halt/continue) or clear meta.phase_handoff "
                    "once the decision is recorded"
                ),
            )
        )

    # 2. terminal run with a stale active handoff pointer.
    if has_active_handoff and meta_status in _TERMINAL_META_STATUSES:
        issues.append(
            RunStateIssue(
                code="terminal_with_stale_handoff",
                severity="warning",
                message=(
                    f"meta.status is {meta_status!r} (terminal) but "
                    f"meta.phase_handoff is still present ({meta_handoff_id!r})"
                ),
                repair_hint=(
                    "clear the stale meta.phase_handoff payload; a terminal "
                    "run has no active handoff to decide"
                ),
            )
        )

    # 3. active handoff present but no decision artifact for its id.
    if (
        has_active_handoff
        and isinstance(meta_handoff_id, str)
        and meta_handoff_id
        and meta_handoff_id not in decided_ids
    ):
        issues.append(
            RunStateIssue(
                code="active_handoff_without_decision",
                severity="info",
                message=(
                    f"active handoff {meta_handoff_id!r} has no decision "
                    "artifact under phase_handoff_decisions/"
                ),
                repair_hint=(
                    "expected while the run waits for an operator; record a "
                    "decision via the handoff decide API before resuming"
                ),
            )
        )

    # 4. a halt decision exists but meta.status is not halted (torn write).
    if halt_decided_ids and meta_status != "halted":
        issues.append(
            RunStateIssue(
                code="halt_decision_without_halted_meta",
                severity="error",
                message=(
                    "a halt decision artifact exists "
                    f"({sorted(halt_decided_ids)!r}) but meta.status is "
                    f"{meta_status!r}, not 'halted'"
                ),
                repair_hint=(
                    "heal the torn meta state: flip meta.status to 'halted' "
                    "and clear meta.phase_handoff so the run is terminal"
                ),
            )
        )

    # 5. meta claims an active handoff whose id never appeared as a
    #    phase.handoff_requested event. Keyed on seen_handoff_ids (full
    #    history) — NOT active_handoff_id — so a halt that cleared the
    #    active pointer does not false-fire (F2).
    if (
        isinstance(meta_handoff_id, str)
        and meta_handoff_id
        and meta_handoff_id not in snapshot.seen_handoff_ids
    ):
        issues.append(
            RunStateIssue(
                code="meta_handoff_without_event",
                severity="error",
                message=(
                    f"meta.phase_handoff id {meta_handoff_id!r} has no "
                    "corresponding phase.handoff_requested event in "
                    "events.jsonl"
                ),
                repair_hint=(
                    "the event stream is the source of truth; either the "
                    "event was lost or meta.phase_handoff is stale — "
                    "reconcile by re-emitting the event or clearing meta"
                ),
            )
        )

    return issues


def _read_meta(run_dir: Path) -> dict[str, Any]:
    """Read ``meta.json`` tolerantly; return ``{}`` when absent/malformed."""
    meta_file = run_dir / "meta.json"
    if not meta_file.is_file():
        return {}
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_decisions(run_dir: Path) -> list[dict[str, Any]]:
    """Read decision artifacts tolerantly, taking ``action`` + ``handoff_id``.

    Returns one ``{"action", "handoff_id"}`` dict per readable artifact.
    A missing directory or an unreadable / malformed file is skipped — a
    single bad artifact never breaks the scan.
    """
    decisions_dir = run_dir / _DECISIONS_DIRNAME
    if not decisions_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(decisions_dir.iterdir()):
        if not (entry.is_file() and entry.suffix == ".json"):
            continue
        try:
            raw = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        out.append(
            {
                "action": raw.get("action"),
                "handoff_id": raw.get("handoff_id"),
            }
        )
    return out


__all__ = ["validate_run_state"]
