"""Stage C semantic built-in profiles — load + recipe-equivalence pins.

The built-in JSON (``core/_config/pipeline_profiles_v2.json``) is keyed by
the nine goal-shaped semantic work kinds plus two internal profiles
(``task`` / ``correction``). This module pins three things:

1.  All nine semantic profiles load with explicit semantic identity, and
    each one's ``default_mode`` agrees with the deterministic projection
    helper (``default_operating_mode``); ``governed`` is never a built-in
    default; the historical ``team`` / ``develop`` names never appear.
2.  The old public names (``lite`` / ``advanced`` / ``enterprise`` /
    ``plan`` / ``review``) are gone from the catalogue.
3.  Each work kind's executable recipe (ordered phase graph, worktree
    isolation, cross-gate policy, implementation_execution, validate_plan
    handoff) still matches the original recipe it was migrated from.

Recipe equivalence is checked against an EXPLICIT frozen fixture compiled
into this module (``_RECIPE_SNAPSHOTS``), NOT against any live old JSON key
(those keys were deleted in this cutover). The migration table:

    feature          ← advanced   snapshot
    refactor         ← advanced   snapshot
    small_task       ← lite        snapshot
    complex_feature  ← enterprise  snapshot
    migration        ← enterprise  snapshot
    planning         ← plan        snapshot
    research         ← plan        snapshot
    delivery_audit   ← review      snapshot
    code_review      ← review      snapshot
"""

from __future__ import annotations

import pytest

from core.infra.paths import CONFIG_DIR as _CONFIG_DIR
from pipeline.profiles.loader import load_profiles_v2
from pipeline.runtime import (
    ImplementationExecution,
    LoopStep,
    OperatingMode,
    PhaseHandoffType,
    PhaseStep,
    Profile,
    SemanticProfile,
)
from pipeline.runtime.semantic_mode_defaults import default_operating_mode

V2_PATH = _CONFIG_DIR / "pipeline_profiles_v2.json"

# The nine public semantic work kinds, in vocabulary order.
SEMANTIC_PROFILES = (
    "small_task",
    "feature",
    "complex_feature",
    "planning",
    "delivery_audit",
    "code_review",
    "research",
    "refactor",
    "migration",
)

# Public names that must NOT survive the cutover.
RETIRED_PUBLIC_NAMES = (
    "lite", "advanced", "enterprise", "plan", "review",
)


@pytest.fixture(scope="module")
def profiles() -> dict[str, Profile]:
    return load_profiles_v2(V2_PATH)


# ── Explicit frozen recipe snapshots (the migration source of truth) ─────────
#
# Each snapshot pins the observable executable shape of an original recipe.
# ``phases`` is the flattened ordered phase-name graph (loop steps expanded
# in place, so loop topology is preserved as a sequence). ``worktree`` is the
# isolation intent (``None`` == global per_run default). ``cross_gates`` maps
# each opted-in runner gate to (run, on_skip, enabled[, mode]). ``impl_exec``
# is the profile-level implementation_execution. ``validate_plan_handoff`` is
# the handoff type on the validate_plan step (or None when absent).


def _full_cycle_phases() -> tuple[str, ...]:
    return (
        "plan", "validate_plan",
        "implement",
        "review_changes", "repair_changes",
        "final_acceptance",
    )


def _enterprise_phases() -> tuple[str, ...]:
    return (
        "plan", "validate_plan",
        "implement",
        "compliance_check",
        "review_changes", "repair_changes",
        "final_acceptance",
    )


_STRICT_BOTH_GATES = {
    "contract_check": {
        "enabled": True, "run": "always", "on_skip": "block",
        "mode": "artifact_bundle",
    },
    "cross_final_acceptance": {
        "enabled": True, "run": "always", "on_skip": "block", "mode": None,
    },
}

_ADVANCED_SNAPSHOT = {
    "phases": _full_cycle_phases(),
    "worktree": None,
    "cross_gates": _STRICT_BOTH_GATES,
    "impl_exec": ImplementationExecution.SUBTASK_DAG,
    "validate_plan_handoff": PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
}

_ENTERPRISE_SNAPSHOT = {
    "phases": _enterprise_phases(),
    "worktree": None,
    "cross_gates": _STRICT_BOTH_GATES,
    "impl_exec": None,
    "validate_plan_handoff": PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
}

_LITE_SNAPSHOT = {
    "phases": ("plan", "validate_plan", "implement"),
    "worktree": "off",
    "cross_gates": {},
    "impl_exec": None,
    "validate_plan_handoff": PhaseHandoffType.HUMAN_BYPASS,
}

_PLAN_SNAPSHOT = {
    "phases": ("plan", "validate_plan"),
    "worktree": "off",
    "cross_gates": {},
    "impl_exec": None,
    "validate_plan_handoff": PhaseHandoffType.HUMAN_FEEDBACK_ALWAYS,
}

_REVIEW_SNAPSHOT = {
    "phases": ("review_changes", "final_acceptance"),
    "worktree": "off",
    "cross_gates": {},
    "impl_exec": None,
    "validate_plan_handoff": None,
}

