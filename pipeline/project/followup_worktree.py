# SPDX-License-Identifier: Apache-2.0
"""Diff-aware worktree continuity classification for follow-up runs.

A follow-up run is meant to continue its parent's change session. The
parent's *undelivered* diff can live in one of two independent places:

* the parent's physical worktree, as uncommitted changes (only when the
  parent ran in an **isolated** per-run worktree); or
* a ``diff.patch`` artifact persisted in the parent's run directory.

This module classifies the continuity case and decides how the follow-up
should pick up the worktree. It deliberately performs **no** ownership or
status check on the parent run — that is out of scope.

Three cases, evaluated in this order:

1. **Undelivered diff** (strict continuity, preserved verbatim):
   * dirty *isolated* parent worktree -> reuse it
     (``diff_source='worktree'``).
   * artifact-only diff (parent worktree clean/absent/non-isolated, but a
     non-empty ``diff.patch`` exists) -> block before any write phase
     (``diff_source='artifact'``). This run does not apply diff artifacts,
     so silently starting on a clean HEAD would drop the parent's change.
2. **Plan-artifact continuation** — no undelivered diff, but the parent
   produced a durable plan artifact -> allow a *fresh* worktree built from
   that plan (``diff_source='plan_artifact'``, ``effective_parent_worktree
   =None``). Same semantics as ``--from-run-plan``.
3. **Nothing to continue** — no undelivered diff and no plan artifact ->
   block with an operator message naming what is missing
   (``diff_source='none'``).

Dirty-worktree detection is ``git status --porcelain`` on the parent
worktree path, but only for an *isolated* parent. A parent counts as
isolated only when its meta carries an explicit isolated marker
(``isolation`` present and not ``'off'``) AND its path is not the shared
source checkout (``source_checkout``). A ``worktree_isolation=off`` parent,
an incomplete meta with no ``isolation`` key, or a worktree whose path
equals the source checkout all ran in-place on the shared checkout, so
their working-tree dirtiness is NOT an undelivered isolated diff and must
not trigger reuse or an artifact block. The two diff sources are kept
separate and never collapsed into one flag.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from core.io.git_helpers import has_uncommitted


@dataclasses.dataclass(frozen=True)
class ParentDiffSources:
    """The two independent places a parent's undelivered diff can live."""

    worktree_dirty: bool
    artifact_diff: bool


@dataclasses.dataclass(frozen=True)
class FollowupWorktreeDecision:
    """Resolved follow-up worktree continuity decision.

    ``effective_parent_worktree`` is the parent worktree metadata to hand
    the worktree resolver: the parent dict on reuse, ``None`` otherwise.
    """

    mode_label: str
    blocked: bool
    block_message: str | None
    diff_source: Literal["worktree", "artifact", "plan_artifact", "none"]
    effective_parent_worktree: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        """Persistable view for the session worktree block."""
        return {
            "mode_label": self.mode_label,
            "blocked": self.blocked,
            "reason": self.block_message,
            "diff_source": self.diff_source,
        }


def detect_parent_diff_sources(
    parent_run_dir: Path | None,
    parent_worktree_path: str | None,
    *,
    parent_worktree_isolated: bool = True,
) -> ParentDiffSources:
    """Probe the two undelivered-diff sources of a follow-up parent.

    ``worktree_dirty`` is True only when the parent ran in an *isolated*
    worktree (``parent_worktree_isolated``) AND ``parent_worktree_path``
    exists on disk AND ``git status --porcelain`` there is non-empty. A
    non-isolated (``worktree_isolation=off``) parent ran in-place on the
    shared source checkout, so its dirtiness is not an undelivered isolated
    diff and is never probed here.

    ``artifact_diff`` is True when ``parent_run_dir`` holds a non-empty
    ``diff.patch``. The two are reported separately and never merged.
    """
    worktree_dirty = False
    if parent_worktree_isolated and parent_worktree_path:
        path = Path(parent_worktree_path)
        if path.exists():
            worktree_dirty = has_uncommitted(str(path))

    artifact_diff = False
    if parent_run_dir is not None:
        diff_patch = Path(parent_run_dir) / "diff.patch"
        try:
            artifact_diff = diff_patch.is_file() and diff_patch.stat().st_size > 0
        except OSError:
            artifact_diff = False

    return ParentDiffSources(
        worktree_dirty=worktree_dirty,
        artifact_diff=artifact_diff,
    )


