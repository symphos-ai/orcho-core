"""profile-name cleanup regression tests."""

from __future__ import annotations

import pytest

from pipeline.project.profile_dispatch import (
    hypothesis_attempts_for_step as _hypothesis_attempts_for_step,
    hypothesis_format_for_step as _hypothesis_format_for_step,
    plan_hypothesis_step as _plan_hypothesis_step,
    resolve_mode_gates as _resolve_mode_gates,
)
from pipeline.project.profile_setup import _resolve_change_handoff
from pipeline.runtime import (
    ChangeHandoffMode,
    HypothesisPrelude,
    PhaseStep,
    Profile,
    ProfileKind,
    PromptSpec,
)


def test_custom_build_only_profile_does_not_enable_plan_gate() -> None:
    """Custom profile shape, not name, decides whether hypothesis/plan run."""
    profile = Profile(
        name="parallel_review",
        kind=ProfileKind.CUSTOM,
        steps=(PhaseStep("implement"), PhaseStep("final_acceptance")),
    )

    do_plan, do_build, do_review, max_rounds = _resolve_mode_gates(
        profile, max_rounds=2,
    )

    assert do_plan is False
    assert do_build is True
    assert do_review is True
    assert max_rounds == 2


def test_plan_step_hypothesis_is_profile_owned() -> None:
    profile = Profile(
        name="custom",
        kind=ProfileKind.CUSTOM,
        steps=(
            PhaseStep(
                "plan",
                prompt=PromptSpec(
                    role="systems_architect",
                    task="plan",
                    format="terse",
                ),
                hypothesis=HypothesisPrelude(attempts=1, format="compact"),
            ),
            PhaseStep("implement"),
        ),
    )

    step = _plan_hypothesis_step(profile, override_enabled=None)

    assert step is profile.steps[0]
    assert _hypothesis_attempts_for_step(step) == 1
    assert _hypothesis_format_for_step(step) == "compact"


def test_plan_step_hypothesis_zero_disables_prelude() -> None:
    profile = Profile(
        name="custom",
        kind=ProfileKind.CUSTOM,
        steps=(
            PhaseStep("plan", hypothesis=HypothesisPrelude(attempts=0)),
            PhaseStep("implement"),
        ),
    )

    assert _plan_hypothesis_step(profile, override_enabled=None) is None


def test_cli_override_can_force_profile_hypothesis_on() -> None:
    profile = Profile(
        name="custom",
        kind=ProfileKind.CUSTOM,
        steps=(PhaseStep("plan"),),
    )

    assert _plan_hypothesis_step(profile, override_enabled=True) is profile.steps[0]


def test_hypothesis_format_inherits_plan_format_when_omitted() -> None:
    profile = Profile(
        name="custom",
        kind=ProfileKind.CUSTOM,
        steps=(
            PhaseStep(
                "plan",
                prompt=PromptSpec(
                    role="systems_architect",
                    task="plan",
                    format="terse",
                ),
                hypothesis=HypothesisPrelude(attempts=1),
            ),
        ),
    )

    step = _plan_hypothesis_step(profile, override_enabled=None)

    assert _hypothesis_format_for_step(step) == "terse"


def test_scoped_review_variant_clamps_runtime_rounds() -> None:
    profile = Profile(
        name="review",
        kind=ProfileKind.SCOPED,
        variant="review",
        steps=(PhaseStep("review_changes"), PhaseStep("final_acceptance")),
    )

    assert _resolve_mode_gates(profile, max_rounds=3) == (
        False, False, True, 0,
    )


def test_profile_change_handoff_wins_over_config_default() -> None:
    profile = Profile(
        name="review",
        kind=ProfileKind.CUSTOM,
        steps=(PhaseStep("review_changes"),),
        change_handoff=ChangeHandoffMode.COMMIT,
    )

    assert _resolve_change_handoff(profile) == "commit"


def test_invalid_config_change_handoff_fails_at_run_start(monkeypatch) -> None:
    from core.infra.config import AppConfig

    cfg = AppConfig.load()
    bad = cfg.__class__(
        phases=cfg.phases,
        timeouts=cfg.timeouts,
        session=cfg.session,
        codemap=cfg.codemap,
        hypothesis=cfg.hypothesis,
        language=cfg.language,
        artifacts=cfg.artifacts,
        pipeline={"change_handoff": "branch"},
    )
    monkeypatch.setattr(AppConfig, "load", classmethod(lambda _cls: bad))
    profile = Profile(
        name="review",
        kind=ProfileKind.CUSTOM,
        steps=(PhaseStep("review_changes"),),
    )

    with pytest.raises(ValueError, match="Invalid change_handoff"):
        _resolve_change_handoff(profile)
