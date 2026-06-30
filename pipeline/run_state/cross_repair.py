"""Safe, opt-in repair for the one self-evident torn cross-run shape (Stage 3c).

Mirror of the :mod:`pipeline.run_state.cross` ↔ :mod:`pipeline.run_state.repair`
pairing applied to cross runs. :func:`repair_cross_run_state` consumes the
read-only cross diagnosis from
:func:`pipeline.run_state.cross.validate_cross_run_state` and, for the single
self-healable cross inconsistency, proposes (and optionally applies) the minimal
``meta.json`` mutation that brings the durable body back in line with the
event-derived state.

Only one cross code is repaired:

- ``cross_terminal_with_stale_handoff`` — ``meta.status`` is a cross terminal
  (``done`` / ``failed`` / ``halted`` / ``cancelled``) but ``meta.phase_handoff``
  still carries a stale active payload. A cross pause short-circuits the run to a
  final terminal before finalization, so any handoff left at a cross terminal is
  unambiguously stale. The repair clears it (``after=None``), exactly matching
  the shape of the single-project ``terminal_with_stale_handoff`` repair in
  :mod:`pipeline.run_state.repair`.

  Safety guard: this clear is applied ONLY when
  ``cross_checkpoint.phase_handoff_pending`` is NOT set. When the checkpoint
  still flags the handoff pending, clearing ``meta.phase_handoff`` alone (the
  repair never mutates ``cross_checkpoint.json``) would leave the run in a
  *more* torn shape — ``checkpoint_pending_without_active_handoff`` (an
  ``error``) where there was only a repairable ``warning``. That case stays
  diagnostic-only with ``needs_operator_decision=True``.

Every other cross code stays strictly diagnostic — ``applied=False``,
``changes=()`` — because healing it would require an operator decision or a
mutation of ``cross_checkpoint.json``, neither of which a safe repair performs:

- ``checkpoint_pending_without_active_handoff`` (severity ``error``) — sets
  ``needs_operator_decision=True``;
- ``active_handoff_without_checkpoint_pending``,
  ``checkpoint_kind_id_mismatch``, ``project_handoff_marker_incomplete``,
  ``cfa_pending_without_paused_state``, ``pending_gate_and_handoff_active`` —
  diagnostic only.

Critical invariants:

- The repair mutates **only** ``meta.json`` and **never** writes
  ``cross_checkpoint.json``. It reuses the crash-safe machinery from
  :mod:`pipeline.run_state.repair` (backup → atomic ``meta.json`` replace →
  audit artifact under ``run_state_repairs/``), so a second ``apply`` re-runs
  the diagnosis, finds nothing repairable, and writes nothing (idempotent).
- Leaf discipline: this module imports only its sibling ``run_state`` modules
  and never ``pipeline.cross_project``. ``validate_cross_run_state`` reads
  ``cross_checkpoint.json`` by path, tolerantly.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipeline.run_state.cross import (
    classify_cross_run_state,
    validate_cross_run_state,
)
from pipeline.run_state.repair import (
    RunStateRepairAction,
    RunStateRepairChange,
    RunStateRepairReport,
    _apply,
    _normalize_action,
    _read_meta,
)
from pipeline.run_state.types import RunStateIssue

_CROSS_TERMINAL_STALE_CODE = "cross_terminal_with_stale_handoff"
_CHECKPOINT_PENDING_NO_ACTIVE = "checkpoint_pending_without_active_handoff"
_PHASE_HANDOFF_FIELD = "phase_handoff"


def repair_cross_run_state(
    run_dir: Path | str,
    action: str | RunStateRepairAction = "safe",
    *,
    apply: bool = False,
) -> RunStateRepairReport:
    """Diagnose and (optionally) repair the one self-evident torn cross shape.

    Runs :func:`validate_cross_run_state` for diagnosis, maps the single
    self-healable code (``cross_terminal_with_stale_handoff``) to a minimal
    ``meta.json`` mutation (clear the stale ``meta.phase_handoff``), and either
    reports the proposed change (``apply=False``, the default — nothing is
    written) or applies it crash-safely (``apply=True``). Every other cross code
    stays diagnostic: ``changes=()``, ``applied=False``.

    ``applied`` is ``True`` only when ``apply=True`` and a non-empty change set
    was atomically written; a no-op ``apply`` (nothing repairable, ambiguous
    code, or clean run) writes nothing and returns ``applied=False``. All
    repairs are idempotent: a second ``apply`` finds nothing repairable.

    The repair touches **only** ``meta.json`` and never writes
    ``cross_checkpoint.json``.

    Raises ``ValueError`` for an unsupported ``action`` and ``RuntimeError`` if
    the atomic ``meta.json`` replace fails (the original ``meta.json`` is left
    intact and no audit artifact is written).
    """
    action_value = _normalize_action(action)
    path = Path(run_dir)
    now = datetime.now(UTC)
    repaired_at = now.isoformat()

    snap = classify_cross_run_state(path)
    issues = validate_cross_run_state(path)
    issue_codes = tuple(sorted({issue.code for issue in issues}))
    meta = _read_meta(path)

    changes, needs_decision, repair_hint = _plan_cross_changes(
        issues, meta, checkpoint_pending=snap.checkpoint_pending,
    )

    if not apply or not changes:
        return RunStateRepairReport(
            run_dir=path,
            action=action_value,
            applied=False,
            changes=tuple(changes),
            issue_codes=issue_codes,
            needs_operator_decision=needs_decision,
            repair_hint=repair_hint,
        )

    backup_path, audit_path = _apply(
        run_dir=path,
        meta=meta,
        changes=changes,
        action_value=action_value,
        issue_codes=issue_codes,
        needs_decision=needs_decision,
        now=now,
        repaired_at=repaired_at,
    )
    return RunStateRepairReport(
        run_dir=path,
        action=action_value,
        applied=True,
        changes=tuple(changes),
        issue_codes=issue_codes,
        needs_operator_decision=needs_decision,
        backup_path=backup_path,
        audit_path=audit_path,
        repaired_at=repaired_at,
        repair_hint=repair_hint,
    )


def _plan_cross_changes(
    issues: tuple[RunStateIssue, ...],
    meta: dict[str, Any],
    *,
    checkpoint_pending: bool,
) -> tuple[list[RunStateRepairChange], bool, str | None]:
    """Map cross codes to a safe change set, a refusal flag, and a hint.

    The only repairable code is ``cross_terminal_with_stale_handoff`` — clear
    the stale ``meta.phase_handoff`` (``after=None``), exactly mirroring the
    single-project ``terminal_with_stale_handoff`` repair.

    Safety guard: the clear is applied ONLY when
    ``cross_checkpoint.phase_handoff_pending`` is NOT set. When the checkpoint
    still flags the handoff pending, removing ``meta.phase_handoff`` alone (the
    repair never mutates ``cross_checkpoint.json``) would leave the run in a
    *more* torn shape — ``checkpoint_pending_without_active_handoff`` (an
    ``error``) where there was only a repairable ``warning``. That requires an
    operator decision or a ``cross_checkpoint`` mutation, so it stays
    diagnostic-only with ``needs_operator_decision=True``.

    Every other code is diagnostic: no change, with a hint explaining why it
    needs an operator decision or a checkpoint mutation.
    ``checkpoint_pending_without_active_handoff`` additionally flags
    ``needs_operator_decision``.
    """
    codes = {issue.code for issue in issues}

    if _CROSS_TERMINAL_STALE_CODE in codes and _PHASE_HANDOFF_FIELD in meta:
        if checkpoint_pending:
            return [], True, _terminal_pending_conflict_hint()
        return (
            [
                RunStateRepairChange(
                    _PHASE_HANDOFF_FIELD,
                    meta.get(_PHASE_HANDOFF_FIELD),
                    None,
                    _CROSS_TERMINAL_STALE_CODE,
                )
            ],
            False,
            None,
        )

    needs_decision = _CHECKPOINT_PENDING_NO_ACTIVE in codes
    return [], needs_decision, _diagnostic_hint(issues)


def _terminal_pending_conflict_hint() -> str:
    """Hint for a stale terminal handoff whose checkpoint is still pending.

    Clearing ``meta.phase_handoff`` alone would surface
    ``checkpoint_pending_without_active_handoff``; the safe repair refuses and
    defers to an operator decision / ``cross_checkpoint`` mutation.
    """
    return (
        "terminal cross run carries a stale meta.phase_handoff but "
        "cross_checkpoint.phase_handoff_pending is still set; clearing "
        "meta.phase_handoff alone would leave "
        "checkpoint_pending_without_active_handoff (an error). This needs an "
        "operator decision or a cross_checkpoint mutation and is not "
        "auto-repaired (the safe cross repair never mutates cross_checkpoint)"
    )


def _diagnostic_hint(issues: tuple[RunStateIssue, ...]) -> str | None:
    """Compose a hint for diagnostic-only cross codes (``None`` when clean).

    Names the diagnostic codes and states they are not auto-repaired because
    each requires an operator decision or a ``cross_checkpoint.json`` mutation —
    the safe cross repair only clears a stale terminal ``meta.phase_handoff``.
    """
    diagnostic_codes = sorted(
        {issue.code for issue in issues if issue.code != _CROSS_TERMINAL_STALE_CODE}
    )
    if not diagnostic_codes:
        return None
    return (
        "cross diagnostic only: "
        + ", ".join(diagnostic_codes)
        + "; this discrepancy requires an operator decision or a "
        "cross_checkpoint mutation and is not auto-repaired (the safe cross "
        "repair only clears a stale terminal meta.phase_handoff)"
    )


__all__ = ["repair_cross_run_state"]
