"""Safe project-plugin materialisation from fine-tune inspection results.

This SDK boundary is deliberately narrow: callers provide the projects that
were explicitly registered during workspace onboarding.  The inspector remains
pure-read; this module alone writes a project plugin and only through exclusive
creation, never by replacing an existing filesystem entry.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pipeline.plugins import PLUGIN_RELATIVE_PATH
from sdk.fine_tune import fine_tune_project
from sdk.workspace_scaffold import render_plugin_template

ProjectPluginStatus = Literal["created", "skipped", "failed"]


@dataclass(frozen=True, slots=True)
class ProjectPluginOutcome:
    """One explicit project's plugin materialisation outcome."""

    project_path: str
    destination: str
    status: ProjectPluginStatus
    detail: str = ""


def materialize_project_plugins(
    project_paths: Iterable[Path | str],
) -> tuple[ProjectPluginOutcome, ...]:
    """Create candidate-backed plugins for explicit project paths.

    Existing files, directories, symlinks (including broken links), and
    concurrent creators are all reported as ``skipped``.  A problem inspecting
    or creating one project yields a ``failed`` outcome without affecting the
    others.
    """
    return tuple(_materialize_project_plugin(project_path) for project_path in project_paths)


def _materialize_project_plugin(project_path: Path | str) -> ProjectPluginOutcome:
    project = Path(project_path).expanduser()
    destination = project / PLUGIN_RELATIVE_PATH

    if not project.is_dir():
        return _outcome(project, destination, "failed", "project path is not a directory")
    if _destination_exists(destination):
        return _outcome(project, destination, "skipped", "destination already exists")

    try:
        candidate = fine_tune_project(str(project), dry_run=True).candidate
        body = render_plugin_template(candidate)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as plugin_file:
            plugin_file.write(body)
    except FileExistsError:
        return _outcome(project, destination, "skipped", "destination already exists")
    except Exception as exc:  # per-project failures must not stop other projects
        return _outcome(project, destination, "failed", str(exc))
    return _outcome(project, destination, "created")


def _outcome(
    project: Path,
    destination: Path,
    status: ProjectPluginStatus,
    detail: str = "",
) -> ProjectPluginOutcome:
    return ProjectPluginOutcome(
        project_path=str(project),
        destination=str(destination),
        status=status,
        detail=detail,
    )


def _destination_exists(path: Path) -> bool:
    """Return true for every existing entry, including a broken symlink."""
    return path.exists() or path.is_symlink()


__all__ = [
    "ProjectPluginOutcome",
    "ProjectPluginStatus",
    "materialize_project_plugins",
]
