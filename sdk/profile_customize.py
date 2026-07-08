"""Profile local-customization writer.

The runtime already supports ``profiles_v2`` overlays in layered
``config.local.json`` files. This SDK surface gives tools a safe writer for that
shape: parse a small patch language, update the chosen local config, and
validate the resulting overlay through the same v2 profile loader invariants.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.infra.paths import CONFIG_DIR, user_config_dir
from sdk.errors import ProfileCustomizeError


@dataclass(frozen=True, slots=True)
class ProfileCustomizeResult:
    """Result of a local profile customization write."""

    profile: str
    scope: str
    config_path: Path
    dry_run: bool
    changes: tuple[str, ...]
    overlay: dict[str, Any]


_SCOPES = frozenset({"workspace", "user"})


def customize_profile(
    profile: str,
    *,
    assignments: list[str] | tuple[str, ...] = (),
    default_mode: str | None = None,
    change_handoff: str | None = None,
    implementation_execution: str | None = None,
    worktree_isolation: str | None = None,
    phase_effort: list[str] | tuple[str, ...] = (),
    session_split: list[str] | tuple[str, ...] = (),
    session_continuity: list[str] | tuple[str, ...] = (),
    handoff: list[str] | tuple[str, ...] = (),
    scope: str = "workspace",
    workspace: str | Path | None = None,
    dry_run: bool = False,
) -> ProfileCustomizeResult:
    """Write ``profiles_v2`` overrides into a local config file.

    ``assignments`` use ``<patch-key>.<field>[.<field>...]=<json-or-string>``.
    The patch key is either ``_profile`` for top-level profile fields or a phase
    name such as ``validate_plan`` / ``implement``. Values are parsed as JSON
    when possible and as strings otherwise.
    """
    name = _non_empty(profile, "profile")
    if scope not in _SCOPES:
        raise ProfileCustomizeError(
            f"profile customize: scope must be one of {sorted(_SCOPES)}, got {scope!r}"
        )

    config_path = _config_path_for_scope(scope, workspace=workspace)
    data = _read_config(config_path)
    patch: dict[str, Any] = {}
    change_labels: list[str] = []

    for label, value in (
        ("default_mode", default_mode),
        ("change_handoff", change_handoff),
        ("implementation_execution", implementation_execution),
        ("worktree_isolation", worktree_isolation),
    ):
        if value is not None:
            _set_nested(patch, ["_profile", label], value)
            change_labels.append(f"_profile.{label}")

    for raw in phase_effort:
        phase, value = _parse_pair(raw, "--phase-effort")
        _set_nested(patch, [phase, "effort"], value)
        change_labels.append(f"{phase}.effort")

    for raw in session_split:
        phase, value = _parse_pair(raw, "--session-split")
        _set_nested(patch, [phase, "execution", "session_split"], value)
        change_labels.append(f"{phase}.execution.session_split")

    for raw in session_continuity:
        phase, value = _parse_pair(raw, "--session-continuity")
        _set_nested(patch, [phase, "execution", "session_continuity"], value)
        change_labels.append(f"{phase}.execution.session_continuity")

    for raw in handoff:
        phase, value = _parse_pair(raw, "--handoff")
        _set_nested(patch, [phase, "handoff", "type"], value)
        change_labels.append(f"{phase}.handoff.type")

    for raw in assignments:
        path, value = _parse_assignment(raw)
        _set_nested(patch, path, value)
        change_labels.append(".".join(path))

    if not patch:
        raise ProfileCustomizeError(
            "profile customize: no changes requested; pass --set or one of the "
            "named customization flags"
        )

    _merge_profile_patch(data, name, patch)
    _validate_local_profiles_v2(data)

    if not dry_run:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    overlay = data.get("profiles_v2", {}).get(name, {})
    return ProfileCustomizeResult(
        profile=name,
        scope=scope,
        config_path=config_path,
        dry_run=dry_run,
        changes=tuple(change_labels),
        overlay=overlay,
    )


def _config_path_for_scope(scope: str, *, workspace: str | Path | None) -> Path:
    if scope == "user":
        return user_config_dir() / "config.local.json"

    ws = _resolve_workspace(workspace)
    if ws is None:
        raise ProfileCustomizeError(
            "profile customize: no workspace could be resolved; pass "
            "--workspace or use --scope user"
        )
    return ws / ".orcho" / "config.local.json"


def _resolve_workspace(workspace: str | Path | None) -> Path | None:
    if workspace is not None:
        return Path(workspace).expanduser().resolve()
    raw = os.environ.get("ORCHO_WORKSPACE")
    if raw:
        return Path(raw).expanduser().resolve()
    from pipeline.project.bootstrap import infer_workspace_from_project

    return infer_workspace_from_project(os.getcwd())


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if not path.is_file():
        raise ProfileCustomizeError(
            f"profile customize: local config path is not a file: {path}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileCustomizeError(
            f"profile customize: local config is not valid JSON: {path}: {exc}"
        ) from exc
    except OSError as exc:
        raise ProfileCustomizeError(
            f"profile customize: could not read local config {path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ProfileCustomizeError(
            f"profile customize: local config must be a JSON object: {path}"
        )
    return data


def _merge_profile_patch(data: dict[str, Any], profile: str, patch: dict[str, Any]) -> None:
    block = data.setdefault("profiles_v2", {})
    if not isinstance(block, dict):
        raise ProfileCustomizeError(
            "profile customize: config.local.json field profiles_v2 must be an object"
        )
    profile_block = block.setdefault(profile, {})
    if not isinstance(profile_block, dict):
        raise ProfileCustomizeError(
            f"profile customize: profiles_v2.{profile} must be an object"
        )
    _deep_merge(profile_block, patch)


def _validate_local_profiles_v2(data: dict[str, Any]) -> None:
    block = data.get("profiles_v2")
    if block is None:
        return
    if not isinstance(block, dict):
        raise ProfileCustomizeError(
            "profile customize: config.local.json field profiles_v2 must be an object"
        )

    profiles_path = CONFIG_DIR / "pipeline_profiles_v2.json"
    try:
        raw = json.loads(profiles_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileCustomizeError(
            f"profile customize: could not read built-in profiles: {exc}"
        ) from exc

    from pipeline.profiles.loader import (
        ProfileLoadError,
        _apply_profile_overlays,
        parse_profiles,
    )

    try:
        _apply_profile_overlays(raw, block)
        parse_profiles(raw)
    except ProfileLoadError as exc:
        raise ProfileCustomizeError(f"profile customize: invalid overlay: {exc}") from exc


def _parse_assignment(raw: str) -> tuple[list[str], Any]:
    if "=" not in raw:
        raise ProfileCustomizeError(
            f"profile customize: assignment must be path=value, got {raw!r}"
        )
    path_raw, value_raw = raw.split("=", 1)
    path = [part.strip() for part in path_raw.split(".") if part.strip()]
    if len(path) < 2:
        raise ProfileCustomizeError(
            "profile customize: assignment path must include a patch key and "
            f"field, got {path_raw!r}"
        )
    return path, _parse_value(value_raw)


def _parse_pair(raw: str, flag: str) -> tuple[str, str]:
    if "=" not in raw:
        raise ProfileCustomizeError(
            f"profile customize: {flag} expects phase=value, got {raw!r}"
        )
    phase, value = raw.split("=", 1)
    return _non_empty(phase, f"{flag} phase"), _non_empty(value, f"{flag} value")


def _parse_value(raw: str) -> Any:
    value = raw.strip()
    if value == "":
        raise ProfileCustomizeError("profile customize: assignment value cannot be empty")
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _set_nested(dst: dict[str, Any], path: list[str], value: Any) -> None:
    if len(path) < 2:
        raise ProfileCustomizeError("profile customize: nested path is too short")
    cur = dst
    for part in path[:-1]:
        existing = cur.get(part)
        if existing is None:
            existing = {}
            cur[part] = existing
        if not isinstance(existing, dict):
            raise ProfileCustomizeError(
                f"profile customize: path {'.'.join(path)} conflicts with a scalar"
            )
        cur = existing
    cur[path[-1]] = value


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_merge(dst[key], value)
        else:
            dst[key] = value


def _non_empty(value: str | None, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ProfileCustomizeError(f"profile customize: {label} is required")
    return text


__all__ = ["ProfileCustomizeResult", "customize_profile"]
