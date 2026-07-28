"""Canonical paths for project-oriented Orcho workspaces.

The project checkout and the Orcho control workspace are independent
identities.  A normal single-project onboarding stores run state outside the
repository and derives that location deterministically from the canonical
project path.  Existing sibling ``workspace-orchestrator`` layouts remain an
explicit multi-project topology.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_PROJECT_REPO_MARKERS: Final[frozenset[str]] = frozenset({
    ".git",
    "pyproject.toml",
    "package.json",
    "composer.json",
    "go.mod",
    "Cargo.toml",
})
_WORKSPACE_SUBDIR: Final[str] = "workspace-orchestrator"
_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9]+")


@dataclass(frozen=True, slots=True)
class WorkspaceInitTarget:
    """Resolved storage and topology for one workspace-init input."""

    input_root: Path
    workspace_dir: Path
    topology: str
    project_name: str | None


def project_repo_marker(path: Path | str) -> str | None:
    """Return the first project marker at ``path``, or ``None``."""
    candidate = Path(path).expanduser()
    if not candidate.is_dir():
        return None
    for marker in sorted(_PROJECT_REPO_MARKERS):
        if (candidate / marker).exists():
            return marker
    return None


def managed_workspaces_root() -> Path:
    """Return the platform-local root for managed Orcho workspaces."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", "~")).expanduser()
        return base / "Orcho" / "workspaces"
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "orcho" / "workspaces"


def managed_workspace_dir(project: Path | str) -> Path:
    """Return the deterministic managed workspace for one canonical project."""
    canonical = Path(project).expanduser().resolve()
    slug = _SLUG_RE.sub("-", canonical.name).strip("-").lower() or "project"
    digest = hashlib.sha256(os.fsencode(canonical)).hexdigest()[:10]
    return managed_workspaces_root() / f"{slug}-{digest}" / _WORKSPACE_SUBDIR


def resolve_workspace_init_target(
    input_root: Path,
    *,
    workspace_dir: Path | str | None = None,
) -> WorkspaceInitTarget:
    """Separate the project identity from the control-workspace identity."""
    marker = project_repo_marker(input_root)
    project_name = input_root.name if marker is not None else None
    resolved_workspace = (
        Path(workspace_dir).expanduser().resolve()
        if workspace_dir is not None
        else (
            managed_workspace_dir(input_root)
            if project_name is not None
            else input_root / _WORKSPACE_SUBDIR
        )
    )
    return WorkspaceInitTarget(
        input_root=input_root,
        workspace_dir=resolved_workspace,
        topology="project" if project_name is not None else "group",
        project_name=project_name,
    )


def infer_workspace_from_project(project: Path | str) -> Path | None:
    """Resolve an existing workspace for ``project``.

    An established sibling/group layout wins only when its config explicitly
    registers the project. Otherwise the deterministic managed workspace
    created by project-oriented onboarding is used. No directory is created by
    this reader.
    """
    try:
        canonical = Path(project).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if not canonical.exists():
        return None

    project_root = next(
        (
            candidate
            for candidate in (canonical, *canonical.parents)
            if project_repo_marker(candidate) is not None
        ),
        None,
    )
    if project_root is None:
        # A non-project directory may still be a cwd nested inside an explicit
        # shared workspace tree. There is no project identity to bind, so keep
        # the established structural walk-up for this case.
        for ancestor in (canonical, *canonical.parents):
            sibling = ancestor / _WORKSPACE_SUBDIR
            if sibling.is_dir():
                return sibling
        return None

    for ancestor in (project_root, *project_root.parents):
        sibling = ancestor / _WORKSPACE_SUBDIR
        if (
            sibling.is_dir()
            and _workspace_registers_project(sibling, project_root)
        ):
            return sibling

    managed = managed_workspace_dir(project_root)
    return managed if managed.is_dir() else None


def _workspace_registers_project(
    workspace_dir: Path,
    project_root: Path,
) -> bool:
    """Return whether a shared workspace explicitly owns ``project_root``."""
    canonical = project_root.resolve()
    for filename in ("config.local.json", "config.json"):
        path = workspace_dir / ".orcho" / filename
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        projects = raw.get("projects") if isinstance(raw, dict) else None
        if not isinstance(projects, dict):
            continue
        for value in projects.values():
            candidate = value.get("path") if isinstance(value, dict) else value
            if not isinstance(candidate, str):
                continue
            try:
                if Path(candidate).expanduser().resolve() == canonical:
                    return True
            except (OSError, RuntimeError):
                continue
    return False


__all__ = [
    "infer_workspace_from_project",
    "managed_workspace_dir",
    "managed_workspaces_root",
    "project_repo_marker",
    "resolve_workspace_init_target",
    "WorkspaceInitTarget",
]