# Work kind → the original recipe snapshot it must match.
_RECIPE_SNAPSHOTS = {
    "feature": _ADVANCED_SNAPSHOT,
    "refactor": _ADVANCED_SNAPSHOT,
    "small_task": _LITE_SNAPSHOT,
    "complex_feature": _ENTERPRISE_SNAPSHOT,
    "migration": _ENTERPRISE_SNAPSHOT,
    "planning": _PLAN_SNAPSHOT,
    "research": _PLAN_SNAPSHOT,
    "delivery_audit": _REVIEW_SNAPSHOT,
    "code_review": _REVIEW_SNAPSHOT,
}


# ── Observers: derive the observable shape from a loaded Profile ─────────────


def _flatten_phases(profile: Profile) -> tuple[str, ...]:
    out: list[str] = []
    for entry in profile.steps:
        if isinstance(entry, LoopStep):
            out.extend(inner.phase for inner in entry.steps)
        else:
            out.append(entry.phase)
    return tuple(out)


def _validate_plan_handoff(profile: Profile) -> PhaseHandoffType | None:
    for entry in profile.steps:
        steps = entry.steps if isinstance(entry, LoopStep) else (entry,)
        for inner in steps:
            if isinstance(inner, PhaseStep) and inner.phase == "validate_plan":
                return (
                    PhaseHandoffType.HUMAN_BYPASS
                    if inner.handoff is None
                    else inner.handoff.type
                )
    return None


def _cross_gate_shape(profile: Profile) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, policy in profile.cross_gates.items():
        out[name] = {
            "enabled": policy.enabled,
            "run": policy.run.value,
            "on_skip": policy.on_skip.value,
            "mode": policy.mode,
        }
    return out


# ── Catalogue membership ─────────────────────────────────────────────────────


def test_catalogue_has_nine_semantic_plus_two_internal(
    profiles: dict[str, Profile],
) -> None:
    assert set(profiles) == set(SEMANTIC_PROFILES) | {"task", "correction"}


def test_retired_public_names_absent(profiles: dict[str, Profile]) -> None:
    for name in RETIRED_PUBLIC_NAMES:
        assert name not in profiles, f"retired public name {name!r} still shipped"


def test_internal_profiles_have_no_semantic_profile(
    profiles: dict[str, Profile],
) -> None:
    for name in ("task", "correction"):
        p = profiles[name]
        assert p.internal is True
        assert p.recipe_kind == "internal"
        assert p.semantic_profile is None


# ── Semantic identity + default-mode projection ──────────────────────────────


@pytest.mark.parametrize("name", SEMANTIC_PROFILES)
def test_semantic_profile_matches_key(
    name: str, profiles: dict[str, Profile],
) -> None:
    p = profiles[name]
    assert p.semantic_profile is SemanticProfile(name)


@pytest.mark.parametrize("name", SEMANTIC_PROFILES)
def test_default_mode_agrees_with_projection_helper(
    name: str, profiles: dict[str, Profile],
) -> None:
    p = profiles[name]
    assert p.default_mode is not None
    assert p.default_mode == default_operating_mode(p.semantic_profile)


@pytest.mark.parametrize("name", SEMANTIC_PROFILES)
def test_recipe_kind_valid(name: str, profiles: dict[str, Profile]) -> None:
    assert profiles[name].recipe_kind in {"full_cycle", "focused"}


def test_no_governed_default_anywhere(profiles: dict[str, Profile]) -> None:
    for p in profiles.values():
        assert p.default_mode is not OperatingMode.GOVERNED
        # Only fast / pro appear as live built-in defaults (or None for
        # the internal profiles that carry no semantic default).
        assert p.default_mode in (None, OperatingMode.FAST, OperatingMode.PRO)


def test_no_team_or_develop_values(profiles: dict[str, Profile]) -> None:
    # Regression guard: historical drafted names never leak into a live
    # built-in value (mode or semantic profile). 'team' / 'develop' are not
    # even enum members, so no built-in can carry them.
    live_mode_values = {m.value for m in OperatingMode}
    live_semantic_values = {s.value for s in SemanticProfile}
    assert "team" not in live_mode_values
    assert "develop" not in live_semantic_values and "team" not in live_semantic_values
    for p in profiles.values():
        if p.default_mode is not None:
            assert p.default_mode.value in live_mode_values
        if p.semantic_profile is not None:
            assert p.semantic_profile.value not in {"develop", "team"}


# ── Recipe equivalence against frozen snapshots ──────────────────────────────


@pytest.mark.parametrize("name", sorted(_RECIPE_SNAPSHOTS))
def test_recipe_equivalent_to_migration_snapshot(
    name: str, profiles: dict[str, Profile],
) -> None:
    snapshot = _RECIPE_SNAPSHOTS[name]
    p = profiles[name]

    assert _flatten_phases(p) == snapshot["phases"], (
        f"{name}: phase graph drifted from its migration snapshot"
    )
    assert p.worktree_isolation == snapshot["worktree"], (
        f"{name}: worktree_isolation drifted"
    )
    assert _cross_gate_shape(p) == snapshot["cross_gates"], (
        f"{name}: cross_gates policy drifted"
    )
    assert p.implementation_execution == snapshot["impl_exec"], (
        f"{name}: implementation_execution drifted"
    )
    assert _validate_plan_handoff(p) == snapshot["validate_plan_handoff"], (
        f"{name}: validate_plan handoff type drifted"
    )