def _parent_worktree_is_isolated(
    followup_parent_worktree: dict[str, Any] | None,
    parent_worktree_path: str | None,
    source_checkout: str | Path | None,
) -> bool:
    """True only for an explicitly isolated parent worktree subject.

    A parent worktree counts as isolated — and therefore as a place an
    *undelivered diff* can live — only when its meta marks an explicit isolated
    mode (``isolation`` present and not ``'off'``) AND its path is not the shared
    source checkout. An ``isolation='off'`` parent, an incomplete meta with no
    ``isolation`` key, or a worktree whose path equals ``source_checkout`` all
    ran in-place on the shared checkout, so their dirtiness is NOT an undelivered
    isolated diff. This guards against a stale/incomplete ``meta.worktree`` (path
    only, no isolation) being misread as a reusable isolated subject.
    """
    if not followup_parent_worktree:
        return False
    isolation = followup_parent_worktree.get("isolation")
    if isolation in (None, "off"):
        return False
    if parent_worktree_path is None:
        return False
    # Not isolated when the recorded path IS the shared source checkout.
    return not (
        source_checkout is not None
        and _paths_equal(parent_worktree_path, source_checkout)
    )


def parent_worktree_is_isolated(
    worktree: Mapping[str, Any] | None,
    *,
    source_checkout: str | Path | None = None,
) -> bool:
    """Public retained-subject predicate shared by control and setup paths."""
    raw_path = worktree.get("path") if isinstance(worktree, Mapping) else None
    path = str(raw_path) if raw_path else None
    return _parent_worktree_is_isolated(
        dict(worktree) if isinstance(worktree, Mapping) else None,
        path,
        source_checkout,
    )


def _paths_equal(a: str | Path, b: str | Path) -> bool:
    """Compare two filesystem paths by resolved form. Never raises."""
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a) == str(b)


def classify_followup_worktree(
    *,
    parent_run_dir: Path | None,
    followup_parent_worktree: dict[str, Any] | None,
    parent_has_persisted_plan: bool = False,
    source_checkout: str | Path | None = None,
) -> FollowupWorktreeDecision:
    """Classify how a follow-up should pick up its parent's change session.

    Three-way, diff-aware policy with no ownership logic, evaluated in
    order: (1) an undelivered diff is honoured strictly — reuse a dirty
    *isolated* parent worktree, block an artifact-only diff; (2) failing
    that, a durable parent plan artifact starts a *fresh* worktree as a
    plan-artifact continuation; (3) otherwise nothing is left to continue
    and the run blocks with an operator message.

    ``parent_has_persisted_plan`` is the plan-artifact-continuation signal:
    the caller asserts this run is (or is being promoted to) a from-run-plan
    continuation, so case (2) is allowed. ``source_checkout`` is the run's git
    root; it lets the classifier tell a ``worktree_isolation=off`` parent (whose
    path is the shared source checkout, or whose meta lacks an explicit isolated
    marker) from a genuine isolated worktree — an off/source-checkout parent's
    dirtiness is never treated as an undelivered diff.
    """
    parent_worktree_path: str | None = None
    if followup_parent_worktree:
        raw_path = followup_parent_worktree.get("path")
        parent_worktree_path = str(raw_path) if raw_path else None
    parent_worktree_isolated = _parent_worktree_is_isolated(
        followup_parent_worktree, parent_worktree_path, source_checkout,
    )

    sources = detect_parent_diff_sources(
        parent_run_dir,
        parent_worktree_path,
        parent_worktree_isolated=parent_worktree_isolated,
    )

    # (1) Undelivered diff — strict continuity, preserved verbatim.
    if sources.worktree_dirty:
        return FollowupWorktreeDecision(
            mode_label=f"reused parent {parent_worktree_path}",
            blocked=False,
            block_message=None,
            diff_source="worktree",
            effective_parent_worktree=followup_parent_worktree,
        )

    if sources.artifact_diff:
        block_message = (
            "follow-up parent has an undelivered diff that exists only as a "
            "diff.patch artifact (the parent worktree is clean or absent). "
            "This run does not apply diff artifacts, so it refuses to start "
            "on a clean HEAD and silently drop the parent's change. Recover "
            "by resuming the parent run."
        )
        return FollowupWorktreeDecision(
            mode_label="blocked: parent diff/worktree unavailable",
            blocked=True,
            block_message=block_message,
            diff_source="artifact",
            effective_parent_worktree=None,
        )

    # (2) No undelivered diff, but a durable parent plan artifact — continue
    # from the plan on a fresh worktree (the ``--from-run-plan`` semantics).
    # ``effective_parent_worktree=None`` makes the resolver allocate a fresh
    # worktree instead of attaching to a (missing/non-isolated) parent one.
    if parent_has_persisted_plan:
        return FollowupWorktreeDecision(
            mode_label=(
                "plan artifact continuation (fresh worktree from parent plan)"
            ),
            blocked=False,
            block_message=None,
            diff_source="plan_artifact",
            effective_parent_worktree=None,
        )

    # (3) Nothing to continue — no undelivered diff and no plan artifact.
    block_message = (
        "follow-up parent has nothing to continue from: no undelivered diff "
        "(parent worktree is clean, absent, or non-isolated, and there is no "
        "diff.patch artifact) and no persisted plan artifact "
        "(parsed_plan.json). Recover by resuming the parent run, or start a "
        "fresh run with its own plan."
    )
    return FollowupWorktreeDecision(
        mode_label="blocked: no parent diff or plan to continue",
        blocked=True,
        block_message=block_message,
        diff_source="none",
        effective_parent_worktree=None,
    )


