# SPDX-License-Identifier: Apache-2.0
"""Recovery-lineage / continuation-subject resolver (ADR 0114).

The recovery half of the core-owned run-diagnosis read-model. Where
:mod:`sdk.run_control.diagnosis` owns the *condition* vocabulary (the priority
ladder mirroring MCP's ``project_run_diagnosis``), this module owns the
*continuation_subject* vocabulary and its resolution — the core mirror of MCP's
``project_recovery_lineage``. Splitting the two keeps each module a cohesive,
sub-700-line read-model rather than one God classifier.

Discipline matches the diagnosis half: it **never re-derives** terminal /
resumable logic. The single owners it leans on:

- terminality of a candidate source — :func:`is_terminal_resume_parent`
  and :func:`is_terminal_success` from
  :mod:`pipeline.control.resume_context`;
- worktree continuity — the persisted
  ``meta['worktree']['followup_continuity']`` block (the exact shape
  ``pipeline/project/isolation_setup.py`` writes via
  :meth:`FollowupWorktreeDecision.to_dict`).

No status / halt-reason decision-table literal is declared here; the only
frozensets (``_PERSISTED_PLAN_SOURCES`` / ``_PLANNING_PROFILES``) are
plan-attribution vocabularies, not lifecycle decision tables, so the ownership
guard (which protects the status / halt-reason sets) does not cover them.

Provider boundary: ``source_meta`` lets an embedder feed already-merged meta for
the *source* candidates so a stale on-disk ``status='running'`` cannot make core
recommend a blind ``recover_via_source_run`` for a source the supervisor already
settled as terminal. This module never reads a supervisor file. Strictly
read-only: it never prints, mutates, or finalizes.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pipeline.control.continuation import resolve_continuation_decision
from pipeline.control.resume_context import (
    detect_active_followup_child,
    is_terminal_resume_parent,
    is_terminal_success,
)
from pipeline.run_state.release_verdict import is_rejected
from sdk.run_control.delivery import delivery_decision_state
from sdk.run_control.recovery_lineage_resolve import (
    _MISSING_CHILD as _MISSING_CHILD,
    _MISSING_GATE as _MISSING_GATE,
    _MISSING_PLAN as _MISSING_PLAN,
    _MISSING_SOURCE as _MISSING_SOURCE,
    ACTION_STOP_UNKNOWN as ACTION_STOP_UNKNOWN,
    SUBJECT_UNKNOWN as SUBJECT_UNKNOWN,
    _strict_load_meta,
    _unreadable_meta_lineage,
)
from sdk.run_control.types import RecoveryLineage
from sdk.runs import _CWD_DEFAULT, find_run, load_meta

# ── Closed continuation-subject + next-action vocabulary ──────────────────────
# The ``unknown`` / ``stop_unknown`` dead-end half of this vocabulary
# (``SUBJECT_UNKNOWN`` / ``ACTION_STOP_UNKNOWN`` + the ``_MISSING_*`` fact labels)
# lives with the failure resolver in ``recovery_lineage_resolve`` and is imported
# above so a read failure projects one shared dead-end; the rest is owned here.
SUBJECT_ACTIVE_CHILD_RUN = "active_child_run"
SUBJECT_DELIVERY_GATE = "delivery_gate"
SUBJECT_SOURCE_RUN_CHECKPOINT = "source_run_checkpoint"
SUBJECT_PLAN_ARTIFACT = "plan_artifact"
SUBJECT_RETAINED_CHANGE = "retained_change"
SUBJECT_NONE = "none"

ACTION_RESUME_ACTIVE_CHILD = "resume_active_child"
ACTION_DELIVERY_DECISION = "delivery_decision"
ACTION_RESUME_SOURCE_RUN = "resume_source_run"
ACTION_START_FOLLOWUP = "start_followup"
ACTION_PLAN_ARTIFACT_CONTINUATION = "plan_artifact_continuation"

# ``meta.plan_source`` values that mean a parsed plan is durably attributable;
# the profile work-kinds that produce a plan artifact rather than an undelivered
# implementation diff. Neither is a lifecycle decision table (the ownership
# guard only protects the status / halt-reason sets), so they live here.
_PERSISTED_PLAN_SOURCES = frozenset({"local", "run", "cross"})
_PLANNING_PROFILES = frozenset({"planning", "research"})


class _Continuation:
    """The resolved continuation subject for a terminal/rejected dead-end."""

    __slots__ = ("subject", "action", "recommended_run_id", "source_run_id",
                 "missing_facts")

    def __init__(
        self,
        subject: str,
        action: str | None,
        recommended_run_id: str | None,
        source_run_id: str | None,
        missing_facts: tuple[str, ...],
    ) -> None:
        self.subject = subject
        self.action = action
        self.recommended_run_id = recommended_run_id
        self.source_run_id = source_run_id
        self.missing_facts = missing_facts


class _SourceFacts:
    """Resumability facts about one candidate source run."""

    __slots__ = ("run_id", "status", "resumable", "worktree_preserved")

    def __init__(
        self,
        run_id: str,
        status: str | None,
        resumable: bool,
        worktree_preserved: bool,
    ) -> None:
        self.run_id = run_id
        self.status = status
        self.resumable = resumable
        self.worktree_preserved = worktree_preserved


def _resolve_continuation(
    run_id: str,
    run_dir: Path,
    meta: dict[str, Any],
    parent_run_id: str | None,
    terminal: bool,
    *,
    has_worktree: bool,
    diff_source: str | None,
    workspace: Path | str | None,
    runs_dir: Path | str | None,
    cwd: Path | str | None | object,
    source_meta: Mapping[str, dict[str, Any]] | None = None,
) -> _Continuation:
    """Resolve the durable continuation subject of a terminal/rejected run.

    Only meaningful for a terminal dead-end (a non-terminal run continues
    itself → ``none``). Priority: a resumable source checkpoint, else a
    persisted plan artifact, else a clean terminal-success start-followup, else
    an explicit ``unknown`` with the missing durable facts enumerated.
    """
    if not terminal:
        return _Continuation(SUBJECT_NONE, None, None, None, ())

    plan_source_run_id = _optional_str(meta.get("plan_source_run_id"))
    source = _resolve_source(
        parent_run_id, plan_source_run_id,
        workspace=workspace, runs_dir=runs_dir, cwd=cwd,
        source_meta=source_meta,
    )
    source_run_id = source.run_id if source else None

    if source is not None and source.resumable:
        return _Continuation(
            SUBJECT_SOURCE_RUN_CHECKPOINT, ACTION_RESUME_SOURCE_RUN,
            source.run_id, source.run_id, (),
        )

    if _plan_subject_available(
        meta, run_dir, has_worktree=has_worktree, diff_source=diff_source,
    ):
        return _Continuation(
            SUBJECT_PLAN_ARTIFACT, ACTION_PLAN_ARTIFACT_CONTINUATION,
            run_id, source_run_id, (),
        )

    if is_terminal_success(meta):
        return _Continuation(
            SUBJECT_NONE, ACTION_START_FOLLOWUP, None, source_run_id, (),
        )

    # Dead-end with no durable continuation fact → stop, and say which facts
    # are absent (gate / child were ruled out by the earlier branches).
    missing: list[str] = []
    if source is None or not source.resumable:
        missing.append(_MISSING_SOURCE)
    missing.append(_MISSING_PLAN)
    missing.append(_MISSING_GATE)
    missing.append(_MISSING_CHILD)
    return _Continuation(
        SUBJECT_UNKNOWN, ACTION_STOP_UNKNOWN, None, source_run_id, tuple(missing),
    )


def _resolve_source(
    parent_run_id: str | None,
    plan_source_run_id: str | None,
    *,
    workspace: Path | str | None,
    runs_dir: Path | str | None,
    cwd: Path | str | None | object,
    source_meta: Mapping[str, dict[str, Any]] | None = None,
) -> _SourceFacts | None:
    """Resolve the best source candidate from the durable pointers.

    Returns the first *resumable* candidate (``parent_run_id`` then
    ``plan_source_run_id``); else the first readable candidate's facts so the
    caller can still report its status; ``None`` when none is readable.
    """
    candidates: list[str] = []
    if parent_run_id:
        candidates.append(parent_run_id)
    if plan_source_run_id and plan_source_run_id not in candidates:
        candidates.append(plan_source_run_id)

    first_readable: _SourceFacts | None = None
    for cid in candidates:
        facts = _resolve_source_facts(
            cid, workspace=workspace, runs_dir=runs_dir, cwd=cwd,
            source_meta=source_meta,
        )
        if facts is None:
            continue
        if first_readable is None:
            first_readable = facts
        if facts.resumable:
            return facts
    return first_readable


def _resolve_source_facts(
    source_run_id: str,
    *,
    workspace: Path | str | None,
    runs_dir: Path | str | None,
    cwd: Path | str | None | object,
    source_meta: Mapping[str, dict[str, Any]] | None = None,
) -> _SourceFacts | None:
    """Resolve resumability facts for one candidate source run.

    A source is resumable when it is NOT a terminal-resume-parent (the canonical
    predicate) AND it has retained work — a preserved worktree OR a persisted
    plan. When ``source_meta`` carries an already-resolved meta for this
    candidate (an embedder's supervisor-merged status), it is used verbatim in
    place of the on-disk read, so a stale on-disk status cannot drive a blind
    resume. Any read failure degrades to ``None`` so a corrupt source-meta cannot
    break the inspected run's diagnosis.
    """
    meta: dict[str, Any] | None = None
    if source_meta is not None:
        provided = source_meta.get(source_run_id)
        if isinstance(provided, dict):
            meta = provided
    if meta is None:
        try:
            ref = find_run(
                source_run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd,
            )
            meta = load_meta(ref.run_dir)
        except Exception:  # noqa: BLE001 — a read-only probe must never raise
            return None
    if not isinstance(meta, dict):
        return None

    not_terminal = not is_terminal_resume_parent(meta)
    has_worktree, blocked, _, _ = _worktree_continuity(meta)
    worktree_preserved = has_worktree and not blocked
    plan_source = _optional_str(meta.get("plan_source"))
    has_plan = bool(plan_source) and plan_source != "none"
    retained = worktree_preserved or has_plan
    return _SourceFacts(
        run_id=source_run_id,
        status=_optional_str(meta.get("status")),
        resumable=not_terminal and retained,
        worktree_preserved=worktree_preserved,
    )


def _plan_subject_available(
    meta: dict[str, Any],
    run_dir: Path,
    *,
    has_worktree: bool,
    diff_source: str | None,
) -> bool:
    """Whether this run is a plan-only continuation subject.

    True when a parsed plan is persisted (``meta.plan_source`` in
    ``{local, run, cross}``) AND a durable, readable ``parsed_plan.json`` exists
    AND the run carries no undelivered diff / retained worktree AND the profile
    is plan-only / research. The artifact check keeps from_run_plan honest: a
    bare ``plan_source`` stamp without a persisted plan is NOT a plan subject.
    """
    plan_source = _optional_str(meta.get("plan_source"))
    if plan_source not in _PERSISTED_PLAN_SOURCES:
        return False
    if not _has_durable_parsed_plan(run_dir):
        return False
    no_retained_diff = (not has_worktree) or diff_source == "none"
    if not no_retained_diff:
        return False
    prof = (_optional_str(meta.get("profile")) or "").lower()
    return (
        prof in _PLANNING_PROFILES
        or "planning" in prof
        or "research" in prof
    )


# ── Durable-fact read helpers (shared with the diagnosis half) ────────────────


def _worktree_continuity(
    meta: dict[str, Any],
) -> tuple[bool, bool, str | None, str | None]:
    """Read ``meta['worktree']['followup_continuity']`` → continuity facts.

    Returns ``(has_worktree, blocked, block_message, diff_source)`` from the
    exact persisted shape ``pipeline/project/isolation_setup.py`` writes via
    :meth:`FollowupWorktreeDecision.to_dict` (``{mode_label, blocked, reason,
    diff_source}``). A run with a worktree but no follow-up sub-block kept its
    own worktree (not blocked); a missing block has no worktree. Never raises.
    """
    wt = meta.get("worktree") if isinstance(meta, dict) else None
    if not isinstance(wt, dict) or not wt:
        return (False, False, None, None)
    fc = wt.get("followup_continuity")
    if not isinstance(fc, dict):
        return (True, False, None, None)
    return (
        True,
        bool(fc.get("blocked")),
        _optional_str(fc.get("reason")),
        _optional_str(fc.get("diff_source")),
    )


def _has_durable_parsed_plan(run_dir: Path) -> bool:
    """Whether a durable, readable ``parsed_plan.json`` artifact exists."""
    try:
        path = run_dir / "parsed_plan.json"
        if not path.is_file():
            return False
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except Exception:  # noqa: BLE001 — a missing / corrupt plan reads as absent
        return False


def _optional_str(value: Any) -> str | None:
    """Coerce ``value`` to a non-empty stripped ``str``, else ``None``."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s or None


# ── Public recovery-lineage read-model ────────────────────────────────────────
#
# Field-for-field + branch-for-branch parity with MCP's
# ``project_recovery_lineage`` → ``RecoveryLineageProjection``
# (``orcho_mcp.services.run_lineage``); the two surfaces stay identical so a
# client reads one durable lineage instead of re-deriving it twice.
#
# Field mapping (RecoveryLineage ↔ RecoveryLineageProjection): every field is a
# 1:1 mirror (``run_id``, ``is_terminal_or_rejected``, ``continuation_subject``,
# ``recommended_next_action``, ``recommended_run_id``, ``source_run_id``,
# ``source_status``, ``source_resumable``, ``source_worktree_preserved``,
# ``plan_subject_available``, ``active_child_run_id``, ``reason``). The lone shape
# difference is ``missing_facts``: ``tuple[str, ...]`` here (frozen+slots
# convention) vs ``list[str]`` there — same values, same order.
#
# Branch mapping (this ladder ↔ MCP ``project_recovery_lineage`` cases):
#   (0) inspected-run read failure → MCP defensive ``except`` → unknown/stop_unknown
#       (all four missing_facts);  (1) active child → MCP Case B (active_child_run/
#       resume_active_child);  (2) pending gate → MCP gate branch (delivery_gate/
#       delivery_decision);  (3) terminal/rejected dead-end: (3a) resumable source →
#       Case A (source_run_checkpoint/resume_source_run), (3b) plan-only → Case C
#       (plan_artifact/plan_artifact_continuation), (3c) clean terminal-success →
#       none/start_followup, (3d) no continuation fact → Case D (unknown/stop_unknown);
#   (4/5) non-terminal → none/None *with* source_*+plan_subject_available enrichment.
#
# ``is_terminal_or_rejected`` reproduces MCP's
# ``terminal or (release == 'rejected' and not gate_pending)`` purely by composing
# existing core predicates: ``is_terminal_resume_parent`` (terminal),
# :func:`delivery_decision_state` ``.decidable`` (gate_pending), and the durable
# ``commit_delivery.release_verdict`` (the fact
# :func:`sdk.run_control.delivery._is_rejected_release_gate` reads) mapped exactly
# as MCP's ``_map_release``. No new gate-kind frozenset — the ownership guard is
# untouched.


def recovery_lineage(
    run_id: str,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
    meta: dict[str, Any] | None = None,
    source_meta: Mapping[str, dict[str, Any]] | None = None,
) -> RecoveryLineage:
    """Classify a run's durable recovery lineage into one :class:`RecoveryLineage`.

    The core mirror of MCP's ``project_recovery_lineage`` — see the module-level
    field/branch mapping. Resolves the run directory through
    :func:`sdk.runs.find_run` and reads the inspected run's ``meta.json`` lazily
    via the failure-aware :func:`_strict_load_meta`; when ``meta`` is supplied it
    is used verbatim (the embedder seam an already-merged supervisor status flows
    through — this module never reads a provider file). ``source_meta`` is the
    same seam for the *source* candidates (see :func:`_resolve_source_facts`).

    Fully defensive: any failure reading the inspected run's meta — a missing,
    corrupt, or non-object ``meta.json`` — degrades to an ``unknown`` /
    ``stop_unknown`` projection (all four ``missing_facts``) with a fact-built
    ``reason`` rather than raising. The sole exception is an empty ``run_id`` →
    :class:`ValueError`, matching ``run_diagnosis``.
    """
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("recovery_lineage: run_id must be a non-empty string")

    try:
        ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
        run_dir = ref.run_dir
        # Strict read of the inspected run's meta: unlike the tolerant
        # ``load_meta`` (which returns ``{}`` so status/history still render), a
        # missing / corrupt inspected meta must degrade to ``unknown`` /
        # ``stop_unknown`` here, not be misread as a bare non-terminal ``none``.
        resolved_meta = meta if isinstance(meta, dict) else _strict_load_meta(run_dir)
    except Exception as exc:  # noqa: BLE001 — defensive: never raise here
        return _unreadable_meta_lineage(run_id, type(exc).__name__)

    return _build_recovery_lineage(
        run_id,
        run_dir,
        resolved_meta,
        workspace=workspace,
        runs_dir=runs_dir,
        cwd=cwd,
        source_meta=source_meta,
    )


def _build_recovery_lineage(
    run_id: str,
    run_dir: Path,
    meta: dict[str, Any],
    *,
    workspace: Path | str | None,
    runs_dir: Path | str | None,
    cwd: Path | str | None | object,
    source_meta: Mapping[str, dict[str, Any]] | None = None,
) -> RecoveryLineage:
    """Resolve the five-branch recovery ladder from an already-read run dir + meta.

    Split from :func:`recovery_lineage` so ``run_diagnosis`` (T3) can attach the
    lineage from its already-resolved ``meta`` / ``source_meta`` without a second
    on-disk read of the inspected run.
    """
    status = _optional_str(meta.get("status"))
    halt_reason = _optional_str(meta.get("halt_reason"))
    parent_run_id = _optional_str(meta.get("parent_run_id"))
    plan_source_run_id = _optional_str(meta.get("plan_source_run_id"))

    terminal = is_terminal_resume_parent(meta)
    continuation = resolve_continuation_decision(
        run_id=run_id, meta=meta, parent_run_dir=run_dir,
    )

    # (1) active follow-up child supersedes the inspected run (composed predicate).
    child = _safe_active_child(run_id, run_dir.parent)
    active_child_run_id = child.child_run_id if child is not None else None

    # (2) pending delivery / correction gate (composed predicate). ``decidable``
    # is the core mirror of MCP's ``gate.kind in {delivery,correction}_decision_required``.
    state = _safe_delivery_state(
        run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd, meta=meta,
    )
    gate_pending = state is not None and state.decidable

    rejected_release = _rejected_release(meta)
    rejected_deadend = rejected_release and not gate_pending
    is_terminal_or_rejected = terminal or rejected_deadend

    if active_child_run_id is not None:
        return RecoveryLineage(
            run_id=run_id,
            is_terminal_or_rejected=is_terminal_or_rejected,
            continuation_subject=SUBJECT_ACTIVE_CHILD_RUN,
            recommended_next_action=ACTION_RESUME_ACTIVE_CHILD,
            recommended_run_id=active_child_run_id,
            active_child_run_id=active_child_run_id,
            reason=(
                f"a newer unfinished follow-up child {active_child_run_id} "
                "supersedes this run; resume the child"
            ),
        )

    if continuation.continuation_subject == SUBJECT_RETAINED_CHANGE:
        return RecoveryLineage(
            run_id=run_id,
            is_terminal_or_rejected=True,
            continuation_subject=SUBJECT_RETAINED_CHANGE,
            recommended_next_action=continuation.recommended_next_action,
            recommended_run_id=run_id if not continuation.blocked else None,
            source_worktree_preserved=not continuation.blocked,
            missing_facts=() if not continuation.blocked else (_MISSING_SOURCE,),
            reason=continuation.reason,
        )

    # (2) pending delivery / correction gate — a decision, not a dead-end.
    if gate_pending:
        return RecoveryLineage(
            run_id=run_id,
            is_terminal_or_rejected=is_terminal_or_rejected,
            continuation_subject=SUBJECT_DELIVERY_GATE,
            recommended_next_action=ACTION_DELIVERY_DECISION,
            recommended_run_id=run_id,
            reason=(
                "run is paused at an Orcho-managed delivery/correction gate "
                f"(kind={state.kind}); resolve it with a delivery decision"
            ),
        )

    # Source + plan facts shared by branches (3) and (4/5).
    has_worktree, _blocked, _block_message, diff_source = _worktree_continuity(meta)
    source = _resolve_source(
        parent_run_id, plan_source_run_id,
        workspace=workspace, runs_dir=runs_dir, cwd=cwd, source_meta=source_meta,
    )
    plan_available = _plan_subject_available(
        meta, run_dir, has_worktree=has_worktree, diff_source=diff_source,
    )

    # (3) terminal / rejected dead-end.
    if is_terminal_or_rejected:
        # (3a) resumable source — resume the source, NOT a fresh from_run_plan.
        if source is not None and source.resumable:
            return RecoveryLineage(
                run_id=run_id,
                is_terminal_or_rejected=True,
                continuation_subject=SUBJECT_SOURCE_RUN_CHECKPOINT,
                recommended_next_action=ACTION_RESUME_SOURCE_RUN,
                recommended_run_id=source.run_id,
                source_run_id=source.run_id,
                source_status=source.status,
                source_resumable=True,
                source_worktree_preserved=source.worktree_preserved,
                plan_subject_available=plan_available,
                reason=(
                    f"run is a terminal/rejected dead-end (status={status}"
                    + (f", halt_reason={halt_reason}" if halt_reason else "")
                    + f"); source run {source.run_id} (status={source.status}) "
                    "is resumable — resume it"
                ),
            )

        # (3b) plan-only subject — implement the persisted plan as a new run.
        if plan_available:
            return RecoveryLineage(
                run_id=run_id,
                is_terminal_or_rejected=True,
                continuation_subject=SUBJECT_PLAN_ARTIFACT,
                recommended_next_action=ACTION_PLAN_ARTIFACT_CONTINUATION,
                recommended_run_id=run_id,
                source_run_id=source.run_id if source else None,
                source_status=source.status if source else None,
                source_worktree_preserved=bool(
                    source and source.worktree_preserved
                ),
                plan_subject_available=True,
                reason=(
                    "run holds a persisted plan artifact (plan_source="
                    f"{_optional_str(meta.get('plan_source'))}, profile="
                    f"{_optional_str(meta.get('profile'))}); start a new "
                    "implementation run from the plan"
                ),
            )

        # (3c) clean terminal-success with no recovery subject → start fresh.
        if is_terminal_success(meta) and not rejected_release:
            return RecoveryLineage(
                run_id=run_id,
                is_terminal_or_rejected=True,
                continuation_subject=SUBJECT_NONE,
                recommended_next_action=ACTION_START_FOLLOWUP,
                source_run_id=source.run_id if source else None,
                source_status=source.status if source else None,
                plan_subject_available=False,
                reason=(
                    f"run completed cleanly (status={status}) with no resumable "
                    "source or plan artifact; start a fresh follow-up"
                ),
            )

        # (3d) dead-end with no durable continuation fact → unknown.
        return _unknown_lineage(
            run_id,
            is_terminal_or_rejected=True,
            source=source,
            plan_subject_available=plan_available,
            gate_pending=gate_pending,
            active_child_run_id=active_child_run_id,
            reason=(
                f"run is a terminal/rejected dead-end (status={status}"
                + (f", halt_reason={halt_reason}" if halt_reason else "")
                + ") with no resumable source, plan artifact, delivery gate, "
                "or active child"
            ),
        )

    # (4/5) non-terminal — continues itself; no action, but source/plan facts are
    # still enriched (MCP branch 4/5), so a client never sees a bare ``none``.
    return RecoveryLineage(
        run_id=run_id,
        is_terminal_or_rejected=False,
        continuation_subject=SUBJECT_NONE,
        recommended_next_action=None,
        source_run_id=source.run_id if source else None,
        source_status=source.status if source else None,
        source_resumable=bool(source and source.resumable),
        source_worktree_preserved=bool(source and source.worktree_preserved),
        plan_subject_available=plan_available,
        reason=(
            f"run is resumable itself (status={status}"
            + (f", halt_reason={halt_reason}" if halt_reason else "")
            + "); continue this run"
        ),
    )


def _unknown_lineage(
    run_id: str,
    *,
    is_terminal_or_rejected: bool,
    source: _SourceFacts | None,
    plan_subject_available: bool,
    gate_pending: bool,
    active_child_run_id: str | None,
    reason: str,
) -> RecoveryLineage:
    """Build the ``unknown`` / ``stop_unknown`` dead-end (mirrors MCP ``_unknown_projection``).

    ``missing_facts`` enumerates exactly which durable facts are absent so the
    captain sees *why* no continuation subject resolved, never a generic
    from_run_plan fallback.
    """
    missing: list[str] = []
    if source is None or not source.resumable:
        missing.append(_MISSING_SOURCE)
    if not plan_subject_available:
        missing.append(_MISSING_PLAN)
    if not gate_pending:
        missing.append(_MISSING_GATE)
    if active_child_run_id is None:
        missing.append(_MISSING_CHILD)
    return RecoveryLineage(
        run_id=run_id,
        is_terminal_or_rejected=is_terminal_or_rejected,
        continuation_subject=SUBJECT_UNKNOWN,
        recommended_next_action=ACTION_STOP_UNKNOWN,
        recommended_run_id=None,
        source_run_id=source.run_id if source else None,
        source_status=source.status if source else None,
        source_resumable=bool(source and source.resumable),
        source_worktree_preserved=bool(source and source.worktree_preserved),
        plan_subject_available=plan_subject_available,
        active_child_run_id=active_child_run_id,
        missing_facts=tuple(missing),
        reason=reason,
    )


def _rejected_release(meta: dict[str, Any]) -> bool:
    """Whether the durable release verdict is ``REJECTED``.

    Reads ``meta['commit_delivery']['release_verdict']`` — the same durable fact
    :func:`sdk.run_control.delivery._is_rejected_release_gate` and
    ``delivery_decision_state`` consult — and maps it exactly as MCP's
    ``delivery_gate._map_release`` does (``"rejected"`` only for a verbatim
    ``REJECTED`` verdict). Composing this with ``gate_pending`` reproduces MCP's
    rejected-dead-end test without declaring a new gate-kind decision table.
    """
    ctx = meta.get("commit_delivery") if isinstance(meta, dict) else None
    if not isinstance(ctx, dict):
        return False
    return is_rejected(ctx.get("release_verdict"))


def _safe_active_child(run_id: str, runs_dir: Path) -> Any:
    """Detect the newest active follow-up child, swallowing read errors to ``None``."""
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
    :func:`sdk.run_control.delivery.delivery_decision_state` — this module never
    re-derives a gate; an auxiliary read failure must never turn a resolvable run
    into a new failure point. ``meta`` threads the already-resolved inspected-run
    meta through the same provider seam so a supervisor-merged gate context (not
    yet on disk) still classifies the gate — without a second on-disk read of the
    inspected run.
    """
    try:
        return delivery_decision_state(
            run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd, meta=meta,
        )
    except Exception:  # noqa: BLE001 — read-only auxiliary projection
        return None


__all__ = ["RecoveryLineage", "recovery_lineage"]
