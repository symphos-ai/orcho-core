# SPDX-License-Identifier: Apache-2.0
"""Source-candidate facts for the recovery-lineage read-model (ADR 0114).

Extracted from :mod:`sdk.run_control.recovery_lineage` so the ladder module
stays a cohesive sub-700-line read-model. This module owns two things:

- the durable-fact read helpers shared by the lineage and diagnosis halves
  (:func:`_worktree_continuity`, :func:`_has_durable_parsed_plan`,
  :func:`_optional_str`);
- the resolution of one candidate *source* run (``parent_run_id`` /
  ``plan_source_run_id``) into :class:`_SourceFacts`.

Discipline: a source's resumability is **never re-derived here**. The single
owner of "can this run be continued, and how?" is the canonical launch
preflight, :func:`sdk.run_control.continuation.preflight_continuation` — the
exact check every launcher (``resume_run`` / ``launch_from_run_plan``) runs
before spawning. A recovery recommendation that preflight would refuse (for
example a same-run resume of a parent whose ``scheduled_gate_ledger.json`` was
finalized at ``run.end``) is a dead recommendation, so the facts reported here
are the preflight's answers, composed with the terminal-resume-parent predicate
(:func:`pipeline.control.resume_context.is_terminal_resume_parent`) that every
resume surface intercepts on before reaching preflight.

``worktree_preserved`` and ``has_plan`` stay *reported* facts only; they no
longer decide resumability.

Provider boundary: ``source_meta`` lets an embedder feed already-merged meta for
a candidate so a stale on-disk ``status='running'`` cannot drive a blind
recommendation. Strictly read-only; a read failure degrades to ``None``.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pipeline.control.continuation import (
    ContinuationIntent,
    ContinuationRequest,
    ContinuationResolution,
)
from pipeline.control.resume_context import is_terminal_resume_parent
from sdk.run_control.continuation import preflight_continuation
from sdk.runs import find_run, load_meta

_TERMINAL_SOURCE_BLOCKER = "source run is a terminal resume parent; resume is inert"
_PREFLIGHT_FAILED_BLOCKER = "continuation preflight could not read the source run"


class _SourceFacts:
    """Continuation facts about one candidate source run.

    ``resumable`` — a same-run checkpoint resume of the source would be
    accepted (preflight selects ``resume_checkpoint`` and the source is not a
    terminal resume parent); ``resume_blocker`` is the preflight's own blocker
    text when it would not. ``plan_launchable`` — a fresh ``from_run_plan``
    launch off the source's persisted plan artifact would be accepted.
    ``worktree_preserved`` / ``has_plan`` are reported facts only.
    """

    __slots__ = (
        "run_id", "status", "resumable", "resume_blocker", "plan_launchable",
        "worktree_preserved", "has_plan",
    )

    def __init__(
        self,
        run_id: str,
        status: str | None,
        *,
        resumable: bool,
        resume_blocker: str | None,
        plan_launchable: bool,
        worktree_preserved: bool,
        has_plan: bool,
    ) -> None:
        self.run_id = run_id
        self.status = status
        self.resumable = resumable
        self.resume_blocker = resume_blocker
        self.plan_launchable = plan_launchable
        self.worktree_preserved = worktree_preserved
        self.has_plan = has_plan


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
    ``plan_source_run_id``); else the first *plan-launchable* candidate; else
    the first readable candidate's facts so the caller can still report its
    status; ``None`` when none is readable.
    """
    candidates: list[str] = []
    if parent_run_id:
        candidates.append(parent_run_id)
    if plan_source_run_id and plan_source_run_id not in candidates:
        candidates.append(plan_source_run_id)

    first_readable: _SourceFacts | None = None
    first_launchable: _SourceFacts | None = None
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
        if first_launchable is None and facts.plan_launchable:
            first_launchable = facts
    return first_launchable or first_readable


def _resolve_source_facts(
    source_run_id: str,
    *,
    workspace: Path | str | None,
    runs_dir: Path | str | None,
    cwd: Path | str | None | object,
    source_meta: Mapping[str, dict[str, Any]] | None = None,
) -> _SourceFacts | None:
    """Resolve continuation facts for one candidate source run.

    The source's run directory is always resolved (the ledger and plan
    artifacts live there); its meta comes from ``source_meta`` when the
    embedder supplied an already-resolved one, else from disk. Both
    continuation intents are then asked of the canonical preflight against
    that directory + meta, so the reported ``resumable`` / ``plan_launchable``
    are exactly what ``resume_run`` / ``launch_from_run_plan`` would accept.
    Any read failure degrades to ``None`` so a corrupt source cannot break the
    inspected run's diagnosis.
    """
    try:
        ref = find_run(
            source_run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd,
        )
    except Exception:  # noqa: BLE001 — a read-only probe must never raise
        return None
    run_dir = ref.run_dir

    meta: dict[str, Any] | None = None
    if source_meta is not None:
        provided = source_meta.get(source_run_id)
        if isinstance(provided, dict):
            meta = provided
    if meta is None:
        try:
            meta = load_meta(run_dir)
        except Exception:  # noqa: BLE001 — a read-only probe must never raise
            return None
    if not isinstance(meta, dict):
        return None

    has_worktree, blocked, _, _ = _worktree_continuity(meta)
    plan_source = _optional_str(meta.get("plan_source"))

    resume = _safe_preflight(source_run_id, "resume", run_dir, meta)
    plan = _safe_preflight(source_run_id, "from_run_plan", run_dir, meta)
    if is_terminal_resume_parent(meta):
        resumable, resume_blocker = False, _TERMINAL_SOURCE_BLOCKER
    elif resume is None:
        resumable, resume_blocker = False, _PREFLIGHT_FAILED_BLOCKER
    elif resume.operation == "resume_checkpoint":
        resumable, resume_blocker = True, None
    else:
        resumable = False
        resume_blocker = resume.blocker or resume.operation

    return _SourceFacts(
        source_run_id,
        _optional_str(meta.get("status")),
        resumable=resumable,
        resume_blocker=resume_blocker,
        plan_launchable=plan is not None and plan.operation == "launch_from_run_plan",
        worktree_preserved=has_worktree and not blocked,
        has_plan=bool(plan_source) and plan_source != "none",
    )


def _safe_preflight(
    run_id: str,
    intent: ContinuationIntent,
    run_dir: Path,
    meta: dict[str, Any],
) -> ContinuationResolution | None:
    """Ask the canonical launch preflight for one intent, swallowing read errors."""
    try:
        return preflight_continuation(
            ContinuationRequest(run_id=run_id, intent=intent),
            parent_run_dir=run_dir,
            meta=meta,
        ).resolution
    except Exception:  # noqa: BLE001 — read-only auxiliary probe
        return None


# ── Durable-fact read helpers (shared by the lineage and diagnosis halves) ────


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
    from pipeline.engine.worktree import is_worktree_reclaimed
    if is_worktree_reclaimed(wt):
        return (
            False,
            True,
            "retained worktree was reclaimed; recorded path is historical",
            None,
        )
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
