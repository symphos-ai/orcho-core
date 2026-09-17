"""pipeline/project/gate_events.py — the paired ``gate.start`` / ``gate.end`` boundary.

Every engine-owned verification gate execution is bracketed by a durable
``gate.start`` / ``gate.end`` pair on ``events.jsonl`` (ADR 0095), optionally
carrying the ``invocation_id`` that pairs the boundary with the live
``gate.progress`` stream of the same execution (ADR 0190). Two producers run
such gates: the scheduled-gate hooks (``pipeline.project.gate_repair``) and
the required-receipt auto-run before a final phase
(``pipeline.project.verification_autorun``). Both emit through this one
module so the payload shape has a single owner and readers (evidence
collector, MCP live status, the DONE timeline) never see a gate that runs
without a boundary.
"""
from __future__ import annotations

from typing import Any

__all__ = ["emit_gate_end", "emit_gate_start"]


def emit_gate_start(
    command: str,
    *,
    hook: str,
    phase: str,
    project_alias: str | None = None,
    invocation_id: str | None = None,
) -> None:
    """Persist the engine-owned gate boundary before its blocking command.

    The optional ``invocation_id`` pairs this boundary with the ``gate.progress``
    stream of the same execution (ADR 0190); it is omitted (readers tolerant)
    when unknown.
    """
    from core.observability.events import emit

    emit(
        "gate.start",
        name=command,
        gate_kind="scheduled",
        command=command,
        hook=hook,
        phase=phase,
        ownership="engine",
        **({"project_alias": project_alias} if project_alias else {}),
        **({"invocation_id": invocation_id} if invocation_id else {}),
    )


def emit_gate_end(
    command: str,
    *,
    hook: str,
    phase: str,
    outcome: str,
    duration_s: float,
    project_alias: str | None = None,
    classification: Any = None,
    invocation_id: str | None = None,
) -> None:
    """Close the typed gate boundary after the command returns or raises.

    ``outcome`` stays the historic pass/fail rollup. ``receipt_status`` /
    ``failure_kind`` carry the classification alongside it, so the durable
    stream distinguishes a command that failed from one that ran clean but
    could not be proven against the current checkout. Both are omitted when
    the boundary closes on a raise, where no classification exists.

    The optional ``invocation_id`` pairs this settled boundary with the
    ``gate.progress`` stream of the same execution (ADR 0190). ``gate.end``
    remains authoritative for the settled outcome; progress never overrides it.
    """
    from core.observability.events import emit

    status = str(getattr(classification, "status", "") or "")
    failure_kind = str(getattr(classification, "failure_kind", "") or "")
    emit(
        "gate.end",
        name=command,
        outcome=outcome,
        duration_s=duration_s,
        command=command,
        hook=hook,
        phase=phase,
        ownership="engine",
        **({"project_alias": project_alias} if project_alias else {}),
        **({"receipt_status": status} if status else {}),
        **({"failure_kind": failure_kind} if failure_kind else {}),
        **({"invocation_id": invocation_id} if invocation_id else {}),
    )
