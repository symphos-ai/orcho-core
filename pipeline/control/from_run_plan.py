"""Control-flow helpers for ``--from-run-plan`` follow-up runs.

A ``--from-run-plan`` run is a new run derived from a parent run's
:class:`~pipeline.plan_parser.ParsedPlan`. The parent's plan is loaded
via :mod:`pipeline.plan_artifacts` and hydrated into the child run's
state before the pipeline starts. The selected profile is then
*projected* so the planning block (which would re-produce a plan the
child already has) is skipped.

This module owns the projection rule. The over-run-plan follow-up and
change-semantics planning record (internal; Phase 1 §5 "Minimal Profile
Projection") describes the narrow MVP rule:

    when --from-run-plan is present, remove the leading planning block
    from the selected profile, the planning block includes plan and
    validate_plan phases, including the common plan/validate loop, all
    remaining phases run normally.

Two leading-block shapes are recognised:

* A :class:`LoopStep` at index 0 whose inner phase names are a subset
  of ``{plan, validate_plan}`` — this is the ``plan ↔ validate_plan``
  retry loop used by the shipped ``feature`` / ``complex_feature``
  profiles. Drop the loop step entirely.
* A standalone :class:`PhaseStep` at index 0 with ``phase == "plan"``
  (followed optionally by a ``validate_plan`` PhaseStep). Drop both
  in the order they appear.

If neither shape matches the projection is a no-op — a profile without
a leading planning block (e.g. ``task``) runs unchanged. This is
idempotent: applying the projection twice yields the same result.

The projection produces a synthetic profile named
``"<original>#from_run_plan"`` so the surface metadata
(``meta.projected_profile``) makes the derivation visible in dashboards
and evidence bundles without changing the requested profile name on
``meta.profile``.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from pipeline.runtime.profile import LoopStep, Profile
from pipeline.runtime.steps import PhaseStep

# Phase names that constitute the leading planning block. Kept as a
# module constant so a future profile that introduces a new planning-
# adjacent phase (e.g. cross-plan validation) can extend the set
# without churning the projection's body.
PLANNING_PHASES: frozenset[str] = frozenset({"plan", "validate_plan"})

# Suffix applied to the projected profile's ``name`` so the synthetic
# in-memory profile is distinguishable from the original in evidence
# and dashboards. The orchestrator surfaces this as
# ``meta.projected_profile``; ``meta.profile`` still carries the
# requested name.
PROJECTED_PROFILE_SUFFIX: str = "#from_run_plan"

# Semantic work-kind profiles whose recipe contradicts a from-run-plan
# continuation: the plan-only recipe (planning / research) has no phases after
# the planning block, and the review-only recipe (delivery_audit / code_review)
# has no planning or implementation phases for the inherited plan to feed into.
# Shared by the CLI ``--from-run-plan`` guard and the plan-only follow-up
# promotion so the two surfaces cannot drift. Keyed by profile name → reason.
CONTRADICTORY_FROM_RUN_PLAN_PROFILES: dict[str, str] = {
    "planning": (
        "the planning profile produces a plan and pauses for review — "
        "it has no phases after the planning block to run on top "
        "of the inherited plan"
    ),
    "research": (
        "the research profile produces a plan and pauses for review — "
        "it has no phases after the planning block to run on top "
        "of the inherited plan"
    ),
    "delivery_audit": (
        "the delivery_audit profile reviews the working tree and has no "
        "planning or implementation phases — there is nothing for "
        "the inherited plan to feed into"
    ),
    "code_review": (
        "the code_review profile reviews the working tree and has no "
        "planning or implementation phases — there is nothing for "
        "the inherited plan to feed into"
    ),
}


@dataclass(frozen=True)
class ProfileProjectionResult:
    """Outcome of projecting a profile for ``--from-run-plan``.

    Carries both the projected profile (for the runtime) and a list
    of stripped phase names (for CLI / evidence diagnostics). The
    stripped list is empty when the projection was a no-op.
    """

    profile: Profile
    stripped_phases: tuple[str, ...]
    is_noop: bool


def project_profile_for_from_run_plan(profile: Profile) -> ProfileProjectionResult:
    """Strip the leading planning block from *profile*.

    Returns the projected profile + diagnostic info. See the module
    docstring for the exact rule (loop-step shape or standalone
    ``plan`` PhaseStep). When the profile has no leading planning
    block, returns *profile* unchanged with ``is_noop=True``.

    The result is idempotent: ``project(project(p)).profile`` has the
    same steps as ``project(p).profile``.
    """
    steps = tuple(profile.steps)
    if not steps:
        # Empty profiles never had a planning block to strip.
        return ProfileProjectionResult(
            profile=profile, stripped_phases=(), is_noop=True,
        )

    stripped: list[str] = []
    new_steps: list = list(steps)

    head = new_steps[0]

    # Shape A: leading LoopStep whose inner phases are a subset of
    # the planning block (i.e. plan ↔ validate_plan retry loop).
    if isinstance(head, LoopStep):
        inner_names = set(head.inner_phases)
        if inner_names and inner_names <= PLANNING_PHASES:
            new_steps.pop(0)
            stripped.extend(head.inner_phases)

    # Shape B: standalone leading planning PhaseSteps. Even if Shape
    # A consumed a leading loop, a following standalone validate_plan
    # is part of the same logical planning block and should also go.
    while new_steps:
        next_step = new_steps[0]
        if (
            isinstance(next_step, PhaseStep)
            and next_step.phase in PLANNING_PHASES
        ):
            new_steps.pop(0)
            stripped.append(next_step.phase)
        else:
            break

    if not stripped:
        return ProfileProjectionResult(
            profile=profile, stripped_phases=(), is_noop=True,
        )

    if not new_steps:
        # Pathological: profile consisted entirely of a planning block
        # with nothing after it. Refuse rather than ship a profile
        # with zero steps (Profile.__post_init__ would reject it anyway,
        # but the error here is more actionable).
        raise ValueError(
            f"profile {profile.name!r} consists entirely of planning "
            f"phases {sorted(set(stripped))} — there is nothing left "
            "to run after --from-run-plan strips the planning block. "
            "Pick a profile that has an implement / review block "
            "downstream of plan (e.g. feature, complex_feature, task).",
        )

    projected = dataclasses.replace(
        profile,
        name=f"{profile.name}{PROJECTED_PROFILE_SUFFIX}",
        steps=tuple(new_steps),
    )
    return ProfileProjectionResult(
        profile=projected,
        stripped_phases=tuple(stripped),
        is_noop=False,
    )


__all__ = [
    "CONTRADICTORY_FROM_RUN_PLAN_PROFILES",
    "PLANNING_PHASES",
    "PROJECTED_PROFILE_SUFFIX",
    "ProfileProjectionResult",
    "project_profile_for_from_run_plan",
]
