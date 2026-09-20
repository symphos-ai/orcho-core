# SPDX-License-Identifier: Apache-2.0
"""Retry-subject proofs for the operator resume paths.

Two resumes re-execute work against a subject a prior phase already
observed, and each must prove that subject is still there before mutating
anything: :func:`ensure_repair_subject_proven` for a review retry (the
rejected diff must be present to repair) and
:func:`ensure_verification_subject_retained` for an env-failure gate retry
(the exact tree the failing receipts measured must still be the one under
the gate). Both are read-only and raise :class:`RepairSubjectUnproven`.

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

The verification guard is documented on
:func:`ensure_verification_subject_retained`; it proves the *opposite*
property (the subject is unchanged), so it shares none of the dirty-tree
requirement above.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.io.git_helpers import git_head, has_uncommitted
from pipeline.engine.worktree import is_worktree_reclaimed

# The operator-facing recoverable message for the clean-HEAD case. Quoted
# verbatim by callers and pinned by tests.
CLEAN_HEAD_MESSAGE = (
    "Cannot run repair_changes against clean HEAD: review retry requires the "
    "retained rejected diff subject. Resume/apply the retained worktree diff "
    "or halt this run."
)


class RepairSubjectUnproven(RuntimeError):
    """The retry subject this module was asked to prove is not present.

    Raised for the review-retry repair subject (the rejected diff) and,
    by :func:`ensure_verification_subject_retained`, for the retained
    verification subject an env retry must re-execute against.

    Recoverable: the guard runs before any state mutation, so the active
    handoff + its decision survive. Restore the retained worktree (or
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


def ensure_verification_subject_retained(
    *,
    cwd: str,
    worktree_block: dict[str, Any] | None,
    expected_head: str | None,
) -> None:
    """Raise :class:`RepairSubjectUnproven` unless the *verification* subject survived.

    A ``retry_verification`` re-execution runs no agent and changes nothing in
    the checkout: the operator repaired the environment *outside* the run. So
    the subject this proves is the opposite of
    :func:`ensure_repair_subject_proven`'s — not "a diff is present to fix",
    but "the exact tree the failing receipts observed is still the one we are
    about to re-measure". A dirty working tree is therefore neither required
    nor meaningful here, and ``tree_oid`` is deliberately not compared: a gate
    that writes into its own checkout (a cache dir, a build artifact) would
    move the tree oid without the run having been resumed anywhere else.

    ``expected_head`` is the ``observed_head_oid`` the failing receipts of this
    blocking set agree on, or ``None`` when every receipt recorded an
    unavailable subject. ``None`` skips only the HEAD comparison — the
    retained-worktree checks below stay mandatory, since a re-execution in a
    *different* checkout is not a retry of this gate set at all.

    Read-only: no session, decision, or handoff mutation.
    """
    isolation = (
        worktree_block.get("isolation")
        if isinstance(worktree_block, dict)
        else None
    )
    if isinstance(worktree_block, dict) and isolation != "off":
        recorded_path = worktree_block.get("path")
        if not (isinstance(recorded_path, str) and recorded_path.strip()):
            raise RepairSubjectUnproven(
                "Cannot re-run the verification gate: the run records an "
                "isolated worktree with no path, so the retained verification "
                "subject cannot be identified. Halt this run instead.",
            )
        if is_worktree_reclaimed(worktree_block):
            raise RepairSubjectUnproven(
                f"Cannot re-run the verification gate: the retained worktree "
                f"{recorded_path!r} was reclaimed by workspace cleanup, so its "
                "recorded path is historical. Restore the archive explicitly "
                "or begin a new recovery run.",
            )
        if not Path(recorded_path).exists():
            raise RepairSubjectUnproven(
                f"Cannot re-run the verification gate: the retained worktree "
                f"{recorded_path!r} no longer exists. Re-running in a fresh "
                "checkout would measure a different subject than the one the "
                "failing receipts observed. Restore it or halt this run.",
            )
        if _normalised(cwd) != _normalised(recorded_path):
            raise RepairSubjectUnproven(
                f"Cannot re-run the verification gate: the gate working "
                f"directory {cwd!r} does not match the retained verification "
                f"subject {recorded_path!r}. Restore the retained worktree or "
                "halt this run.",
            )

    if expected_head is None:
        return
    head = git_head(cwd)
    if head != expected_head:
        raise RepairSubjectUnproven(
            f"Cannot re-run the verification gate: the retained subject is at "
            f"HEAD {head!r}, but the failing receipts observed "
            f"{expected_head!r}. The retry would re-measure different content "
            "than the operator decided on. Halt this run instead.",
        )


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
    "ensure_verification_subject_retained",
    "guard_review_retry_subject",
]
