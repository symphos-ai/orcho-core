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
from pipeline.run_state.status_vocab import RESUMABLE_TERMINAL_STATUSES

ContinuationSubject = Literal["checkpoint", "plan_artifact", "retained_change", "none"]
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
    return ContinuationDecision(run_id, "none", "none", (), False, False, None, None, False, "no continuation decision")


__all__ = ["ContinuationDecision", "resolve_continuation_decision"]
