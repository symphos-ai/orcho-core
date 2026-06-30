# SPDX-License-Identifier: Apache-2.0
"""Repair-subject proof for the review-retry resume path.

After ``review_changes`` rejects a change and the operator decides
``retry_feedback``, the resumed run must run ``repair_changes`` against the
**rejected diff** — the very subject the reviewer looked at. If that subject
is gone (a clean HEAD on the recorded base, or the repair cwd no longer
points at the retained worktree), repairing would silently start from
scratch on an empty tree and "fix" nothing the reviewer saw.

This module proves the subject is present *before* the write phase dispatches:

* **isolated run** (``meta.worktree.isolation != off``): the repair cwd must
  be the recorded retained worktree path AND that worktree must carry the
  diff — either uncommitted changes (``git status --porcelain`` non-empty) or
  a committed diff (``HEAD`` moved off the recorded base). A clean HEAD
  sitting on the recorded base is an unproven subject.
* **isolation off**: there is no retained path to match, so only the
  dirty/HEAD-shift check in the cwd applies.

An unproven subject raises :class:`RepairSubjectUnproven` (a narrow
``RuntimeError`` subclass). The guard is strictly read-only — it never
mutates the session, the decision artifact, or the active handoff — so a run
that aborts here stays decidable and can be resumed again once the retained
worktree diff is restored.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.io.git_helpers import git_head, has_uncommitted

# The operator-facing recoverable message for the clean-HEAD case. Quoted
# verbatim by callers and pinned by tests.
CLEAN_HEAD_MESSAGE = (
    "Cannot run repair_changes against clean HEAD: review retry requires the "
    "retained rejected diff subject. Resume/apply the retained worktree diff "
    "or halt this run."
)


class RepairSubjectUnproven(RuntimeError):
    """The review-retry repair subject (the rejected diff) is not present.

    Recoverable: the guard runs before any state mutation, so the active
    handoff + its decision survive. Restore the retained worktree diff (or
    halt) and resume again.
    """


def _normalised(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(Path(path))


def _recorded_base(worktree_block: dict[str, Any] | None) -> str | None:
    """The HEAD the retained worktree started from (committed-diff anchor)."""
    if not isinstance(worktree_block, dict):
        return None
    for key in ("source_start_head", "base_ref"):
        value = worktree_block.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def ensure_repair_subject_proven(
    *,
    cwd: str,
    worktree_block: dict[str, Any] | None,
) -> None:
    """Raise :class:`RepairSubjectUnproven` unless the rejected diff is present.

    ``cwd`` is the working directory the repair phase will run in (the run's
    ``git_cwd`` / worktree path). ``worktree_block`` is the session's
    persisted ``worktree`` block (``isolation`` / ``path`` /
    ``base_ref`` / ``source_start_head``), or ``None`` when no worktree was
    recorded.

    The subject is proven when, for an isolated run, ``cwd`` matches the
    recorded retained path AND the tree carries the diff (dirty working tree
    OR ``HEAD`` moved off the recorded base); for an off-isolation run, only
    the dirty/HEAD-shift check applies. Read-only — no mutation.
    """
    isolation = (
        worktree_block.get("isolation")
        if isinstance(worktree_block, dict)
        else None
    )
    isolation_off = worktree_block is None or isolation == "off"

    recorded_path = (
        worktree_block.get("path") if isinstance(worktree_block, dict) else None
    )
    # Isolated run: the repair cwd must be the retained worktree. A mismatch
    # means the resume is about to repair the wrong tree (e.g. a fresh
    # checkout) and lose the rejected diff subject entirely.
    if (
        not isolation_off
        and recorded_path
        and _normalised(cwd) != _normalised(str(recorded_path))
    ):
        raise RepairSubjectUnproven(
            f"Cannot run repair_changes: the repair working directory "
            f"{cwd!r} does not match the retained rejected diff subject "
            f"{str(recorded_path)!r}. Resume/apply the retained worktree "
            "diff or halt this run."
        )

    # The diff itself must be present: uncommitted changes, or a committed
    # diff (HEAD advanced past the recorded base). Either is a valid subject.
    if has_uncommitted(cwd):
        return
    recorded_base = _recorded_base(worktree_block)
    if recorded_base is not None:
        head = git_head(cwd)
        if head is not None and head != recorded_base:
            return

    raise RepairSubjectUnproven(CLEAN_HEAD_MESSAGE)


def guard_review_retry_subject(run: Any) -> None:
    """Thin run-level adapter: prove the repair subject before dispatch.

    Resolves the repair cwd the same way the runtime does
    (``state.extras['git_cwd']`` → ``state.project_dir``) and the recorded
    worktree block from the session, then defers to
    :func:`ensure_repair_subject_proven`. Strictly read-only.
    """
    cwd = run.state.extras.get("git_cwd") or run.state.project_dir
    worktree_block = run.session.get("worktree")
    ensure_repair_subject_proven(cwd=str(cwd), worktree_block=worktree_block)


__all__ = [
    "CLEAN_HEAD_MESSAGE",
    "RepairSubjectUnproven",
    "ensure_repair_subject_proven",
    "guard_review_retry_subject",
]
