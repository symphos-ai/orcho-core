"""Layered ``config.local.json`` lookup semantics."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.infra import config


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture()
def config_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Path]:
    package_config = tmp_path / "package" / "_config"
    fake_home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    _write_json(
        package_config / "config.defaults.json",
        {
            "phases": {
                "plan": {
                    "runtime": "claude",
                    "model": "default-plan",
                    "effort": "low",
                },
                "implement": {
                    "runtime": "claude",
                    "model": "default-implement",
                    "effort": "low",
                },
                "review_changes": {
                    "runtime": "codex",
                    "model": "default-review",
                    "effort": "low",
                },
            },
            "timeouts": {"claude_idle_seconds": 1},
            "session": {"mode": "auto"},
            "codemap": {"enabled": False},
            "hypothesis": {"enabled": False},
            "language": {
                "plan_language": "English",
                "task_language": "English",
            },
            "artifacts": {"mirror_to_project": False},
            "pipeline": {"change_handoff": "uncommitted"},
            "commit": {
                "enabled": True,
                "interactive_default": "apply",
                "auto_in_ci": "approve",
            },
            "worktree": {"enabled": True, "isolation": "per_run"},
            "pre_run_dirty": {
                "enabled": True,
                "interactive_default": "include",
                "non_interactive_default": "halt",
                "include_untracked": "prompt",
            },
            "sandbox": {"mode": "env", "network": "open"},
        },
    )
    monkeypatch.setattr(config, "_CONFIG_DIR", package_config)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))
    monkeypatch.delenv("ORCHO_DISABLE_LOCAL_CONFIG", raising=False)
    config.AppConfig.load.cache_clear()
    return {
        "package": package_config,
        "home": fake_home,
        "workspace": workspace,
    }


def test_package_only_layer(config_layout: dict[str, Path]) -> None:
    _write_json(
        config_layout["package"] / "config.local.json",
        {"phases": {"implement": {"model": "package-implement"}}},
    )

    merged = config._merge_json_layers()

    assert merged["phases"]["implement"]["model"] == "package-implement"


def test_user_overrides_package(config_layout: dict[str, Path]) -> None:
    _write_json(
        config_layout["package"] / "config.local.json",
        {"phases": {"implement": {"model": "package-implement"}}},
    )
    _write_json(
        config_layout["home"] / ".orcho" / "config.local.json",
        {"phases": {"implement": {"model": "user-implement"}}},
    )

    merged = config._merge_json_layers()

    assert merged["phases"]["implement"]["model"] == "user-implement"


def test_workspace_overrides_user(config_layout: dict[str, Path]) -> None:
    _write_json(
        config_layout["home"] / ".orcho" / "config.local.json",
        {"phases": {"implement": {"model": "user-implement"}}},
    )
    _write_json(
        config_layout["workspace"] / ".orcho" / "config.local.json",
        {"phases": {"implement": {"model": "workspace-implement"}}},
    )

    merged = config._merge_json_layers()

    assert merged["phases"]["implement"]["model"] == "workspace-implement"


def test_workspace_layer_off_when_env_unset(
    config_layout: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_json(
        config_layout["workspace"] / ".orcho" / "config.local.json",
        {"phases": {"implement": {"model": "workspace-implement"}}},
    )
    monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)

    merged = config._merge_json_layers()

    assert merged["phases"]["implement"]["model"] == "default-implement"


def test_disable_local_config_skips_all_layers(
    config_layout: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_json(
        config_layout["package"] / "config.local.json",
        {"phases": {"implement": {"model": "package-implement"}}},
    )
    _write_json(
        config_layout["home"] / ".orcho" / "config.local.json",
        {"phases": {"implement": {"model": "user-implement"}}},
    )
    _write_json(
        config_layout["workspace"] / ".orcho" / "config.local.json",
        {"phases": {"implement": {"model": "workspace-implement"}}},
    )
    monkeypatch.setenv("ORCHO_DISABLE_LOCAL_CONFIG", "1")

    merged = config._merge_json_layers()

    assert merged["phases"]["implement"]["model"] == "default-implement"


def test_per_phase_partial_override(config_layout: dict[str, Path]) -> None:
    _write_json(
        config_layout["home"] / ".orcho" / "config.local.json",
        {
            "phases": {
                "implement": {
                    "runtime": "claude",
                    "model": "user-implement",
                    "effort": "high",
                },
            },
        },
    )
    _write_json(
        config_layout["workspace"] / ".orcho" / "config.local.json",
        {"phases": {"implement": {"effort": "medium"}}},
    )

    merged = config._merge_json_layers()

    assert merged["phases"]["implement"] == {
        "runtime": "claude",
        "model": "user-implement",
        "effort": "medium",
    }
    assert merged["phases"]["review_changes"]["model"] == "default-review"


def test_null_scaffold_values_do_not_override_lower_layers(
    config_layout: dict[str, Path],
) -> None:
    _write_json(
        config_layout["home"] / ".orcho" / "config.local.json",
        {
            "phases": {
                "implement": {
                    "runtime": "claude",
                    "model": "user-implement",
                    "effort": "high",
                },
            },
            "language": {"plan_language": "Russian"},
        },
    )
    _write_json(
        config_layout["workspace"] / ".orcho" / "config.local.json",
        {
            "phases": {
                "implement": {
                    "runtime": None,
                    "model": None,
                    "effort": "medium",
                },
            },
            "language": {
                "_comment": "workspace scaffold metadata",
                "plan_language": None,
                "task_language": None,
            },
        },
    )

    merged = config._merge_json_layers()

    assert merged["phases"]["implement"] == {
        "runtime": "claude",
        "model": "user-implement",
        "effort": "medium",
    }
    assert merged["language"]["plan_language"] == "Russian"
    assert "_comment" not in merged["language"]


def test_defaults_carry_runtime_policy_sections(
    config_layout: dict[str, Path],
) -> None:
    """Runtime policy sections in ``config.defaults.json`` must reach
    the merged config the same way other top-level sections do —
    otherwise hard-coded Python defaults silently win and the shipped
    JSON defaults are dead weight."""
    merged = config._merge_json_layers()
    assert merged["worktree"]["isolation"] == "per_run"
    assert merged["commit"]["interactive_default"] == "apply"
    assert merged["commit"]["auto_in_ci"] == "approve"
    assert merged["pre_run_dirty"]["interactive_default"] == "include"
    assert merged["sandbox"]["mode"] == "env"
    assert merged["sandbox"]["network"] == "open"


def test_local_overlay_can_disable_sandbox(
    config_layout: dict[str, Path],
) -> None:
    """An operator who sets ``sandbox.mode: off`` in
    ``config.local.json`` must see the override take effect.
    Without the overlay including ``sandbox`` in its known-section
    list, this value is silently dropped on the way through."""
    _write_json(
        config_layout["package"] / "config.local.json",
        {"sandbox": {"mode": "off"}},
    )
    merged = config._merge_json_layers()
    assert merged["sandbox"]["mode"] == "off"


def test_local_overlay_can_disable_worktree(
    config_layout: dict[str, Path],
) -> None:
    """Mirror of the sandbox override case — the worktree section
    suffered the same overlay-bypass bug and is regression-guarded
    here so a future refactor cannot silently re-break it."""
    _write_json(
        config_layout["package"] / "config.local.json",
        {"worktree": {"enabled": False}},
    )
    merged = config._merge_json_layers()
    assert merged["worktree"]["enabled"] is False


def test_local_overlay_can_change_pre_run_dirty_default(
    config_layout: dict[str, Path],
) -> None:
    """Pre-run dirty intake is a top-level runtime policy section and
    must be locally overrideable like worktree and sandbox policy."""
    _write_json(
        config_layout["package"] / "config.local.json",
        {"pre_run_dirty": {"interactive_default": "exclude"}},
    )
    merged = config._merge_json_layers()
    assert merged["pre_run_dirty"]["interactive_default"] == "exclude"


def test_local_overlay_can_change_commit_default(
    config_layout: dict[str, Path],
) -> None:
    """Commit delivery is a top-level runtime policy section and must
    be locally overrideable like worktree and pre-run dirty policy."""
    _write_json(
        config_layout["package"] / "config.local.json",
        {"commit": {"auto_in_ci": "apply"}},
    )
    merged = config._merge_json_layers()
    assert merged["commit"]["auto_in_ci"] == "apply"


def test_workspace_overlay_overrides_sandbox_limits(
    config_layout: dict[str, Path],
) -> None:
    """Per-workspace ``config.local.json`` overlays nested limit
    fields, not just the top-level ``mode``."""
    _write_json(
        config_layout["workspace"] / ".orcho" / "config.local.json",
        {"sandbox": {"limits": {"memory_mb": 8192}}},
    )
    merged = config._merge_json_layers()
    assert merged["sandbox"]["limits"] == {"memory_mb": 8192}
