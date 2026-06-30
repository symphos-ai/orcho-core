"""Safe, opt-in repair for known torn run-state shapes (Stage 0 brain).

:func:`repair_run_state` consumes the read-only diagnosis produced by
:func:`pipeline.run_state.consistency.validate_run_state` and, for a strictly
limited set of self-healable inconsistencies, proposes (and optionally applies)
the minimal ``meta.json`` mutation that brings the durable body back in line
with the event-derived projection.

Design rules:

- ``validate_run_state`` is the single source of truth for problem codes; this
  module never re-derives them, it only maps known codes to a safe mutation.
- Dry-run is the default (``apply=False``): the report lists the proposed
  changes but nothing is written to disk.
- ``apply=True`` writes in a strict, crash-safe order: backup the original
  ``meta.json`` first, replace ``meta.json`` atomically (temp file in the same
  directory + ``os.replace`` after ``flush``/``fsync``), and only after the
  replace succeeds write the audit artifact under ``run_state_repairs/``.
- Repairs are idempotent by construction: a second ``apply`` re-runs the
  diagnosis, finds nothing repairable, and writes nothing.

Only three torn shapes are repaired in this stage:

- ``halt_decision_without_halted_meta`` — a halt decision artifact exists but
  ``meta.status`` never flipped. Heal to the exact post-halt shape the halt
  writer produces: ``status='halted'``, ``halt_reason='phase_handoff_halt'``,
  ``halted_at`` set from the halt decision's ``decided_at`` (falling back to
  the repair timestamp only when no artifact carries one), and the stale active
  ``phase_handoff`` cleared.
- ``terminal_with_stale_handoff`` (for ``status='halted'`` and ``'done'``) —
  clear the stale active ``phase_handoff`` payload.

``meta_handoff_without_event`` (a lost / desynced event stream) and an ordinary
pending ``active_handoff_without_decision`` are deliberately NOT repaired here.
An ``interrupted`` run that still carries an active handoff with no recorded
decision is refused (``needs_operator_decision``): the operator must decide the
handoff through the sanctioned decision API rather than have status flipped
automatically.

This package depends at most on its own modules and
:mod:`core.observability`; it never imports runtime / resume / finalization
paths and changes no on-disk schema beyond the new repair audit artifact.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pipeline.run_state.consistency import validate_run_state
from pipeline.run_state.status_vocab import INTERRUPTED_STATUS

_REPAIRS_DIRNAME = "run_state_repairs"
_META_FILENAME = "meta.json"

_HALT_CODE = "halt_decision_without_halted_meta"
_TERMINAL_STALE_CODE = "terminal_with_stale_handoff"
_INTERRUPTED_ACTIVE_CODE = "interrupted_with_active_handoff"
_NO_DECISION_CODE = "active_handoff_without_decision"

_HALTED_STATUS = "halted"
_HALT_REASON = "phase_handoff_halt"


class RunStateRepairAction(StrEnum):
    """Repair policy mode. ``SAFE`` is the only mode in this stage.

    Further modes are reserved for explicit future policy commands; passing an
    unsupported action raises ``ValueError``.
    """

    SAFE = "safe"


@dataclass(frozen=True, slots=True)
class RunStateRepairChange:
    """One field-level mutation of ``meta.json`` proposed by a repair.

    ``after`` is ``None`` when the field is removed (e.g. clearing a stale
    ``phase_handoff``). ``issue_code`` records which diagnosed problem drove
    the change.
    """

    field: str
    before: Any
    after: Any
    issue_code: str


@dataclass(frozen=True, slots=True)
class RunStateRepairReport:
    """Outcome of a :func:`repair_run_state` call.

    ``applied`` is ``True`` ONLY when the call ran with ``apply=True`` and
    actually wrote changes to ``meta.json`` (non-empty ``changes`` and a
    successful atomic replace). A dry-run, a no-op ``apply``, or a refusal all
    leave ``applied=False`` with ``backup_path``/``audit_path``/``repaired_at``
    as ``None``.

    ``backup_path`` and ``audit_path`` are absolute when set; the audit
    artifact records them relative to ``run_dir``.
    """

    run_dir: Path
    action: str
    applied: bool
    changes: tuple[RunStateRepairChange, ...] = field(default_factory=tuple)
    issue_codes: tuple[str, ...] = field(default_factory=tuple)
    needs_operator_decision: bool = False
    backup_path: Path | None = None
    audit_path: Path | None = None
    repaired_at: str | None = None
    repair_hint: str | None = None


def repair_run_state(
    run_dir: Path | str,
    action: str | RunStateRepairAction = "safe",
    *,
    apply: bool = False,
) -> RunStateRepairReport:
    """Diagnose and (optionally) repair known torn run-state shapes.

    Runs :func:`validate_run_state` for diagnosis, maps the known self-healable
    codes to a minimal ``meta.json`` mutation, and either reports the proposed
    changes (``apply=False``, the default — nothing is written) or applies them
    crash-safely (``apply=True``).

    ``applied`` is ``True`` only when ``apply=True`` and a non-empty change set
    was atomically written; a no-op ``apply`` (nothing to repair, or a refusal)
    writes nothing and returns ``applied=False``. All repairs are idempotent: a
    second ``apply`` finds nothing repairable and writes nothing.

    Raises ``ValueError`` for an unsupported ``action`` and ``RuntimeError`` if
    the atomic ``meta.json`` replace fails (the original ``meta.json`` is left
    intact and no audit artifact is written).
    """
    action_value = _normalize_action(action)
    path = Path(run_dir)
    now = datetime.now(UTC)
    repaired_at = now.isoformat()

    report = validate_run_state(path)
    issue_codes = tuple(sorted({issue.code for issue in report.issues}))
    meta = _read_meta(path)

    changes, needs_decision, repair_hint = _plan_changes(
        report, meta, path, fallback_halted_at=repaired_at
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


def _normalize_action(action: str | RunStateRepairAction) -> str:
    """Validate ``action`` and return its canonical string value."""
    try:
        return RunStateRepairAction(action).value
    except ValueError as e:
        raise ValueError(
            f"repair_run_state: action {action!r} is not supported; only "
            "'safe' is available (other modes are reserved for future "
            "explicit policy commands)"
        ) from e


def _plan_changes(
    report: Any,
    meta: dict[str, Any],
    run_dir: Path,
    *,
    fallback_halted_at: str,
) -> tuple[list[RunStateRepairChange], bool, str | None]:
    """Map diagnosed codes to a safe change set, a refusal flag, and a hint."""
    codes = {issue.code for issue in report.issues}
    status = meta.get("status")

    # Refusal: an interrupted run with an active handoff and no recorded
    # decision must not be flipped automatically. This is distinct from a torn
    # halt (interrupted + active + halt decision), where the halt-decision code
    # fires instead and active_handoff_without_decision does NOT.
    #
    # Layering decision (T3): this refusal is driven strictly by consistency
    # *diagnostic codes* (from validate_run_state), not by the SDK's
    # ``(status, active)`` decidable-handoff predicate. run_state must not import
    # sdk, and it does not need to: it only shares the torn-status constant,
    # imported from the run_state-local status_vocab. The decidable predicate
    # stays owned by sdk.phase_handoff; this module deliberately does not
    # re-derive it.
    if (
        status == INTERRUPTED_STATUS
        and _INTERRUPTED_ACTIVE_CODE in codes
        and _NO_DECISION_CODE in codes
    ):
        hint = (
            "interrupted run carries an active handoff with no recorded "
            "decision; decide it via the handoff decide API (halt/continue) "
            "before resuming, or run a later explicit policy command — this "
            "safe repair will not flip status automatically"
        )
        return [], True, hint

    changes: list[RunStateRepairChange] = []

    if _HALT_CODE in codes:
        if status != _HALTED_STATUS:
            changes.append(
                RunStateRepairChange("status", status, _HALTED_STATUS, _HALT_CODE)
            )
        # Match the full post-halt meta shape the SDK halt branch writes
        # (sdk.phase_handoff.phase_handoff_decide): status, halt_reason, and
        # halted_at = the halt decision's decided_at, with the stale active
        # handoff cleared. halted_at falls back to the repair timestamp only
        # when no halt decision artifact carries a usable decided_at.
        halted_at = _read_halt_decided_at(run_dir) or fallback_halted_at
        if meta.get("halt_reason") != _HALT_REASON:
            changes.append(
                RunStateRepairChange(
                    "halt_reason", meta.get("halt_reason"), _HALT_REASON, _HALT_CODE
                )
            )
        if meta.get("halted_at") != halted_at:
            changes.append(
                RunStateRepairChange(
                    "halted_at", meta.get("halted_at"), halted_at, _HALT_CODE
                )
            )
        if "phase_handoff" in meta:
            changes.append(
                RunStateRepairChange(
                    "phase_handoff", meta.get("phase_handoff"), None, _HALT_CODE
                )
            )
    elif _TERMINAL_STALE_CODE in codes and "phase_handoff" in meta:
        changes.append(
            RunStateRepairChange(
                "phase_handoff", meta.get("phase_handoff"), None, _TERMINAL_STALE_CODE
            )
        )

    return changes, False, None


def _apply(
    *,
    run_dir: Path,
    meta: dict[str, Any],
    changes: list[RunStateRepairChange],
    action_value: str,
    issue_codes: tuple[str, ...],
    needs_decision: bool,
    now: datetime,
    repaired_at: str,
) -> tuple[Path, Path]:
    """Write backup, atomically replace meta.json, then write the audit.

    Returns ``(backup_path, audit_path)``. Raises ``RuntimeError`` if the
    atomic replace fails (original meta.json left intact, no audit).
    """
    stamp = f"{now.strftime('%Y%m%dT%H%M%S_%f')}-{uuid.uuid4().hex[:8]}"

    repairs_dir = run_dir / _REPAIRS_DIRNAME
    repairs_dir.mkdir(exist_ok=True)

    backup_path = repairs_dir / f"meta.{stamp}.bak.json"
    _write_backup(run_dir, backup_path)

    new_meta = _apply_changes(meta, changes)
    _atomic_write_meta(run_dir, new_meta)

    audit_path = repairs_dir / f"{stamp}.json"
    audit_doc = {
        "repaired_at": repaired_at,
        "action": action_value,
        "issue_codes": list(issue_codes),
        "changes": [
            {
                "field": c.field,
                "before": c.before,
                "after": c.after,
                "issue_code": c.issue_code,
            }
            for c in changes
        ],
        "backup_path": str(backup_path.relative_to(run_dir)),
        "audit_path": str(audit_path.relative_to(run_dir)),
        "needs_operator_decision": needs_decision,
    }
    audit_path.write_text(
        json.dumps(audit_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return backup_path, audit_path


def _apply_changes(
    meta: dict[str, Any], changes: list[RunStateRepairChange]
) -> dict[str, Any]:
    """Return a new meta dict with ``changes`` applied (after=None removes)."""
    new_meta = dict(meta)
    for change in changes:
        if change.after is None:
            new_meta.pop(change.field, None)
        else:
            new_meta[change.field] = change.after
    return new_meta


def _write_backup(run_dir: Path, backup_path: Path) -> None:
    """Copy the original meta.json bytes to ``backup_path`` before mutation."""
    meta_file = run_dir / _META_FILENAME
    if meta_file.is_file():
        backup_path.write_bytes(meta_file.read_bytes())
    else:
        backup_path.write_text("{}\n", encoding="utf-8")


def _atomic_write_meta(run_dir: Path, meta: dict[str, Any]) -> None:
    """Atomically replace meta.json (temp file in run_dir + os.replace).

    Serializes identically to the lifecycle writer. On any failure the temp
    file is removed and a ``RuntimeError`` is raised; ``os.replace`` guarantees
    the original meta.json is never left partially written.
    """
    fd, tmp_name = tempfile.mkstemp(dir=run_dir, prefix=".meta.repair.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
            handle.flush()
            with contextlib.suppress(OSError):
                os.fsync(handle.fileno())
        os.replace(tmp_path, run_dir / _META_FILENAME)
    except Exception as e:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise RuntimeError(
            f"repair_run_state: failed to atomically replace meta.json in "
            f"{run_dir}: {e}. The original meta.json is unchanged and no audit "
            "artifact was written."
        ) from e


def _read_halt_decided_at(run_dir: Path) -> str | None:
    """Return the latest ``decided_at`` among halt decision artifacts.

    Reads ``phase_handoff_decisions/*.json`` tolerantly. Returns ``None`` when
    no readable halt decision carries a non-empty ``decided_at`` string, so the
    caller can fall back to the repair timestamp.
    """
    decisions_dir = run_dir / "phase_handoff_decisions"
    if not decisions_dir.is_dir():
        return None
    decided: list[str] = []
    for entry in sorted(decisions_dir.iterdir()):
        if not (entry.is_file() and entry.suffix == ".json"):
            continue
        try:
            raw = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(raw, dict)
            and raw.get("action") == "halt"
            and isinstance(raw.get("decided_at"), str)
            and raw["decided_at"]
        ):
            decided.append(raw["decided_at"])
    return max(decided) if decided else None


def _read_meta(run_dir: Path) -> dict[str, Any]:
    """Read meta.json tolerantly; return ``{}`` when absent or malformed."""
    meta_file = run_dir / _META_FILENAME
    if not meta_file.is_file():
        return {}
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


__all__ = [
    "RunStateRepairAction",
    "RunStateRepairChange",
    "RunStateRepairReport",
    "repair_run_state",
]
