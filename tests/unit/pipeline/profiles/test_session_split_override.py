from __future__ import annotations

from pipeline.profiles.session_split_override import apply_session_split_overrides
from pipeline.runtime.profile import ExecutionPolicy, LoopStep, Profile
from pipeline.runtime.steps import PhaseStep


def _step(
    phase: str,
    split: str | None = None,
    *,
    continuity: str | None = None,
) -> PhaseStep:
    return PhaseStep(
        phase=phase,
        execution_policy=ExecutionPolicy(
            mode="linear",
            session_split=split,
            session_continuity=continuity,
        ),
    )


def test_applies_override_to_top_level_phase() -> None:
    profile = Profile(
        name="demo",
        steps=(_step("plan", "common"), _step("implement", "per_phase")),
    )

    updated = apply_session_split_overrides(profile, {"implement": "common"})

    implement = updated.steps[1]
    assert isinstance(implement, PhaseStep)
    assert implement.execution_policy.session_split == "common"
    plan = updated.steps[0]
    assert isinstance(plan, PhaseStep)
    assert plan.execution_policy.session_split == "common"


def test_applies_override_inside_loop() -> None:
    profile = Profile(
        name="demo",
        steps=(
            LoopStep(
                steps=(
                    _step("plan", "common"),
                    _step("validate_plan", "per_phase"),
                ),
                until="validate_plan.approved",
            ),
            _step("implement", "per_phase"),
        ),
    )

    updated = apply_session_split_overrides(
        profile,
        {"validate_plan": "common", "implement": "common"},
    )

    loop = updated.steps[0]
    assert isinstance(loop, LoopStep)
    assert loop.steps[1].execution_policy.session_split == "common"
    implement = updated.steps[1]
    assert isinstance(implement, PhaseStep)
    assert implement.execution_policy.session_split == "common"


def test_override_preserves_session_continuity_top_level() -> None:
    # A split override must patch only the split axis; the orthogonal
    # session_continuity declaration on the same execution policy must survive,
    # otherwise the phase-role resolver would later fail loudly on a profile
    # that was valid before the override (F1 regression guard).
    profile = Profile(
        name="demo",
        steps=(
            _step("implement", "per_phase", continuity="same_zone_continue"),
        ),
    )

    updated = apply_session_split_overrides(profile, {"implement": "common"})

    implement = updated.steps[0]
    assert isinstance(implement, PhaseStep)
    assert implement.execution_policy.session_split == "common"
    assert implement.execution_policy.session_continuity == "same_zone_continue"


def test_override_preserves_session_continuity_inside_loop() -> None:
    profile = Profile(
        name="demo",
        steps=(
            LoopStep(
                steps=(
                    _step("plan", "common", continuity="loop_continue"),
                    _step(
                        "validate_plan", "per_phase", continuity="loop_continue"
                    ),
                ),
                until="validate_plan.approved",
            ),
            _step("implement", "per_phase", continuity="same_zone_continue"),
        ),
    )

    updated = apply_session_split_overrides(
        profile,
        {"validate_plan": "common", "implement": "common"},
    )

    loop = updated.steps[0]
    assert isinstance(loop, LoopStep)
    validate = loop.steps[1]
    assert validate.execution_policy.session_split == "common"
    assert validate.execution_policy.session_continuity == "loop_continue"
    # The untouched loop step keeps both axes.
    plan = loop.steps[0]
    assert plan.execution_policy.session_split == "common"
    assert plan.execution_policy.session_continuity == "loop_continue"
    implement = updated.steps[1]
    assert isinstance(implement, PhaseStep)
    assert implement.execution_policy.session_split == "common"
    assert implement.execution_policy.session_continuity == "same_zone_continue"


def test_missing_phase_is_ignored_for_scoped_profile() -> None:
    profile = Profile(name="review", steps=(_step("review_changes", "per_phase"),))

    updated = apply_session_split_overrides(
        profile,
        {"implement": "common", "review_changes": "common"},
    )

    review = updated.steps[0]
    assert isinstance(review, PhaseStep)
    assert review.execution_policy.session_split == "common"
