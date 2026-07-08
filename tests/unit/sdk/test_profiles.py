"""Unit tests for the ``sdk.profiles`` catalogue read-surface."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.infra.paths import CONFIG_DIR


def test_public_imports() -> None:
    """Both the package-level and module-level import paths resolve."""
    import sdk
    from sdk import ProfileSummary, list_profiles
    from sdk.profiles import (
        ProfileSummary as ModProfileSummary,
        catalogue_path,
        list_profiles as mod_list_profiles,
    )

    assert ProfileSummary is ModProfileSummary
    assert list_profiles is mod_list_profiles
    assert catalogue_path is not None
    for name in ("ProfileSummary", "list_profiles", "catalogue_path"):
        assert name in sdk.__all__
    from sdk import profiles as profiles_mod

    assert set(profiles_mod.__all__) == {
        "ProfileSummary",
        "list_profiles",
        "catalogue_path",
    }


def test_shipped_catalogue_projection() -> None:
    """The shipped catalogue projects the documented per-profile fields."""
    from sdk.profiles import list_profiles

    summaries = list_profiles()
    assert summaries, "shipped catalogue should not be empty"
    by_name = {s.name: s for s in summaries}

    # auto-detect is a selector token, never an executable profile.
    assert "auto-detect" not in by_name

    # Every summary carries a non-empty description.
    assert all(s.description.strip() for s in summaries)

    feature = by_name["feature"]
    assert feature.default_mode == "pro"
    assert feature.isolated is True
    assert feature.phases  # non-empty flattened phase sequence
    assert all(isinstance(p, str) for p in feature.phases)
    assert isinstance(feature.cross_gates, dict) and feature.cross_gates
    # Projected gate policy fields are plain strings/bools, not enums.
    gate = next(iter(feature.cross_gates.values()))
    assert set(gate) == {"enabled", "run", "on_skip", "mode"}
    assert isinstance(gate["enabled"], bool)
    assert isinstance(gate["run"], str)

    small = by_name["small_task"]
    assert small.default_mode == "fast"
    assert small.isolated is False

    # task/correction leave default_mode unset.
    assert by_name["task"].default_mode is None

    # A profile without a cross_gates block projects cross_gates to None.
    assert by_name["planning"].cross_gates is None

    # hypothesis honours the dict|None contract for every profile.
    for summary in summaries:
        assert summary.hypothesis is None or isinstance(summary.hypothesis, dict)
        if isinstance(summary.hypothesis, dict):
            assert set(summary.hypothesis) == {"attempts", "format"}


def _build_fixture_catalogue(tmp_path: Path) -> Path:
    """Derive a distinguishable single-profile catalogue from the shipped one.

    Reuse the shipped ``feature`` entry (a schema-valid profile) but rename
    its key to a unique name and stamp a unique description marker, so the
    env-override test observably differs from the default catalogue.
    """
    raw = json.loads(
        (CONFIG_DIR / "pipeline_profiles_v2.json").read_text(encoding="utf-8")
    )
    fixture: dict = {}
    # Preserve comment-shaped keys so load_profiles_v2 parses identically.
    for key, value in raw.items():
        if key.startswith("_"):
            fixture[key] = value
    entry = dict(raw["feature"])
    entry["description"] = "FIXTURE-ONLY-MARKER"
    fixture["fixture_only"] = entry

    target = tmp_path / "profiles_fixture.json"
    target.write_text(json.dumps(fixture), encoding="utf-8")
    return target


def test_env_override_is_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ORCHO_PROFILES_V2_PATH redirects both catalogue_path and list_profiles."""
    from sdk.profiles import catalogue_path, list_profiles

    fixture = _build_fixture_catalogue(tmp_path)
    monkeypatch.setenv("ORCHO_PROFILES_V2_PATH", str(fixture))

    assert catalogue_path() == fixture

    summaries = list_profiles()
    names = {s.name for s in summaries}
    # The unique fixture profile is present; shipped defaults are absent —
    # this fails if the implementation reads the default catalogue instead.
    assert "fixture_only" in names
    assert "feature" not in names
    assert "small_task" not in names
    assert any(s.description == "FIXTURE-ONLY-MARKER" for s in summaries)


def test_missing_catalogue_returns_empty_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-existent override path yields an empty tuple, no traceback."""
    from sdk.profiles import list_profiles

    monkeypatch.setenv("ORCHO_PROFILES_V2_PATH", str(tmp_path / "nope.json"))
    assert list_profiles() == ()
