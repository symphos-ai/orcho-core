"""Durable continuation classification for checkpoint, plan, and correction.

This module deliberately does not inspect transcript text.  It is the single
owner of the retained-change recovery decision consumed by the SDK, CLI, and
external control planes.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.io.git_helpers import has_uncommitted
from pipeline.run_state.status_vocab import PAUSE_STATUS, RESUMABLE_TERMINAL_STATUSES

ContinuationSubject = Literal["checkpoint", "plan_artifact", "retained_change", "none"]
ContinuationIntent = Literal["resume", "followup", "from_run_plan"]
ContinuationOperation = Literal[
    "resume_checkpoint", "start_followup", "launch_from_run_plan", "blocked",
]
RecommendedNextAction = Literal[
    "resume_checkpoint", "plan_artifact_continuation", "start_followup", "none",
]
DiffSource = Literal["worktree", "artifact", "none"]

_CORRECTION_REASONS = frozenset({
    "commit_decision_fix",
    "final_acceptance_rejected",
    "final_acceptance_no_diff",
})


@dataclass(frozen=True, slots=True)
class ContinuationDecision:
    """A client-neutral, durable answer to "what can continue this run?"."""

    run_id: str
    continuation_subject: ContinuationSubject
    recommended_next_action: RecommendedNextAction
    allowed_intents: tuple[Literal["followup", "exit"], ...]
    requires_operator_comment: bool
    checkpoint_resumable: bool
    retained_worktree: str | None
    diff_source: DiffSource | None
    blocked: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ContinuationRequest:
    """An explicit operator request to continue a durable run.

    Intent is deliberately separate from the run's phase/status: a terminal
    parent may have a checkpoint, a retained change, or a plan artifact, but
    only this reducer selects the one operation that can consume it.
    """

    run_id: str
    intent: ContinuationIntent
    operator_comment: str | None = None


@dataclass(frozen=True, slots=True)
class ContinuationResolution:
    """Canonical operation selection or an operator-readable blocker."""

    request: ContinuationRequest
    decision: ContinuationDecision
    operation: ContinuationOperation
    blocker: str | None = None


def _worktree_path(meta: Mapping[str, Any]) -> str | None:
    worktree = meta.get("worktree")
    if not isinstance(worktree, Mapping):
        return None
    path = worktree.get("path")
    return path.strip() if isinstance(path, str) and path.strip() else None


def _retained_worktree_is_isolated(meta: Mapping[str, Any]) -> bool:
    """Only an isolated parent checkout can prove a retained change."""
    worktree = meta.get("worktree")
    if not isinstance(worktree, Mapping):
        return False
    from pipeline.project.followup_worktree import parent_worktree_is_isolated

    project = meta.get("project")
    return parent_worktree_is_isolated(
        worktree,
        source_checkout=project if isinstance(project, str) else None,
    )


def _artifact_diff(parent_run_dir: Path | None) -> bool:
    if parent_run_dir is None:
        return False
    try:
        patch = parent_run_dir / "diff.patch"
        return patch.is_file() and patch.stat().st_size > 0
    except OSError:
        return False


def _has_persisted_plan(parent_run_dir: Path | None) -> bool:
    if parent_run_dir is None:
        return False
    try:
        return (parent_run_dir / "parsed_plan.json").is_file()
    except OSError:
        return False


def _is_correction_candidate(meta: Mapping[str, Any]) -> bool:
    if meta.get("status") != "halted":
        return False
    if meta.get("halt_reason") not in _CORRECTION_REASONS:
        return False
    # A commit-decision fix is itself the durable correction-gate selection.
    # Rejected final acceptance must retain its gate evidence rather than merely
    # carry a coincidental halt string.
    if meta.get("halt_reason") == "commit_decision_fix":
        return True
    phases = meta.get("phases")
    final = phases.get("final_acceptance") if isinstance(phases, Mapping) else None
    return isinstance(final, Mapping) and bool(final)


def resolve_continuation_decision(
    *, run_id: str, meta: Mapping[str, Any] | None, parent_run_dir: Path | None = None,
) -> ContinuationDecision:
    """Resolve continuation from durable state and the retained worktree.

    A correction candidate remains typed even if its worktree is gone.  This is
    intentional: callers can explain the blocked recovery without silently
    advertising an unrelated plan-artifact path.
    """
    if not isinstance(meta, Mapping):
        return ContinuationDecision(run_id, "none", "none", (), False, False, None, None, True, "missing or unreadable parent meta")

    if _is_correction_candidate(meta):
        retained = _worktree_path(meta)
        artifact = _artifact_diff(parent_run_dir)
        if not retained:
            return ContinuationDecision(run_id, "retained_change", "start_followup", ("followup", "exit"), True, False, None, "artifact" if artifact else "none", True, "parent meta has no retained worktree path")
        if not _retained_worktree_is_isolated(meta):
            return ContinuationDecision(
                run_id, "retained_change", "start_followup", ("followup", "exit"),
                True, False, retained, "artifact" if artifact else "none", True,
                "retained worktree is not an isolated checkout",
            )
        path = Path(retained)
        try:
            if not path.is_dir():
                raise OSError("path is not a directory")
            dirty = has_uncommitted(str(path))
        except (OSError, ValueError) as exc:
            return ContinuationDecision(run_id, "retained_change", "start_followup", ("followup", "exit"), True, False, retained, "artifact" if artifact else "none", True, f"retained worktree is unreadable: {retained} ({exc})")
        if not dirty:
            source: DiffSource = "artifact" if artifact else "none"
            reason = "retained worktree is clean"
            if artifact:
                reason += "; diff exists only as artifact and will not be applied"
            return ContinuationDecision(run_id, "retained_change", "start_followup", ("followup", "exit"), True, False, retained, source, True, reason)
        return ContinuationDecision(run_id, "retained_change", "start_followup", ("followup", "exit"), True, False, retained, "worktree", False, "terminal correction retains an uncommitted worktree change")

    if meta.get("status") in RESUMABLE_TERMINAL_STATUSES:
        return ContinuationDecision(run_id, "checkpoint", "resume_checkpoint", (), False, True, None, None, False, "checkpoint-resumable terminal state")
    if meta.get("status") == PAUSE_STATUS:
        return ContinuationDecision(
            run_id, "none", "none", (), False, False, None, None, False,
            "run is awaiting a phase-handoff decision",
        )
    # Older interrupted launches can have a supervisor record and task before
    # their first status write. They are not finalized and remain a checkpoint,
    # rather than being silently redirected to a new child operation.
    if meta.get("status") is None and isinstance(meta.get("task"), str):
        return ContinuationDecision(run_id, "checkpoint", "resume_checkpoint", (), False, True, None, None, False, "unclassified persisted checkpoint")
    return ContinuationDecision(run_id, "none", "none", (), False, False, None, None, False, "no continuation decision")


def resolve_continuation(
    request: ContinuationRequest,
    *,
    meta: Mapping[str, Any] | None,
    parent_run_dir: Path | None = None,
    allow_paused_checkpoint: bool = False,
) -> ContinuationResolution:
    """Reduce an explicit continuation intent to one safe operation.

    This is intentionally policy-only. Durable artifact reads and launch
    mechanics live at the SDK boundary, allowing CLI and SDK callers to share
    the same selection without importing each other's adapters.
    """
    decision = resolve_continuation_decision(
        run_id=request.run_id, meta=meta, parent_run_dir=parent_run_dir,
    )
    # Awaiting a handoff is intentionally not advertised as a resumable
    # checkpoint in operator read models: the next action is to decide the
    # handoff.  Launch preflight may opt in after that decision was persisted,
    # before the runner has written its next status.
    if (
        allow_paused_checkpoint
        and isinstance(meta, Mapping)
        and meta.get("status") == PAUSE_STATUS
    ):
        decision = ContinuationDecision(
            request.run_id,
            "checkpoint",
            "resume_checkpoint",
            decision.allowed_intents,
            False,
            True,
            None,
            None,
            False,
            "checkpoint-resumable paused state after a persisted decision",
        )
    if decision.blocked:
        return ContinuationResolution(request, decision, "blocked", decision.reason)
    if request.intent == "resume":
        if decision.continuation_subject == "checkpoint":
            return ContinuationResolution(request, decision, "resume_checkpoint")
        return ContinuationResolution(
            request, decision, "blocked",
            "same-run resume is only available for a checkpoint-resumable state",
        )
    if request.intent == "followup":
        if decision.continuation_subject != "retained_change":
            return ContinuationResolution(
                request, decision, "blocked",
                "follow-up requires a retained-change continuation subject",
            )
        if not (request.operator_comment or "").strip():
            return ContinuationResolution(
                request, decision, "blocked", "operator_comment is required for a follow-up",
            )
        return ContinuationResolution(request, decision, "start_followup")
    if request.intent == "from_run_plan":
        if decision.continuation_subject == "retained_change":
            return ContinuationResolution(
                request, decision, "blocked",
                "from-run-plan cannot be used for a retained-change correction",
            )
        if _has_persisted_plan(parent_run_dir):
            return ContinuationResolution(request, decision, "launch_from_run_plan")
        return ContinuationResolution(
            request, decision, "blocked", "parent has no persisted parsed plan artifact",
        )
    # Kept defensive for callers that deserialize an untyped wire payload.
    return ContinuationResolution(request, decision, "blocked", "unknown continuation intent")


__all__ = [
    "ContinuationDecision", "ContinuationIntent", "ContinuationOperation",
    "ContinuationRequest", "ContinuationResolution", "resolve_continuation",
    "resolve_continuation_decision",
]
