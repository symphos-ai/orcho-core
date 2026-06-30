"""The internal ``correction`` profile shape (ADR 0085, T2).

Loads the real shipped ``pipeline_profiles_v2.json`` and pins the
correction profile: internal flag, phase order (correction_triage first,
no plan / validate_plan), and that the six operator-facing profiles are
unchanged (names, internal=False, opening phases).
"""

from __future__ import annotations

from core.infra.paths import CONFIG_DIR
from pipeline.profiles.loader import (
    load_profiles_v2,
    load_profiles_v2_with_plugins,
)
from pipeline.project.profile_dispatch import profile_contains_phase
from pipeline.runtime import LoopStep, PhaseStep

_V2_PATH = CONFIG_DIR / "pipeline_profiles_v2.json"


def _flat_phases(profile) -> list[str]:
    out: list[str] = []
    for entry in profile.steps:
        if isinstance(entry, LoopStep):
            out.extend(s.phase for s in entry.steps)
        elif isinstance(entry, PhaseStep):
            out.append(entry.phase)
    return out


def _profiles() -> dict:
    return load_profiles_v2_with_plugins(_V2_PATH)


# ── correction profile ─────────────────────────────────────────────────


def test_correction_profile_loads_without_validation_error() -> None:
    profiles = _profiles()
    assert "correction" in profiles
    assert profiles["correction"].internal is True
    assert profiles["correction"].kind.value == "custom"
    assert profiles["correction"].variant == "correction"


def test_correction_first_phase_is_triage() -> None:
    correction = _profiles()["correction"]
    assert isinstance(correction.steps[0], PhaseStep)
    assert correction.steps[0].phase == "correction_triage"


def test_correction_phase_order() -> None:
    correction = _profiles()["correction"]
    assert _flat_phases(correction) == [
        "correction_triage",
        "implement",
        "review_changes",
        "repair_changes",
        "final_acceptance",
    ]


def test_correction_omits_plan_and_validate_plan() -> None:
    correction = _profiles()["correction"]
    assert profile_contains_phase(correction, "plan") is False
    assert profile_contains_phase(correction, "validate_plan") is False


def test_correction_declares_no_cross_gates() -> None:
    # Never projected to cross — no cross_gates block.
    correction = _profiles()["correction"]
    assert dict(correction.cross_gates) == {}


def test_correction_triage_prompt_uses_release_manager_terse() -> None:
    correction = _profiles()["correction"]
    triage = correction.steps[0]
    assert triage.prompt is not None
    assert triage.prompt.role == "release_manager"
    assert triage.prompt.task == "correction_triage"
    assert triage.prompt.format == "terse"


# ── existing profiles unchanged ────────────────────────────────────────


def test_internal_defaults_false_for_semantic_profiles() -> None:
    profiles = load_profiles_v2(_V2_PATH)
    # The nine semantic work kinds are operator-facing (internal=False);
    # task / correction are the internal profiles (internal=True).
    for name in (
        "small_task", "feature", "complex_feature", "planning",
        "delivery_audit", "code_review", "research", "refactor", "migration",
    ):
        assert profiles[name].internal is False, name
    assert profiles["task"].internal is True
    assert profiles["correction"].internal is True


def test_shipped_operator_profiles_keep_their_opening_phase() -> None:
    profiles = load_profiles_v2(_V2_PATH)
    # full-cycle work kinds open on the plan loop; the plan-only recipes
    # (planning / research) open on plan; the review recipes
    # (delivery_audit / code_review) open on review_changes; the internal
    # task opens on implement. None of the migrated recipes changed shape.
    for name in (
        "small_task", "feature", "complex_feature", "planning",
        "research", "refactor", "migration",
    ):
        assert _flat_phases(profiles[name])[0] == "plan", name
    assert _flat_phases(profiles["delivery_audit"])[0] == "review_changes"
    assert _flat_phases(profiles["code_review"])[0] == "review_changes"
    assert _flat_phases(profiles["task"])[0] == "implement"


def test_profile_registry_has_nine_semantic_plus_two_internal() -> None:
    assert set(_profiles()) == {
        "small_task", "feature", "complex_feature", "planning",
        "delivery_audit", "code_review", "research", "refactor", "migration",
        "task", "correction",
    }
