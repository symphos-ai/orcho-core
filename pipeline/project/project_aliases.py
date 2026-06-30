"""Workspace project alias resolution for CLI entry points."""
from __future__ import annotations

import json
from pathlib import Path


def _parse_project_entry(raw_entry: object) -> tuple[str, str] | None:
    """Return (path_str, git_dir_str) from a projects-map entry, or None to skip."""
    if isinstance(raw_entry, str):
        raw_path = raw_entry.strip()
        if not raw_path:
            return None
        return raw_path, ""
    if isinstance(raw_entry, dict):
        raw_path = raw_entry.get("path", "")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        git_dir = raw_entry.get("git_dir", "")
        if not isinstance(git_dir, str):
            git_dir = ""
        return raw_path.strip(), git_dir.strip()
    return None


def load_workspace_project_aliases(
    *, workspace: str | Path | None = None,
) -> dict[str, Path]:
    workspace_dir = _resolve_workspace_dir(workspace)
    if workspace_dir is None:
        return {}
    config_path = workspace_dir / ".orcho" / "config.local.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    raw_projects = data.get("projects")
    if not isinstance(raw_projects, dict):
        return {}

    aliases: dict[str, Path] = {}
    for alias, raw_entry in raw_projects.items():
        if not isinstance(alias, str) or not alias.strip():
            continue
        parsed = _parse_project_entry(raw_entry)
        if parsed is None:
            continue
        path_str, _ = parsed
        aliases[alias] = Path(path_str).expanduser().resolve()
    return aliases


def load_workspace_project_git_dir(
    project_path: str | Path,
    *,
    workspace: str | Path | None = None,
) -> str:
    """Return the ``git_dir`` recorded in the workspace config for ``project_path``.

    Matches by resolved absolute path. Returns ``""`` when the project is not
    registered or has no ``git_dir`` entry.
    """
    workspace_dir = _resolve_workspace_dir(workspace)
    if workspace_dir is None:
        return ""
    config_path = workspace_dir / ".orcho" / "config.local.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    raw_projects = data.get("projects")
    if not isinstance(raw_projects, dict):
        return ""

    target = Path(project_path).expanduser().resolve()
    for raw_entry in raw_projects.values():
        parsed = _parse_project_entry(raw_entry)
        if parsed is None:
            continue
        path_str, git_dir = parsed
        if Path(path_str).expanduser().resolve() == target:
            return git_dir
    return ""


def resolve_project_alias(
    spec: str | Path | None,
    *,
    workspace: str | Path | None = None,
) -> Path | None:
    if spec is None:
        return None
    raw = str(spec).strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.exists():
        return path.resolve()
    aliases = load_workspace_project_aliases(workspace=workspace)
    return aliases.get(raw)


def _resolve_workspace_dir(workspace: str | Path | None) -> Path | None:
    if workspace is not None and str(workspace).strip():
        return Path(workspace).expanduser().resolve()

    from core.infra.paths import workspace_config_dir

    config_dir = workspace_config_dir()
    if config_dir is not None:
        return config_dir.parent.resolve()
    return None
