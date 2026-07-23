from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdk import ProfileCustomizeError, customize_profile


def test_customize_profile_writes_workspace_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))

    result = customize_profile(
        "feature",
        default_mode="pro",
        phase_effort=("implement=high",),
        handoff=("validate_plan=human_feedback_always",),
    )

    config_path = workspace / ".orcho" / "config.local.json"
    assert result.config_path == config_path
    assert result.scope == "workspace"
    assert result.changes == (
        "_profile.default_mode",
        "implement.effort",
        "validate_plan.handoff.type",
    )

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["profiles_v2"]["feature"] == {
        "_profile": {"default_mode": "pro"},
        "implement": {"effort": "high"},
        "validate_plan": {
            "handoff": {"type": "human_feedback_always"},
        },
    }


def test_customize_profile_deep_merges_existing_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    config_path = workspace / ".orcho" / "config.local.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({
            "profiles_v2": {
                "feature": {
                    "implement": {
                        "skill": "unity-team-lead",
                    },
                },
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))

    customize_profile("feature", phase_effort=("implement=high",))

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["profiles_v2"]["feature"]["implement"] == {
        "skill": "unity-team-lead",
        "effort": "high",
    }


def test_customize_profile_dry_run_validates_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))

    result = customize_profile(
        "feature",
        default_mode="pro",
        dry_run=True,
    )

    assert result.dry_run is True
    assert not (workspace / ".orcho" / "config.local.json").exists()


def test_customize_profile_rejects_unknown_phase_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))

    with pytest.raises(ProfileCustomizeError, match="no PhaseStep"):
        customize_profile("feature", phase_effort=("ghost=high",))

    assert not (workspace / ".orcho" / "config.local.json").exists()


def test_customize_profile_rejects_invalid_profile_value_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))

    with pytest.raises(ProfileCustomizeError, match="default_mode"):
        customize_profile("feature", default_mode="turbo")

    assert not (workspace / ".orcho" / "config.local.json").exists()


def test_customize_profile_user_scope_uses_home_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = customize_profile(
        "small_task",
        scope="user",
        assignments=("_profile.default_mode=pro",),
    )

    assert result.config_path == tmp_path / ".orcho" / "config.local.json"
    data = json.loads(result.config_path.read_text(encoding="utf-8"))
    assert data["profiles_v2"]["small_task"]["_profile"]["default_mode"] == "pro"

