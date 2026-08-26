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



def _fake_pipeline_config(monkeypatch: pytest.MonkeyPatch, override: object) -> None:
    """Pin ``pipeline.session_split_override`` without touching real config."""
    import core.infra.config as core_config

    class _App:
        pipeline = {"session_split_override": override}

    monkeypatch.setattr(core_config.AppConfig, "load", classmethod(lambda _cls: _App()))


def test_customize_reports_a_write_the_global_override_supersedes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write is valid, persisted — and not what the run will use.

    ``session_split_override`` applies after the profile by design, so this is
    the one customizable field where a successful write can be inert. Saying
    nothing is what turned the same shape into a multi-day field debug once.
    """
    monkeypatch.setenv("ORCHO_WORKSPACE", str(tmp_path / "workspace"))
    _fake_pipeline_config(monkeypatch, {"plan": "common"})

    result = customize_profile("feature", session_split=("plan=per_role",))

    assert result.changes == ("plan.execution.session_split",)
    assert len(result.shadowed) == 1
    note = result.shadowed[0]
    assert "plan.execution.session_split" in note
    assert "session_split_override" in note
    assert "'common'" in note
    # The advisory never withholds the write.
    data = json.loads(result.config_path.read_text(encoding="utf-8"))
    assert data["profiles_v2"]["feature"]["plan"]["execution"]["session_split"] == "per_role"


def test_customize_is_silent_when_nothing_supersedes_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a live override for that same phase is worth reporting."""
    monkeypatch.setenv("ORCHO_WORKSPACE", str(tmp_path / "workspace"))
    _fake_pipeline_config(monkeypatch, {"implement": "common"})

    result = customize_profile(
        "feature", session_split=("plan=per_role",), phase_effort=("plan=low",),
    )

    assert result.shadowed == ()


def test_customize_advisory_never_breaks_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable or malformed config must not fail a valid customization."""
    import core.infra.config as core_config

    monkeypatch.setenv("ORCHO_WORKSPACE", str(tmp_path / "workspace"))

    def _boom(_cls: object) -> object:
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(core_config.AppConfig, "load", classmethod(_boom))
    result = customize_profile("feature", session_split=("plan=per_role",))
    assert result.shadowed == ()

    _fake_pipeline_config(monkeypatch, "not-a-mapping")
    result = customize_profile("feature", session_split=("plan=per_role",))
    assert result.shadowed == ()
