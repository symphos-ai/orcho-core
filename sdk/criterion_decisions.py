"""Public SDK access to typed per-criterion human decisions (ADR 0188).

Narrow on purpose: record one decision, read the durable log. The SDK input
does **not** accept a caller-supplied ``decision_id`` or ``recorded_at`` — the
durable writer assigns both — and it always requires a concrete
``accept``/``reject``. Prompting an operator (elicitation, a CLI prompt, an MCP
form) is the caller's job; this boundary only records a made decision.

Returned records are the durable JSON shape: unused optional keys are absent,
never ``null``, and ``recorded_at`` is passed through as an opaque stable
string.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.criterion_decisions import (
    HumanDecisionError,
    load_human_decisions,
    record_human_decision,
)
from sdk.errors import CriterionDecisionRejected
from sdk.runs import _CWD_DEFAULT, find_run

__all__ = [
    "CriterionDecisionRejected",
    "list_criterion_decisions",
    "record_criterion_decision",
]


def record_criterion_decision(
    run_id: str | None = None,
    *,
    criterion_id: str,
    decision: str,
    note: str | None = None,
    actor: str | None = None,
    supersedes: str | None = None,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> dict[str, Any]:
    """Record one operator decision for a ``human`` criterion.

    Every admission rule is enforced once, at the durable writer: the run must
    be the one the decision names, the criterion must exist in that run's
    accepted plan, and its class must be ``human``. Unknown criterion,
    non-human criterion, wrong run, invalid payload, and invalid supersession
    all raise :class:`sdk.errors.CriterionDecisionRejected` and leave the
    durable artifact untouched.
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    try:
        record = record_human_decision(
            ref.run_dir,
            run_id=ref.run_id,
            criterion_id=criterion_id,
            decision=decision,
            note=note,
            actor=actor,
            supersedes=supersedes,
        )
    except HumanDecisionError as exc:
        raise CriterionDecisionRejected(str(exc)) from exc
    return record.to_dict()


def list_criterion_decisions(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> list[dict[str, Any]]:
    """The run's append-only decision log, in durable write order."""
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    try:
        return [r.to_dict() for r in load_human_decisions(ref.run_dir)]
    except HumanDecisionError as exc:
        raise CriterionDecisionRejected(str(exc)) from exc