# ── Plan-only follow-up promotion ───────────────────────────────────────────
# A plain follow-up (``--resume <parent> "<task>"``) whose parent left a
# durable plan artifact but no undelivered diff should continue from that plan
# on a fresh worktree — exactly the ``--from-run-plan`` flow — instead of
# blocking on a missing parent worktree. These helpers decide that promotion at
# request-assembly time (before profile setup) so the existing ``--from-run-plan``
# machinery (load_parsed_plan_artifact -> project_profile_for_from_run_plan ->
# state hydration) is reused rather than duplicated.


def parent_has_persisted_plan_artifact(parent_run_dir: Path | str | None) -> bool:
    """True when ``parent_run_dir`` holds a non-empty ``parsed_plan.json``.

    The durable-plan precondition for promoting a plan-only follow-up. Mirrors
    the ``_has_persisted_plan`` intent in :mod:`sdk.actions` (a run that stamped
    a plan artifact) but probes the artifact directly, since
    ``load_parsed_plan_artifact`` reads exactly this file.
    """
    if parent_run_dir is None:
        return False
    plan_path = Path(parent_run_dir) / "parsed_plan.json"
    try:
        return plan_path.is_file() and plan_path.stat().st_size > 0
    except OSError:
        return False


def load_followup_parent_worktree(
    parent_run_dir: Path | str | None,
) -> dict[str, Any] | None:
    """Read the parent run's persisted ``meta.worktree`` block, if any.

    Returns the worktree dict, or ``None`` when there is no parent dir, no
    ``meta.json``, or no worktree block. Never raises.
    """
    if parent_run_dir is None:
        return None
    with contextlib.suppress(Exception):
        parent_meta_path = Path(parent_run_dir) / "meta.json"
        parent_meta = json.loads(parent_meta_path.read_text(encoding="utf-8"))
        parent_worktree = parent_meta.get("worktree")
        if isinstance(parent_worktree, dict):
            return parent_worktree
    return None


class FollowupPlanContinuationError(ValueError):
    """A plan-only follow-up cannot continue with the selected child profile.

    Raised at the request-assembly chokepoint when the parent offers a durable
    plan to continue from but the child profile has no implement / review phases
    downstream of planning (the same contradiction the CLI ``--from-run-plan``
    guard rejects). A ``ValueError`` subclass so it shares the existing
    ``project_profile_for_from_run_plan`` failure surface.
    """


def _child_profile_from_run_plan_rejection(
    resolved_profile_name: str,
    profile_obj: Any | None,
) -> str | None:
    """Reason the child profile cannot host a plan-artifact continuation, else None.

    Mirrors the CLI ``--from-run-plan`` contradictory-profile guard via the
    shared ``CONTRADICTORY_FROM_RUN_PLAN_PROFILES`` blocklist (it catches the
    review-only recipes — delivery_audit / code_review — that
    ``project_profile_for_from_run_plan`` treats as a harmless no-op because they
    have no leading planning block). Falls back to the structural projection
    check so a custom profile that is *entirely* a planning block is rejected
    too. ``profile_obj`` (a pre-resolved projected child profile) short-circuits
    name resolution, mirroring ``setup_profile``.
    """
    from pipeline.control.from_run_plan import (
        CONTRADICTORY_FROM_RUN_PLAN_PROFILES,
        project_profile_for_from_run_plan,
    )

    reason = CONTRADICTORY_FROM_RUN_PLAN_PROFILES.get(resolved_profile_name)
    if reason is not None:
        return reason

    profile = profile_obj
    if profile is None:
        from pipeline.project.profile_setup import _resolve_v2_profile

        # ``resolved_profile_name`` is the already-inherited follow-up profile;
        # keep the env override off so an ambient ``ORCHO_PIPELINE`` cannot swap
        # the projected profile out from under the inherited name when loading
        # its v2 ``Profile`` for the contradiction check.
        profile = _resolve_v2_profile(
            profile_name=resolved_profile_name,
            allow_env_override=False,
        )
    if profile is None:
        # Unknown profile — let downstream resolution raise the canonical
        # "profile not registered" diagnostic rather than block here.
        return None
    try:
        project_profile_for_from_run_plan(profile)
    except ValueError as exc:
        return str(exc)
    return None


