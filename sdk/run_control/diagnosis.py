# SPDX-License-Identifier: Apache-2.0
"""Core-owned run-diagnosis read-model (ADR 0114).

A single read-only classifier that *composes* the existing P0 lifecycle
predicates into one :class:`~sdk.run_control.types.RunDiagnosis`. It is the
core-side mirror of the union of MCP's ``project_run_diagnosis`` (the
``condition`` vocabulary, owned here) and ``project_recovery_lineage`` (the
``continuation_subject`` vocabulary + resolution, owned by the cohesive sibling
:mod:`sdk.run_control.recovery_lineage`), so a headless client (MCP, UI,
supervisor) can read one durable diagnosis instead of re-deriving terminality
and lineage twice.

Discipline — this module **never re-derives** terminal / resumable logic. Each
branch leans on a single existing owner:

- ``needs_decision`` — :func:`sdk.phase_handoff._is_decidable_handoff_status`
  (which itself compares against ``PAUSE_STATUS`` / ``INTERRUPTED_STATUS``);
- ``needs_delivery_decision`` / ``correction_followup_required`` —
  :func:`sdk.run_control.delivery.delivery_decision_state` (its ``kind`` /
  ``decidable`` / ``available_actions``);
- ``superseded_by_child`` / ``active_child_run`` —
  :func:`pipeline.control.resume_context.detect_active_followup_child`;
- ``blocked_worktree`` — the persisted ``meta['worktree']['followup_continuity']``
  block read via :func:`sdk.run_control.recovery_lineage._worktree_continuity`;
- terminality (``resume_inert_terminal`` / ``closed_by_followup``) —
  :func:`~pipeline.control.resume_context.is_terminal_resume_parent`;
- the continuation subject / recovery lineage —
  :func:`sdk.run_control.recovery_lineage._resolve_continuation`;
- a resumable non-terminal stop — ``RESUMABLE_TERMINAL_STATUSES`` from
  :mod:`pipeline.run_state.status_vocab`.

No decision-table literal (a status ``frozenset``, the halt-reason map, or the
``{awaiting_phase_handoff, interrupted}`` set) is re-declared here — every set
is imported and every terminal / decidable question is answered by calling the
predicate that owns it.

Provider boundary: :func:`run_diagnosis` reads durable ``meta.json`` lazily and
accepts an already-resolved ``meta`` so an embedder's supervisor-merge (e.g.
reading ``mcp_supervisor.json``) stays *outside* core — this module never reads
a supervisor file. The same seam extends to recovery lineage via ``source_meta``
(see :mod:`sdk.run_control.recovery_lineage`). Strictly read-only: it never
prints, mutates, or finalizes.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from pipeline.control.resume_context import (
    detect_active_followup_child,
    is_terminal_resume_parent,
)
from pipeline.run_state.status_vocab import RESUMABLE_TERMINAL_STATUSES
from sdk.phase_handoff import _is_decidable_handoff_status
from sdk.run_control.delivery import delivery_decision_state

# The closed continuation-subject vocabulary + lineage resolver live in the
# recovery_lineage sibling. They are re-exported here so the read-model's full
# closed contract (condition + continuation_subject) resolves from one module.
from sdk.run_control.recovery_lineage import (
    _MISSING_CHILD as _MISSING_CHILD,
    _MISSING_GATE as _MISSING_GATE,
    _MISSING_PLAN as _MISSING_PLAN,
    _MISSING_SOURCE as _MISSING_SOURCE,
    ACTION_DELIVERY_DECISION as ACTION_DELIVERY_DECISION,
    ACTION_PLAN_ARTIFACT_CONTINUATION as ACTION_PLAN_ARTIFACT_CONTINUATION,
    ACTION_RESUME_ACTIVE_CHILD,
    ACTION_RESUME_SOURCE_RUN,
    ACTION_START_FOLLOWUP,
    ACTION_STOP_UNKNOWN as ACTION_STOP_UNKNOWN,
    SUBJECT_ACTIVE_CHILD_RUN,
    SUBJECT_DELIVERY_GATE,
    SUBJECT_NONE,
    SUBJECT_PLAN_ARTIFACT,
    SUBJECT_SOURCE_RUN_CHECKPOINT,
    SUBJECT_UNKNOWN,
    _build_recovery_lineage,
    _Continuation,
    _optional_str,
    _resolve_continuation,
    _worktree_continuity,
)
from sdk.run_control.recovery_lineage_resolve import (
    _strict_load_meta,
    _unreadable_meta_lineage,
)
from sdk.run_control.types import RecoveryLineage, RunDiagnosis
from sdk.runs import _CWD_DEFAULT, find_run

__all__ = ["run_diagnosis"]

# ── Closed condition vocabulary (one per priority branch) ─────────────────────
CONDITION_NEEDS_DECISION = "needs_decision"
CONDITION_SUPERSEDED_BY_CHILD = "superseded_by_child"
CONDITION_BLOCKED_WORKTREE = "blocked_worktree"
CONDITION_CORRECTION_FOLLOWUP_REQUIRED = "correction_followup_required"
CONDITION_NEEDS_DELIVERY_DECISION = "needs_delivery_decision"
CONDITION_RECOVER_VIA_SOURCE_RUN = "recover_via_source_run"
CONDITION_RESUME_INERT_TERMINAL = "resume_inert_terminal"
CONDITION_CLOSED_BY_FOLLOWUP = "closed_by_followup"
CONDITION_ACTIVE = "active"

_RUNNING_STATUS = "running"


def run_diagnosis(
    run_id: str,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
    meta: dict[str, Any] | None = None,
    source_meta: Mapping[str, dict[str, Any]] | None = None,
) -> RunDiagnosis:
    """Classify a run's resume situation into one :class:`RunDiagnosis`.

    Resolves the run directory through :func:`sdk.runs.find_run` (so the
    missing-run contract matches the rest of the SDK) and reads ``meta.json``
    lazily. When ``meta`` is supplied it is used verbatim instead of the on-disk
    read — that is the seam an embedder uses to feed an already-merged status
    (e.g. supervisor state) without core ever reading a provider file. The
    condition classification stays tolerant of an unreadable on-disk meta
    (degrading to ``{}``), but the attached ``recovery`` honours the read failure
    (via :func:`sdk.run_control.recovery_lineage_resolve._strict_load_meta`) so it
    never diverges from the standalone :func:`recovery_lineage`.

    ``source_meta`` is the same seam for recovery lineage: a
    ``{source_run_id: resolved_meta}`` mapping the resumability probe consults
    *before* falling back to an on-disk read of that candidate. An embedder that
    has supervisor-merged a source run's status feeds the merged meta here so a
    stale on-disk ``status='running'`` cannot make core recommend a blind
    ``recover_via_source_run`` for a source the supervisor already settled as
    terminal. Candidates absent from the mapping still resolve from disk.
    Strictly read-only.

    Raises:
        ValueError: ``run_id`` is empty — a programming error, distinct from a
            run that merely cannot be classified.
    """
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_diagnosis: run_id must be a non-empty string")

    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    # Read the inspected meta once, failure-aware: a supplied ``meta`` is used
    # verbatim (embedder seam); otherwise a strict read distinguishes a corrupt /
    # missing / non-object ``meta.json`` from a genuinely empty one. The
    # condition classification below stays tolerant (an unreadable meta degrades
    # to ``{}`` exactly as before), but the recovery attachment honours the read
    # failure so it can never diverge from the standalone ``recovery_lineage()``.
    if isinstance(meta, dict):
        resolved_meta: dict[str, Any] = meta
        meta_error: str | None = None
    else:
        try:
            resolved_meta = _strict_load_meta(ref.run_dir)
            meta_error = None
        except Exception as exc:  # noqa: BLE001 — tolerant condition, failure-aware recovery
            resolved_meta = {}
            meta_error = type(exc).__name__

    diagnosis = _classify(
        run_id,
        ref.run_dir,
        resolved_meta,
        workspace=workspace,
        runs_dir=runs_dir,
        cwd=cwd,
        source_meta=source_meta,
    )
    # Additively attach the recovery read-model (ADR 0114, mirror of MCP's
    # ``RunDiagnosisProjection.recovery_lineage``). When the inspected meta could
    # not be read (and was not supplied), attach the *same* unknown/stop_unknown
    # dead-end the standalone ``recovery_lineage()`` returns, so the attached and
    # standalone read-models stay byte-identical. Otherwise build from the already
    # resolved ``meta`` / ``source_meta`` (no second on-disk read; provider seam
    # preserved). Defensive: an auxiliary lineage failure must never break the
    # condition classification above.
    if meta_error is not None:
        recovery: RecoveryLineage | None = _unreadable_meta_lineage(run_id, meta_error)
    else:
        recovery = _safe_recovery(
            run_id, ref.run_dir, resolved_meta,
            workspace=workspace, runs_dir=runs_dir, cwd=cwd, source_meta=source_meta,
        )
    return replace(diagnosis, recovery=recovery)


def _safe_recovery(
    run_id: str,
    run_dir: Path,
    meta: dict[str, Any],
    *,
    workspace: Path | str | None,
    runs_dir: Path | str | None,
    cwd: Path | str | None | object,
    source_meta: Mapping[str, dict[str, Any]] | None,
) -> RecoveryLineage | None:
    """Build the recovery lineage from the already-read meta, never raising."""
    try:
        return _build_recovery_lineage(
            run_id, run_dir, meta if isinstance(meta, dict) else {},
            workspace=workspace, runs_dir=runs_dir, cwd=cwd,
            source_meta=source_meta,
        )
    except Exception:  # noqa: BLE001 — auxiliary lineage must never break diagnosis
        return None


def _classify(
    run_id: str,
    run_dir: Path,
    meta: dict[str, Any],
    *,
    workspace: Path | str | None,
    runs_dir: Path | str | None,
    cwd: Path | str | None | object,
    source_meta: Mapping[str, dict[str, Any]] | None = None,
) -> RunDiagnosis:
    """Resolve the first matching branch in the fixed priority order."""
    status = _optional_str(meta.get("status"))
    halt_reason = _optional_str(meta.get("halt_reason"))
    active = meta.get("phase_handoff") if isinstance(meta, dict) else None
    parent_run_id = _optional_str(meta.get("parent_run_id"))

    # (1) needs_decision — paused (or torn-interrupted) awaiting a phase handoff.
    if _is_decidable_handoff_status(status, active):
        return _needs_decision(run_id, status, halt_reason, active)

    # (2) superseded_by_child — a newer unfinished follow-up child is live.
    child = _safe_active_child(run_id, run_dir.parent)
    if child is not None:
        return RunDiagnosis(
            run_id=run_id,
            condition=CONDITION_SUPERSEDED_BY_CHILD,
            reason=(
                f"a newer unfinished follow-up child {child.child_run_id} "
                "supersedes this run; resume the child instead of this parent"
            ),
            status=status,
            halt_reason=halt_reason,
            continuation_subject=SUBJECT_ACTIVE_CHILD_RUN,
            recommended_next_action=ACTION_RESUME_ACTIVE_CHILD,
            recommended_run_id=child.child_run_id,
            handoff_id=child.active_handoff_id,
        )

    # (3) blocked_worktree — follow-up worktree continuity is blocked.
    has_worktree, blocked, block_message, diff_source = _worktree_continuity(meta)
    if blocked:
        return RunDiagnosis(
            run_id=run_id,
            condition=CONDITION_BLOCKED_WORKTREE,
            reason=block_message or (
                "follow-up worktree continuity is blocked; the parent's "
                "undelivered diff is not replayable here"
            ),
            status=status,
            halt_reason=halt_reason,
            recommended_run_id=parent_run_id,
            blocked=True,
            block_message=block_message,
        )

    # (4) correction_followup_required / needs_delivery_decision — a parked
    # post-release delivery / correction gate (the gate kind is authoritative
    # even when halt_reason reads as a terminal ``commit_decision_fix``).
    state = _safe_delivery_state(
        run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd, meta=meta,
    )
    if state is not None and state.decidable:
        return _delivery_branch(run_id, status, halt_reason, state)

    # Recovery-lineage resolution shared by branches (5) and (6).
    terminal = is_terminal_resume_parent(meta)
    cont = _resolve_continuation(
        run_id, run_dir, meta, parent_run_id, terminal,
        has_worktree=has_worktree, diff_source=diff_source,
        workspace=workspace, runs_dir=runs_dir, cwd=cwd,
        source_meta=source_meta,
    )

    # (5) recover_via_source_run — resume the source checkpoint, not this run.
    if cont.subject == SUBJECT_SOURCE_RUN_CHECKPOINT:
        return RunDiagnosis(
            run_id=run_id,
            condition=CONDITION_RECOVER_VIA_SOURCE_RUN,
            reason=(
                f"run is a terminal/rejected dead-end (status={status}"
                + (f", halt_reason={halt_reason}" if halt_reason else "")
                + f"); source run {cont.recommended_run_id} is resumable — "
                "resume it"
            ),
            status=status,
            halt_reason=halt_reason,
            continuation_subject=SUBJECT_SOURCE_RUN_CHECKPOINT,
            recommended_next_action=ACTION_RESUME_SOURCE_RUN,
            recommended_run_id=cont.recommended_run_id,
            source_run_id=cont.source_run_id,
        )

    # (6) resume_inert_terminal / closed_by_followup — a terminal dead-end.
    if terminal:
        return _terminal_branch(run_id, status, halt_reason, meta, cont)

    # (7) active — the run is currently executing.
    if status == _RUNNING_STATUS:
        return RunDiagnosis(
            run_id=run_id,
            condition=CONDITION_ACTIVE,
            reason="run is currently running",
            status=status,
            halt_reason=halt_reason,
        )

    # (8) residual resumable non-terminal stop — halted / failed / interrupted
    # (the status itself is the condition). RESUMABLE_TERMINAL_STATUSES is the
    # canonical owner of that vocabulary, consulted only to document intent; the
    # condition mirrors the status regardless so no parser is needed.
    resumable = status in RESUMABLE_TERMINAL_STATUSES
    reason = (
        f"run is resumable (status={status}"
        + (f", halt_reason={halt_reason}" if halt_reason else "")
        + ")"
        if resumable
        else f"run is in status {status!r}; continue this run"
    )
    return RunDiagnosis(
        run_id=run_id,
        condition=status or SUBJECT_UNKNOWN,
        reason=reason,
        status=status,
        halt_reason=halt_reason,
        continuation_subject=SUBJECT_NONE,
    )


# ── Branch builders ──────────────────────────────────────────────────────────


def _needs_decision(
    run_id: str, status: str | None, halt_reason: str | None, active: Any,
) -> RunDiagnosis:
    """Build the ``needs_decision`` diagnosis from the active handoff payload."""
    handoff_id = None
    available_actions: tuple[str, ...] = ()
    if isinstance(active, dict):
        handoff_id = _optional_str(active.get("id"))
        available_actions = _str_tuple(active.get("available_actions"))
    id_suffix = f" ({handoff_id})" if handoff_id else ""
    return RunDiagnosis(
        run_id=run_id,
        condition=CONDITION_NEEDS_DECISION,
        reason=f"run is paused awaiting a phase-handoff decision{id_suffix}",
        status=status,
        halt_reason=halt_reason,
        handoff_id=handoff_id,
        available_actions=available_actions,
    )


def _delivery_branch(
    run_id: str, status: str | None, halt_reason: str | None, state: Any,
) -> RunDiagnosis:
    """Split a decidable delivery gate into correction-followup vs decision.

    A correction gate whose ``fix`` was already requested (only ``halt``
    remains in ``available_actions``) is NOT a "choose a delivery decide"
    decision: the actionable next step is a from_run_plan follow-up carrying the
    retained diff. Every other decidable gate is a pending operator decision.
    """
    available_actions = tuple(state.available_actions)
    if state.kind == "correction" and "fix" not in available_actions:
        return RunDiagnosis(
            run_id=run_id,
            condition=CONDITION_CORRECTION_FOLLOWUP_REQUIRED,
            reason=(
                "the release was rejected and a correction was requested "
                f"(halt_reason={halt_reason}); the next step is a from_run_plan "
                "follow-up carrying the retained diff — a bare resume or a "
                "repeated fix is inert"
            ),
            status=status,
            halt_reason=halt_reason,
            continuation_subject=SUBJECT_PLAN_ARTIFACT,
            recommended_next_action=ACTION_START_FOLLOWUP,
            available_actions=available_actions,
            delivery_gate_kind=state.kind,
        )
    return RunDiagnosis(
        run_id=run_id,
        condition=CONDITION_NEEDS_DELIVERY_DECISION,
        reason=(
            f"run is paused at an Orcho-managed {state.kind} gate; inspect the "
            "delivery decision state and choose one of its ready actions"
        ),
        status=status,
        halt_reason=halt_reason,
        continuation_subject=SUBJECT_DELIVERY_GATE,
        recommended_next_action=ACTION_DELIVERY_DECISION,
        recommended_run_id=run_id,
        available_actions=available_actions,
        delivery_gate_kind=state.kind,
    )


def _terminal_branch(
    run_id: str,
    status: str | None,
    halt_reason: str | None,
    meta: dict[str, Any],
    cont: _Continuation,
) -> RunDiagnosis:
    """Build ``closed_by_followup`` (superseded parent) or ``resume_inert_terminal``."""
    superseded = _superseded_followup_child(meta)
    if superseded is not None:
        return RunDiagnosis(
            run_id=run_id,
            condition=CONDITION_CLOSED_BY_FOLLOWUP,
            reason=(
                "run was superseded by a successful from_run_plan follow-up "
                f"({superseded}); it is closed and resume is inert"
            ),
            status=status,
            halt_reason=halt_reason,
            continuation_subject=SUBJECT_NONE,
            recommended_run_id=superseded,
            source_run_id=cont.source_run_id,
        )
    return RunDiagnosis(
        run_id=run_id,
        condition=CONDITION_RESUME_INERT_TERMINAL,
        reason=(
            f"run is terminal (status={status}"
            + (f", halt_reason={halt_reason}" if halt_reason else "")
            + "); resume is inert"
        ),
        status=status,
        halt_reason=halt_reason,
        continuation_subject=cont.subject,
        recommended_next_action=cont.action,
        recommended_run_id=cont.recommended_run_id,
        source_run_id=cont.source_run_id,
        missing_facts=cont.missing_facts,
    )


# ── Defensive read helpers ───────────────────────────────────────────────────


def _safe_active_child(run_id: str, runs_dir: Path) -> Any:
    """Detect the newest active follow-up child, swallowing read errors."""
    try:
        return detect_active_followup_child(
            parent_run_id=run_id, runs_dir=runs_dir,
        )
    except Exception:  # noqa: BLE001 — auxiliary lineage must never break this
        return None


def _safe_delivery_state(
    run_id: str,
    *,
    workspace: Path | str | None,
    runs_dir: Path | str | None,
    cwd: Path | str | None | object,
    meta: dict[str, Any] | None = None,
) -> Any:
    """Project the delivery decision state, swallowing read errors to ``None``.

    Single-sources the gate classification on
    :func:`sdk.run_control.delivery.delivery_decision_state` — diagnosis never
    re-derives a gate; an auxiliary read failure must never turn a resolvable
    run into a new failure point. ``meta`` threads the already-resolved meta
    through the provider seam so the gate branch honours a supervisor-merged
    ``commit_delivery`` context (the same ``meta=`` an embedder feeds
    ``run_diagnosis``) instead of re-reading a possibly-stale on-disk snapshot.
    """
    try:
        return delivery_decision_state(
            run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd, meta=meta,
        )
    except Exception:  # noqa: BLE001 — read-only auxiliary projection
        return None


def _superseded_followup_child(meta: dict[str, Any]) -> str | None:
    """Child run id from a durable ``superseded_by_followup`` marker, else None.

    Core finalization stamps ``superseded_by_followup`` on a rejected-FA /
    correction parent once a from_run_plan follow-up child has delivered,
    settling the parent to ``done``. Its presence means the parent is closed.
    """
    marker = meta.get("superseded_by_followup") if isinstance(meta, dict) else None
    if isinstance(marker, dict):
        child = marker.get("child_run_id")
        if isinstance(child, str) and child:
            return child
    return None


def _str_tuple(value: Any) -> tuple[str, ...]:
    """Coerce a payload list to a tuple of non-empty strs (order preserved)."""
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item:
            out.append(item)
    return tuple(out)
