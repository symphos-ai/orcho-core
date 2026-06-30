"""Unit tests for :func:`pipeline.project.profile_setup._profile_phase_names`.

The helper backs the Agents-block filter in ``print_pipeline_header``
so a profile that doesn't run REVIEW / REPAIR / FINAL_ACCEPTANCE
doesn't broadcast those agent rows. Tests assert the helper sees both
top-level ``PhaseStep`` entries and the inner steps of a
``LoopStep``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline.profiles.loader import ProfileLoadError
from pipeline.project import profile_setup
from pipeline.project.profile_setup import _profile_phase_names, _resolve_v2_profile
from pipeline.runtime.profile import LoopStep
from pipeline.runtime.steps import PhaseStep


def test_collects_top_level_phase_step_names():
    profile = SimpleNamespace(
        steps=(PhaseStep(phase="plan"), PhaseStep(phase="implement")),
    )
    assert _profile_phase_names(profile) == {"plan", "implement"}


def test_collects_inner_steps_of_loop_step():
    profile = SimpleNamespace(
        steps=(
            LoopStep(
                steps=(PhaseStep(phase="plan"), PhaseStep(phase="validate_plan")),
                until="validate_plan.approved",
                max_rounds=2,
            ),
            PhaseStep(phase="implement"),
        ),
    )
    assert _profile_phase_names(profile) == {"plan", "validate_plan", "implement"}


def test_unknown_step_types_are_skipped():
    # The helper recognises ``PhaseStep`` and ``LoopStep``; any other
    # object in ``steps`` is silently ignored. Profile validation rejects
    # such shapes upstream, so this just pins the defensive behaviour
    # for direct unit-test calls.
    profile = SimpleNamespace(
        steps=(PhaseStep(phase="plan"), object()),
    )
    assert _profile_phase_names(profile) == {"plan"}


def test_resolve_v2_profile_surfaces_profile_load_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    """Malformed profile config must not masquerade as a missing profile."""

    def _raise(_path):
        raise ProfileLoadError("profiles_v2 overlay: profile 'lite' is broken")

    monkeypatch.setattr(profile_setup, "load_profiles_v2_with_plugins", _raise)

    with pytest.raises(ProfileLoadError, match="profile 'lite' is broken"):
        _resolve_v2_profile(profile_name="correction")
