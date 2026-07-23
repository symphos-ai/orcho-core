"""Deterministic default-mode projection — isolated unit tests.

Covers the Stage C ``semantic_profile → default OperatingMode`` table
(``default_operating_mode``): exact per-member values for all nine work
kinds, the invariant that ``governed`` is never a default output, table
completeness over the closed enum, purity (idempotent / no I/O), and the
side-effect-free import boundary (verified in a clean subprocess so other
tests' ``sys.modules`` pollution cannot mask a loader leak).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from pipeline.runtime.run_shape import OperatingMode, SemanticProfile
from pipeline.runtime.semantic_mode_defaults import default_operating_mode

# The Stage C default-mode decision table, pinned literally so a drift in
# the implementation is caught here rather than inferred from it.
_EXPECTED: dict[str, str] = {
    "small_task": "fast",
    "feature": "pro",
    "complex_feature": "pro",
    "planning": "pro",
    "code_review": "pro",
    "delivery_audit": "pro",
    "research": "fast",
    "refactor": "pro",
    "migration": "pro",
}


@pytest.mark.parametrize(("profile_value", "expected_mode"), _EXPECTED.items())
def test_default_operating_mode_matches_table(
    profile_value: str, expected_mode: str
) -> None:
    result = default_operating_mode(SemanticProfile(profile_value))
    assert result is OperatingMode(expected_mode)


def test_table_covers_every_semantic_profile_member() -> None:
    # All nine members are mapped; the pinned table mirrors the enum exactly.
    assert {p.value for p in SemanticProfile} == set(_EXPECTED)
    for profile in SemanticProfile:
        # Does not raise for any live member.
        assert isinstance(default_operating_mode(profile), OperatingMode)


def test_no_output_is_governed() -> None:
    outputs = {default_operating_mode(p) for p in SemanticProfile}
    assert OperatingMode.GOVERNED not in outputs
    assert outputs == {OperatingMode.FAST, OperatingMode.PRO}


def test_projection_is_pure_and_idempotent() -> None:
    # Same input → same output, repeatedly; the function holds no state.
    for profile in SemanticProfile:
        first = default_operating_mode(profile)
        second = default_operating_mode(profile)
        assert first is second


# ── Side-effect-free import boundary (clean subprocess) ──────────────────────

# Run in a *fresh* interpreter so other tests cannot have already imported the
# loader / read the profile JSON. open() and Path.read_text are traced BEFORE
# importing the helper; the guard fails if the shipped profile JSON is touched
# or the profile loader leaks into sys.modules.
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

import pipeline.runtime.semantic_mode_defaults as smd  # noqa: F401

assert "pipeline.profiles.loader" not in sys.modules, "loader leaked into import"

# Projection stays inert and callable.
from pipeline.runtime.run_shape import OperatingMode, SemanticProfile

assert smd.default_operating_mode(SemanticProfile.FEATURE) is OperatingMode.PRO
assert smd.default_operating_mode(SemanticProfile.MIGRATION) is OperatingMode.PRO

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
