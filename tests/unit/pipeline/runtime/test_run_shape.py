"""Stage B inert run-shape vocabulary — isolated unit tests.

Covers the closed enum vocabularies (``SemanticProfile`` / ``OperatingMode``),
the frozen-dataclass invariants of ``OperatingModePolicy`` / ``RunShape``,
the F2 operating-mode/policy consistency invariant, and the F1 side-effect-free
import boundary (verified in a clean subprocess so other tests' ``sys.modules``
pollution cannot mask a regression).

These tests do **not** duplicate or relax the Stage A profile tests
(``test_profile_loader.py``, ``test_correction_profile.py``,
``test_verification_contract.py``) — those keep owning the shipped flat-profile
and WORK_MODES contracts.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from types import SimpleNamespace

import pytest

from pipeline.runtime.roles import ImplementationExecution
from pipeline.runtime.run_shape import (
    OperatingMode,
    OperatingModePolicy,
    RunShape,
    ScopeExpansionSanctionPolicy,
    SemanticProfile,
    coerce_operating_mode,
    operating_mode_from_state,
)

# ── OperatingMode state-stamp readers (single sanction-mode source) ──────────


def test_coerce_operating_mode_member_string_and_unknown() -> None:
    assert coerce_operating_mode(OperatingMode.PRO) is OperatingMode.PRO
    assert coerce_operating_mode("governed") is OperatingMode.GOVERNED
    # Surrounding whitespace is tolerated (work_mode values are pre-validated).
    assert coerce_operating_mode("  fast  ") is OperatingMode.FAST
    # Absent / blank / unknown → None so the caller applies its own default.
    assert coerce_operating_mode(None) is None
    assert coerce_operating_mode("") is None
    assert coerce_operating_mode("turbo") is None


def test_operating_mode_from_state_reads_projected_stamp() -> None:
    state = SimpleNamespace(extras={"operating_mode": "pro"})
    assert operating_mode_from_state(state) is OperatingMode.PRO
    # A member object is accepted verbatim.
    state.extras["operating_mode"] = OperatingMode.GOVERNED
    assert operating_mode_from_state(state) is OperatingMode.GOVERNED


def test_operating_mode_from_state_defaults_fast_when_unstamped() -> None:
    # No projected posture → the conservative FAST default (fast stays default
    # when the mode is unresolved), for both an empty and an absent extras map.
    assert operating_mode_from_state(SimpleNamespace(extras={})) is OperatingMode.FAST
    assert operating_mode_from_state(SimpleNamespace(extras=None)) is OperatingMode.FAST


# ── Enum vocabularies (closed sets) ──────────────────────────────────────────


def test_semantic_profile_exact_value_set() -> None:
    assert {p.value for p in SemanticProfile} == {
        "small_task",
        "feature",
        "complex_feature",
        "planning",
        "code_review",
        "delivery_audit",
        "research",
        "refactor",
        "migration",
    }


def test_semantic_profile_accepts_stage_c_members() -> None:
    # Stage C vocab: complex_feature (renamed from heavy_feature) and the
    # newly added refactor work kind are both live members.
    assert SemanticProfile("complex_feature") is SemanticProfile.COMPLEX_FEATURE
    assert SemanticProfile("refactor") is SemanticProfile.REFACTOR


def test_semantic_profile_rejects_develop() -> None:
    # Regression guard: 'develop' must never become a live member.
    with pytest.raises(ValueError):
        SemanticProfile("develop")


def test_semantic_profile_rejects_heavy_feature() -> None:
    # Regression guard: 'heavy_feature' is the historical draft name (now
    # 'complex_feature') and is not retained, even as an alias.
    with pytest.raises(ValueError):
        SemanticProfile("heavy_feature")


def test_operating_mode_exact_value_set() -> None:
    assert {m.value for m in OperatingMode} == {"fast", "pro", "governed"}


def test_operating_mode_rejects_team() -> None:
    # Regression guard: 'team' is historical (now 'pro'), not a live member.
    with pytest.raises(ValueError):
        OperatingMode("team")


# ── Minimal construction (no loader / no JSON) ───────────────────────────────


def test_minimal_run_shape_constructs() -> None:
    shape = RunShape(
        semantic_profile=SemanticProfile.FEATURE,
        operating_mode=OperatingMode.PRO,
    )
    assert shape.semantic_profile is SemanticProfile.FEATURE
    assert shape.operating_mode is OperatingMode.PRO
    # Documented inert defaults.
    assert shape.policy is None
    assert shape.worktree_isolation_intent is None
    assert shape.implementation_execution_intent is None
    assert shape.includes_planning is False
    assert shape.includes_review is False
    assert shape.includes_repair is False
    assert shape.includes_final_acceptance is False
    assert shape.reason == ""
    assert shape.notes == ""
    # NB: the side-effect-free import boundary (loader absent / profile JSON
    # untouched) is asserted authoritatively by
    # ``test_import_is_side_effect_free_in_clean_subprocess`` — a shared-process
    # ``sys.modules`` check here would be order-dependent (a sibling test that
    # imports the loader would pollute it), so it is intentionally not made.


def test_minimal_policy_constructs() -> None:
    policy = OperatingModePolicy(operating_mode=OperatingMode.GOVERNED)
    assert policy.operating_mode is OperatingMode.GOVERNED
    assert policy.require_proof_before_transitions is False
    assert policy.repair_on_gate_failure is False
    assert policy.notes == ""


def test_run_shape_accepts_optional_intents() -> None:
    shape = RunShape(
        semantic_profile=SemanticProfile.COMPLEX_FEATURE,
        operating_mode=OperatingMode.GOVERNED,
        worktree_isolation_intent="per_run",
        implementation_execution_intent=ImplementationExecution.SUBTASK_DAG,
        includes_planning=True,
        includes_review=True,
        includes_repair=True,
        includes_final_acceptance=True,
        reason="resolver would explain here",
    )
    assert shape.worktree_isolation_intent == "per_run"
    assert (
        shape.implementation_execution_intent is ImplementationExecution.SUBTASK_DAG
    )
    assert shape.includes_final_acceptance is True


# ── Frozen ───────────────────────────────────────────────────────────────────


def test_run_shape_is_frozen() -> None:
    shape = RunShape(
        semantic_profile=SemanticProfile.SMALL_TASK,
        operating_mode=OperatingMode.FAST,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        shape.operating_mode = OperatingMode.PRO  # type: ignore[misc]


def test_operating_mode_policy_is_frozen() -> None:
    policy = OperatingModePolicy(operating_mode=OperatingMode.PRO)
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.notes = "mutated"  # type: ignore[misc]


# ── Field type validation ────────────────────────────────────────────────────


def test_run_shape_rejects_non_str_worktree_intent() -> None:
    with pytest.raises(TypeError):
        RunShape(
            semantic_profile=SemanticProfile.FEATURE,
            operating_mode=OperatingMode.PRO,
            worktree_isolation_intent=123,  # type: ignore[arg-type]
        )


def test_run_shape_rejects_non_bool_includes_flag() -> None:
    with pytest.raises(TypeError):
        RunShape(
            semantic_profile=SemanticProfile.FEATURE,
            operating_mode=OperatingMode.PRO,
            includes_planning="yes",  # type: ignore[arg-type]
        )


def test_run_shape_rejects_wrong_implementation_execution_type() -> None:
    with pytest.raises(TypeError):
        RunShape(
            semantic_profile=SemanticProfile.FEATURE,
            operating_mode=OperatingMode.PRO,
            implementation_execution_intent="subtask_dag",  # type: ignore[arg-type]
        )


def test_run_shape_rejects_wrong_policy_type() -> None:
    with pytest.raises(TypeError):
        RunShape(
            semantic_profile=SemanticProfile.FEATURE,
            operating_mode=OperatingMode.PRO,
            policy="governed",  # type: ignore[arg-type]
        )


def test_run_shape_coerces_bad_enum_value() -> None:
    # An invalid semantic-profile string fails fast at coercion time.
    with pytest.raises(ValueError):
        RunShape(
            semantic_profile="develop",  # type: ignore[arg-type]
            operating_mode=OperatingMode.PRO,
        )


def test_operating_mode_policy_rejects_non_bool_flag() -> None:
    with pytest.raises(TypeError):
        OperatingModePolicy(
            operating_mode=OperatingMode.PRO,
            require_proof_before_transitions="strict",  # type: ignore[arg-type]
        )


def test_operating_mode_policy_coerces_bad_enum_value() -> None:
    with pytest.raises(ValueError):
        OperatingModePolicy(operating_mode="team")  # type: ignore[arg-type]


# ── F2 consistency invariant ─────────────────────────────────────────────────


def test_consistent_operating_mode_and_policy_constructs() -> None:
    policy = OperatingModePolicy(operating_mode=OperatingMode.GOVERNED)
    shape = RunShape(
        semantic_profile=SemanticProfile.DELIVERY_AUDIT,
        operating_mode=OperatingMode.GOVERNED,
        policy=policy,
    )
    assert shape.policy is policy


def test_mismatched_operating_mode_and_policy_raises() -> None:
    # F2: this would be a formally valid but contradictory shape; reject it.
    # Without the invariant this construction would silently succeed.
    with pytest.raises(ValueError):
        RunShape(
            semantic_profile=SemanticProfile.FEATURE,
            operating_mode=OperatingMode.FAST,
            policy=OperatingModePolicy(operating_mode=OperatingMode.GOVERNED),
        )


def test_run_shape_without_policy_is_allowed() -> None:
    shape = RunShape(
        semantic_profile=SemanticProfile.RESEARCH,
        operating_mode=OperatingMode.FAST,
        policy=None,
    )
    assert shape.policy is None


# ── Scope-expansion sanction policy carrier (ADR 0112 §5 knob) ───────────────


def test_minimal_scope_expansion_sanction_policy_constructs() -> None:
    policy = ScopeExpansionSanctionPolicy(operating_mode=OperatingMode.GOVERNED)
    assert policy.operating_mode is OperatingMode.GOVERNED
    assert policy.notes == ""


def test_scope_expansion_sanction_policy_is_frozen() -> None:
    policy = ScopeExpansionSanctionPolicy(operating_mode=OperatingMode.PRO)
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.notes = "mutated"  # type: ignore[misc]


def test_scope_expansion_sanction_policy_coerces_bad_enum_value() -> None:
    with pytest.raises(ValueError):
        ScopeExpansionSanctionPolicy(operating_mode="team")  # type: ignore[arg-type]


def test_scope_expansion_sanction_policy_rejects_non_str_notes() -> None:
    with pytest.raises(TypeError):
        ScopeExpansionSanctionPolicy(
            operating_mode=OperatingMode.PRO,
            notes=123,  # type: ignore[arg-type]
        )


def test_run_shape_carries_scope_expansion_sanction_knob() -> None:
    # The knob is a POLICY carrier, not a baked outcome enum.
    policy = ScopeExpansionSanctionPolicy(operating_mode=OperatingMode.PRO)
    shape = RunShape(
        semantic_profile=SemanticProfile.COMPLEX_FEATURE,
        operating_mode=OperatingMode.PRO,
        scope_expansion_sanction=policy,
    )
    assert shape.scope_expansion_sanction is policy
    assert isinstance(shape.scope_expansion_sanction, ScopeExpansionSanctionPolicy)


def test_run_shape_without_scope_expansion_sanction_is_allowed() -> None:
    shape = RunShape(
        semantic_profile=SemanticProfile.SMALL_TASK,
        operating_mode=OperatingMode.FAST,
    )
    assert shape.scope_expansion_sanction is None


def test_run_shape_rejects_wrong_scope_expansion_sanction_type() -> None:
    with pytest.raises(TypeError):
        RunShape(
            semantic_profile=SemanticProfile.FEATURE,
            operating_mode=OperatingMode.PRO,
            scope_expansion_sanction="governed",  # type: ignore[arg-type]
        )


def test_mismatched_scope_expansion_sanction_mode_raises() -> None:
    # F2: a carried sanction policy must agree with the shape's operating mode,
    # exactly like ``policy``. Without the invariant this would silently succeed.
    with pytest.raises(ValueError):
        RunShape(
            semantic_profile=SemanticProfile.FEATURE,
            operating_mode=OperatingMode.FAST,
            scope_expansion_sanction=ScopeExpansionSanctionPolicy(
                operating_mode=OperatingMode.GOVERNED
            ),
        )


def test_consistent_scope_expansion_sanction_mode_constructs() -> None:
    policy = ScopeExpansionSanctionPolicy(operating_mode=OperatingMode.GOVERNED)
    shape = RunShape(
        semantic_profile=SemanticProfile.DELIVERY_AUDIT,
        operating_mode=OperatingMode.GOVERNED,
        policy=OperatingModePolicy(operating_mode=OperatingMode.GOVERNED),
        scope_expansion_sanction=policy,
    )
    assert shape.scope_expansion_sanction is policy
    # Both carriers coexist and agree with the shape's mode.
    assert shape.policy is not None
    assert shape.policy.operating_mode is OperatingMode.GOVERNED


# ── Regression guards: no resolver, no flat-profile mapping ──────────────────


def test_no_resolver_function_present() -> None:
    import pipeline.runtime.run_shape as run_shape

    assert not hasattr(run_shape, "resolve_run_shape")


def test_module_exposes_only_the_expected_value_types() -> None:
    import pipeline.runtime.run_shape as run_shape

    assert set(run_shape.__all__) == {
        "DeliveryScope",
        "OperatingMode",
        "OperatingModePolicy",
        "RunShape",
        "RunTopology",
        "ScopeExpansionSanctionPolicy",
        "SemanticProfile",
        "coerce_operating_mode",
        "operating_mode_from_state",
    }


# ── F1 side-effect-free import boundary (clean subprocess) ───────────────────

# Run in a *fresh* interpreter so other tests cannot have already imported the
# loader / read the profile JSON. open() and Path.read_text are traced BEFORE
# importing run_shape; the guard fails if the shipped profile JSON is touched.
#
# The forbidden token is the *profile* JSON (``pipeline_profiles``), not all of
# ``core/_config``: importing ``pipeline.runtime.run_shape`` necessarily runs
# ``pipeline/runtime/__init__.py``, which legitimately reads
# ``core/_config/config.defaults.json`` via the prompts/config stack. That read
# is unrelated to semantic profiles. Per the Stage B acceptance criterion the
# boundary we assert is "no profile loader, no profile JSON", not "zero
# submodule imports" — so we trace for the profile JSON filename only.
_GUARD_SOURCE = r"""
import builtins
import pathlib
import sys

_FORBIDDEN = ("pipeline_profiles_v2.json", "pipeline_profiles")


def _check(path):
    text = str(path)
    if any(token in text for token in _FORBIDDEN):
        raise AssertionError("forbidden profile-config path opened: " + text)


_real_open = builtins.open


def _traced_open(file, *args, **kwargs):
    _check(file)
    return _real_open(file, *args, **kwargs)


builtins.open = _traced_open

_real_read_text = pathlib.Path.read_text


def _traced_read_text(self, *args, **kwargs):
    _check(self)
    return _real_read_text(self, *args, **kwargs)


pathlib.Path.read_text = _traced_read_text

import pipeline.runtime.run_shape  # noqa: F401

assert "pipeline.profiles.loader" not in sys.modules, "loader leaked into import"

# Construction stays inert too.
from pipeline.runtime.run_shape import OperatingMode, RunShape, SemanticProfile

RunShape(semantic_profile=SemanticProfile.FEATURE, operating_mode=OperatingMode.PRO)

print("OK")
"""


def test_import_is_side_effect_free_in_clean_subprocess() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _GUARD_SOURCE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"guard subprocess failed (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout, (
        f"guard subprocess did not print OK\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
