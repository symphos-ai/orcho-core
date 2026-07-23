"""Default-mode → work_mode projection (T6).

Pins the focused projection point in ``pipeline.project.runtime_setup``:

* ``project_effective_work_mode`` — pure resolution of the run's effective
  ``work_mode`` from (CLI override) → (project/contract work_mode) →
  (profile ``default_mode``).
* ``apply_default_mode_projection`` — applies that to a built
  ``VerificationContract`` so the effective mode is observable downstream
  via the assembled ``SelectionContext``.

The built-in profiles are loaded for real (the shipped semantic catalogue),
so ``feature → pro`` / ``complex_feature → pro`` are checked against the
actual ``default_mode`` metadata, not a hand-mirrored table. Local config is
disabled by the suite conftest, so the load is hermetic.
"""

from __future__ import annotations

import pytest

from core.infra.paths import CONFIG_DIR as _CONFIG_DIR
from pipeline.profiles.loader import load_profiles_v2
from pipeline.project.runtime_setup import (
    apply_default_mode_projection,
    project_effective_work_mode,
)
from pipeline.runtime import OperatingMode
from pipeline.verification_contract import WORK_MODES, VerificationContract
from pipeline.verification_selection import selection_context_from_extras

V2_PATH = _CONFIG_DIR / "pipeline_profiles_v2.json"

SEMANTIC_PROFILES = (
    "small_task", "feature", "complex_feature", "planning", "delivery_audit",
    "code_review", "research", "refactor", "migration",
)


@pytest.fixture(scope="module")
def profiles() -> dict:
    return load_profiles_v2(V2_PATH)


def _contract(work_mode: str = "") -> VerificationContract:
    """Minimal real contract carrying just a ``work_mode`` (other fields
    empty). Real type so ``dataclasses.replace`` + ``SelectionContext``
    assembly exercise the production path."""
    return VerificationContract(
        dependency_repos={},
        verification_envs={},
        commands={},
        schedule=(),
        default_env="",
        required=(),
        work_mode=work_mode,
    )


# ── pure projection helper ───────────────────────────────────────────────────


def test_feature_defaults_to_pro(profiles: dict) -> None:
    feature = profiles["feature"]
    assert project_effective_work_mode(profile=feature) == "pro"


def test_complex_feature_defaults_to_pro(profiles: dict) -> None:
    complex_feature = profiles["complex_feature"]
    assert project_effective_work_mode(profile=complex_feature) == "pro"


@pytest.mark.parametrize("override", ["fast", "pro", "governed"])
def test_cli_override_wins_over_default(profiles: dict, override: str) -> None:
    feature = profiles["feature"]  # default_mode == pro
    assert (
        project_effective_work_mode(profile=feature, cli_mode=override)
        == override
    )


def test_explicit_contract_work_mode_not_overridden(profiles: dict) -> None:
    # An explicit project/contract work_mode (here 'governed') must survive —
    # the profile default (pro) does not overwrite it.
    feature = profiles["feature"]
    assert (
        project_effective_work_mode(
            profile=feature, contract_work_mode="governed",
        )
        == "governed"
    )


def test_cli_override_beats_contract_work_mode(profiles: dict) -> None:
    feature = profiles["feature"]
    assert (
        project_effective_work_mode(
            profile=feature, cli_mode="fast", contract_work_mode="pro",
        )
        == "fast"
    )


def test_no_default_mode_yields_empty() -> None:
    class _Plugin:  # plugin/custom profile with no semantic default_mode
        default_mode = None

    assert project_effective_work_mode(profile=_Plugin()) == ""


@pytest.mark.parametrize("name", SEMANTIC_PROFILES)
def test_no_governed_default(profiles: dict, name: str) -> None:
    # governed is opt-in only: it is never the projected default for any
    # built-in work kind.
    assert project_effective_work_mode(profile=profiles[name]) != "governed"


def test_projection_result_always_in_work_modes(profiles: dict) -> None:
    for name in SEMANTIC_PROFILES:
        assert project_effective_work_mode(profile=profiles[name]) in WORK_MODES


# ── contract application + SelectionContext observability ────────────────────


def test_apply_fills_unset_work_mode_from_default(profiles: dict) -> None:
    feature = profiles["feature"]
    projected = apply_default_mode_projection(_contract(""), profile=feature)
    assert projected.work_mode == "pro"
    # Observable through the assembled selection context.
    ctx = selection_context_from_extras({}, projected)
    assert ctx.work_mode == "pro"


def test_apply_preserves_explicit_contract_work_mode(profiles: dict) -> None:
    feature = profiles["feature"]  # default fast
    base = _contract("pro")
    projected = apply_default_mode_projection(base, profile=feature)
    # Unchanged object (no needless replace) and explicit value preserved.
    assert projected is base
    assert selection_context_from_extras({}, projected).work_mode == "pro"


def test_apply_cli_override_wins(profiles: dict) -> None:
    feature = profiles["feature"]
    projected = apply_default_mode_projection(
        _contract(""), profile=feature, cli_mode="governed",
    )
    assert projected.work_mode == "governed"
    assert selection_context_from_extras({}, projected).work_mode == "governed"


def test_apply_complex_feature_projects_pro(profiles: dict) -> None:
    projected = apply_default_mode_projection(
        _contract(""), profile=profiles["complex_feature"],
    )
    assert projected.work_mode == "pro"


def test_apply_none_contract_is_noop(profiles: dict) -> None:
    assert (
        apply_default_mode_projection(None, profile=profiles["feature"]) is None
    )


def test_default_mode_metadata_matches_projection(profiles: dict) -> None:
    # Sanity bridge: the profile's own default_mode (OperatingMode) and the
    # projected string agree, and none is governed.
    for name in SEMANTIC_PROFILES:
        p = profiles[name]
        assert isinstance(p.default_mode, OperatingMode)
        assert p.default_mode.value == project_effective_work_mode(profile=p)
        assert p.default_mode is not OperatingMode.GOVERNED
