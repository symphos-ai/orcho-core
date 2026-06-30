"""Profile projection for ``--from-run-plan`` follow-up runs.

Pins the projection rule from the over-run follow-up plan
(the over-run-plan follow-up and change-semantics planning record (internal) Phase 1 §5): when a child run inherits its plan from a
parent, the selected profile must drop the leading planning block so
the child does not re-produce the plan it already has.

Two shapes recognised:

* leading :class:`LoopStep` whose inner phase names are a subset of
  ``{plan, validate_plan}`` (the shipped ``advanced`` /
  ``enterprise`` plan-loop);
* leading standalone :class:`PhaseStep` with ``phase="plan"``,
  optionally followed by a standalone ``validate_plan`` PhaseStep.

If neither shape matches the projection is a no-op (e.g. ``task``
profile, which has no planning block).
"""
from __future__ import annotations

import re

import pytest

from pipeline.control.from_run_plan import (
    PROJECTED_PROFILE_SUFFIX,
    project_profile_for_from_run_plan,
)
from pipeline.runtime.profile import LoopStep, Profile
from pipeline.runtime.roles import ProfileKind
from pipeline.runtime.steps import PhaseStep


def _step(phase: str) -> PhaseStep:
    return PhaseStep(phase=phase)


def _plan_loop(*, max_rounds: int = 2) -> LoopStep:
    """Mirror of the shipped plan-loop shape: a LoopStep wrapping
    [plan, validate_plan] with an ``until: validate_plan.approved``
    predicate."""
    return LoopStep(
        steps=(_step("plan"), _step("validate_plan")),
        until="validate_plan.approved",
        max_rounds=max_rounds,
    )


def _advanced_like_profile() -> Profile:
    """Shape mirrors the shipped ``advanced`` profile: plan-loop
    head, implement / review / repair / final acceptance body."""
    return Profile(
        name="advanced",
        kind=ProfileKind.FULL_CYCLE,
        variant="advanced",
        steps=(
            _plan_loop(),
            _step("implement"),
            _step("review_changes"),
            _step("repair_changes"),
            _step("final_acceptance"),
        ),
    )


def _flat_planning_profile() -> Profile:
    """Standalone leading plan + validate_plan PhaseSteps (Shape B)."""
    return Profile(
        name="flat",
        kind=ProfileKind.CUSTOM,
        steps=(
            _step("plan"),
            _step("validate_plan"),
            _step("implement"),
            _step("final_acceptance"),
        ),
    )


def _task_like_profile() -> Profile:
    """No leading planning block — projection must be a no-op."""
    return Profile(
        name="task",
        kind=ProfileKind.SCOPED,
        variant="task",
        steps=(
            _step("implement"),
            _step("final_acceptance"),
        ),
    )


class TestProjectLoopShape:
    """Shape A: leading LoopStep wrapping the plan-loop."""

    def test_strips_plan_loop_step(self) -> None:
        result = project_profile_for_from_run_plan(_advanced_like_profile())
        assert not result.is_noop
        # The projected profile starts at the first non-planning step.
        first = result.profile.steps[0]
        assert isinstance(first, PhaseStep)
        assert first.phase == "implement"

    def test_keeps_all_post_planning_steps_intact(self) -> None:
        original = _advanced_like_profile()
        result = project_profile_for_from_run_plan(original)
        # Everything after the dropped loop survives byte-equal.
        assert result.profile.steps == original.steps[1:]

    def test_stripped_phases_lists_loop_inner(self) -> None:
        result = project_profile_for_from_run_plan(_advanced_like_profile())
        assert result.stripped_phases == ("plan", "validate_plan")

    def test_projected_profile_name_carries_suffix(self) -> None:
        result = project_profile_for_from_run_plan(_advanced_like_profile())
        assert result.profile.name == f"advanced{PROJECTED_PROFILE_SUFFIX}"

    def test_does_not_strip_loop_with_unrelated_inner_phase(self) -> None:
        """A leading LoopStep with an inner phase outside the planning
        set (e.g. ``implement`` in some custom profile) is NOT a
        planning block — projection must leave it alone."""
        profile = Profile(
            name="custom",
            kind=ProfileKind.CUSTOM,
            steps=(
                LoopStep(
                    steps=(_step("implement"), _step("review_changes")),
                    until="review_changes.approved",
                ),
                _step("final_acceptance"),
            ),
        )
        result = project_profile_for_from_run_plan(profile)
        assert result.is_noop
        assert result.profile is profile


