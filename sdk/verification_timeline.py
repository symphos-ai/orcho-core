# SPDX-License-Identifier: Apache-2.0
"""Artifact-only public projection of the scheduled-gate ledger."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sdk.runs import _CWD_DEFAULT, find_run, load_meta

Disposition = Literal[
    "not_selected", "manual_available", "suggested", "skipped_fresh",
    "executed_pass", "executed_fail", "residual_missing", "residual_stale",
    "residual_failed",
]


@dataclass(frozen=True, slots=True)
class ReceiptEvidence:
    """Receipt evidence recorded by the durable identity trail, if any."""

    classification: str = ""
    path: str = ""
    source: str = ""
    inherited: bool = False
    reason: str = ""
    rerun: bool = False


@dataclass(frozen=True, slots=True)
class ScheduledGateRow:
    command: str
    hook: str
    phase: str
    declared: bool
    selectable: bool
    selected: bool | None
    execution_policy: str
    consequence: str
    disposition: Disposition | None
    selection_reason: str | None
    executor: str | None
    trigger: str | None
    receipt_evidence: ReceiptEvidence | None = None


@dataclass(frozen=True, slots=True)
class ScheduledGateEvent:
    command: str
    hook: str
    phase: str
    kind: str
    outcome: str
    reason: str
    receipt_evidence: ReceiptEvidence | None = None


@dataclass(frozen=True, slots=True)
class VerificationTimelineProjection:
    schema_version: str
    run_id: str
    project: str = ""
    finalized: bool = False
    rows: tuple[ScheduledGateRow, ...] = ()
    events: tuple[ScheduledGateEvent, ...] = ()


def get_verification_timeline(
    *, run_id: str | None = None, project: str | None = None,
    workspace: str | None = None, cwd: Path | str | None | object = _CWD_DEFAULT,
) -> VerificationTimelineProjection:
    """Read only the run ledger; plugin and receipt reconstruction are forbidden."""
    ref = find_run(run_id, workspace=workspace, cwd=cwd)
    meta = load_meta(ref.run_dir)
    from pipeline.verification_ledger_store import ledger_path, load_ledger

    if not ledger_path(ref.run_dir).exists():
        return VerificationTimelineProjection(
            schema_version="1", run_id=ref.run_id,
            project=str(meta.get("project") or project or ""),
        )
    ledger = load_ledger(ref.run_dir)
    rows = tuple(
        ScheduledGateRow(
            command=row.gate, hook=row.hook, phase=row.phase,
            declared=row.declared, selectable=row.selectable, selected=row.selected,
            execution_policy=row.execution_policy, consequence=row.consequence,
            disposition=row.disposition, selection_reason=row.selection_reason,
            executor=row.executor, trigger=row.trigger,
            receipt_evidence=_receipt(row.receipt_evidence),
        )
        for row in ledger.rows
    )
    events = tuple(
        ScheduledGateEvent(
            command=event.command, hook=event.hook, phase=event.phase,
            kind=event.kind, outcome=event.outcome, reason=event.reason,
            receipt_evidence=_receipt(event.receipt_evidence),
        )
        for event in ledger.trail
    )
    return VerificationTimelineProjection(
        schema_version="1", run_id=ref.run_id,
        project=str(meta.get("project") or project or ""),
        finalized=ledger.finalized, rows=rows, events=events,
    )


def _receipt(path: str | None) -> ReceiptEvidence | None:
    return ReceiptEvidence(path=path) if path else None


__all__ = [
    "ReceiptEvidence", "ScheduledGateEvent", "ScheduledGateRow",
    "VerificationTimelineProjection", "get_verification_timeline",
]