def resolve_followup_plan_promotion(
    *,
    resume_mode: str | None,
    explicit_from_run_plan_parent_dir: Path | None,
    followup_parent_run_dir: str | Path | None,
    profile_name: str,
    profile_obj: Any | None = None,
    project_dir: str | Path | None = None,
) -> Path | None:
    """Decide whether a plan-only follow-up promotes to from-run-plan.

    Returns the parent run dir to thread as ``from_run_plan_parent_dir`` when
    ALL hold:

    * this is a follow-up (``resume_mode == 'followup'``);
    * the caller did not pass an explicit ``--from-run-plan`` dir;
    * the parent persisted a ``parsed_plan.json``;
    * the parent has no undelivered diff (classification → ``plan_artifact``);
    * the child profile keeps implement / review phases downstream of planning.

    When the parent IS a plan-only continuation candidate (durable plan, no
    undelivered diff) but the child profile is contradictory (plan-only or
    review-only), this **raises** :class:`FollowupPlanContinuationError` before
    profile setup — the run must not proceed as a silent false continuation.
    Returns ``None`` (no promotion, normal continuity) in every other case.

    Shared by every transport (CLI, typed, MCP) at the single request-assembly
    chokepoint so the decision cannot drift. ``project_dir`` is resolved to the
    run's git root so the off-isolation / source-checkout discrimination matches
    the one ``setup_isolation`` applies.
    """
    if resume_mode != "followup":
        return None
    if explicit_from_run_plan_parent_dir is not None:
        return None
    if followup_parent_run_dir is None:
        return None

    parent_dir = Path(followup_parent_run_dir)
    if not parent_has_persisted_plan_artifact(parent_dir):
        return None

    decision = classify_followup_worktree(
        parent_run_dir=parent_dir,
        followup_parent_worktree=load_followup_parent_worktree(parent_dir),
        parent_has_persisted_plan=True,
        source_checkout=_resolve_source_checkout(project_dir),
    )
    if decision.diff_source != "plan_artifact":
        # An undelivered diff (worktree reuse / artifact block) keeps strict
        # continuity; promotion applies only when there is no undelivered diff.
        return None

    # Plan-only continuation candidate: promote a profile with downstream
    # implement / review phases; block a contradictory one loudly so it cannot
    # slip through as a false plan-artifact continuation (the diff branch above
    # already guarded undelivered-diff parents).
    from pipeline.project.profile_setup import _resolve_profile_name

    # Follow-up inherits the parent's durable profile; an ambient
    # ``ORCHO_PIPELINE`` must not silently re-target the promotion decision
    # (the resume/follow-up invariant — durable profile wins over env). Disable
    # the env override here so the inherited name drives the contradiction
    # check, never a stale A/B knob left in the environment.
    resolved_name = _resolve_profile_name(
        profile_name=profile_name,
        allow_env_override=False,
    )
    rejection = _child_profile_from_run_plan_rejection(resolved_name, profile_obj)
    if rejection is not None:
        raise FollowupPlanContinuationError(
            f"follow-up from a plan-only parent run cannot continue with "
            f"profile {resolved_name!r}: {rejection}. Pick a profile that has "
            "implement / review phases downstream of planning "
            "(feature, complex_feature, task)."
        )

    return parent_dir


def _resolve_source_checkout(project_dir: str | Path | None) -> Path | None:
    """Resolve the run's git root from ``project_dir`` (ADR 0062 nested git_dir).

    Mirrors ``resolve_isolation_inputs`` so the promotion classifier and
    ``setup_isolation`` agree on which path counts as the shared source
    checkout. Returns ``None`` when ``project_dir`` is not given.
    """
    if project_dir is None:
        return None
    from pipeline.project.project_aliases import (
        load_workspace_project_git_dir as _lwpgd,
    )

    project_path = Path(project_dir).resolve()
    rel = _lwpgd(project_path).strip()
    return project_path / rel if rel else project_path


__all__ = [
    "FollowupPlanContinuationError",
    "FollowupWorktreeDecision",
    "ParentDiffSources",
    "classify_followup_worktree",
    "detect_parent_diff_sources",
    "load_followup_parent_worktree",
    "parent_worktree_is_isolated",
    "parent_has_persisted_plan_artifact",
    "resolve_followup_plan_promotion",
]