class TestProjectFlatShape:
    """Shape B: standalone leading plan + validate_plan PhaseSteps."""

    def test_strips_leading_plan_and_validate_plan(self) -> None:
        result = project_profile_for_from_run_plan(_flat_planning_profile())
        assert not result.is_noop
        assert result.stripped_phases == ("plan", "validate_plan")
        assert [s.phase for s in result.profile.steps] == [
            "implement", "final_acceptance",
        ]

    def test_strips_only_leading_plan_when_no_validate(self) -> None:
        profile = Profile(
            name="plan_only_head",
            kind=ProfileKind.CUSTOM,
            steps=(
                _step("plan"),
                _step("implement"),
                _step("final_acceptance"),
            ),
        )
        result = project_profile_for_from_run_plan(profile)
        assert result.stripped_phases == ("plan",)
        assert [s.phase for s in result.profile.steps] == [
            "implement", "final_acceptance",
        ]

    def test_does_not_strip_non_leading_planning_step(self) -> None:
        """A ``plan`` PhaseStep buried mid-profile is not a leading
        planning block — projection only consumes the head. (We are
        not aware of any shipped profile with this shape, but the
        rule must be exact.)"""
        profile = Profile(
            name="mid_plan",
            kind=ProfileKind.CUSTOM,
            steps=(
                _step("implement"),
                _step("plan"),
                _step("final_acceptance"),
            ),
        )
        result = project_profile_for_from_run_plan(profile)
        assert result.is_noop


class TestProjectNoop:
    """Profiles without a leading planning block must be untouched."""

    def test_task_profile_unchanged(self) -> None:
        original = _task_like_profile()
        result = project_profile_for_from_run_plan(original)
        assert result.is_noop
        assert result.stripped_phases == ()
        assert result.profile is original

    def test_idempotent_double_application(self) -> None:
        """Projecting an already-projected profile is a no-op."""
        once = project_profile_for_from_run_plan(_advanced_like_profile())
        twice = project_profile_for_from_run_plan(once.profile)
        assert twice.is_noop
        assert twice.profile is once.profile


class TestProjectPathological:
    """Edge cases that should fail loudly rather than ship an empty
    profile."""

    def test_planning_only_profile_raises(self) -> None:
        """Profile consisting entirely of a planning block has nothing
        left after projection — refuse rather than ship zero-step
        profile."""
        profile = Profile(
            name="plan_only",
            kind=ProfileKind.SCOPED,
            variant="plan",
            steps=(_plan_loop(),),
        )
        with pytest.raises(ValueError) as exc:
            project_profile_for_from_run_plan(profile)
        msg = str(exc.value)
        assert "consists entirely of planning phases" in msg
        # Message must hint at the recovery path using semantic work-kind
        # names (feature / complex_feature / task).
        assert "feature" in msg or "task" in msg

    def test_planning_only_diagnostic_uses_semantic_profile_names(self) -> None:
        """Regression: the recovery hint names semantic work kinds and never
        a retired legacy profile name (advanced / enterprise / lite)."""
        profile = Profile(
            name="plan_only",
            kind=ProfileKind.SCOPED,
            variant="plan",
            steps=(_plan_loop(),),
        )
        with pytest.raises(ValueError) as exc:
            project_profile_for_from_run_plan(profile)
        msg = str(exc.value).lower()
        words = set(re.findall(r"[a-z_]+", msg))
        assert words.isdisjoint({"advanced", "enterprise", "lite"})
        # Points the operator at a real semantic profile with downstream phases.
        assert {"feature", "complex_feature", "task"} & words
