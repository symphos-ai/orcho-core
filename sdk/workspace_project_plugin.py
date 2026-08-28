"""Safe project-plugin materialisation from fine-tune inspection results.

This SDK boundary is deliberately narrow: callers provide the projects that
were explicitly registered during workspace onboarding.  The inspector remains
pure-read; this module alone writes a project plugin and only through exclusive
creation, never by replacing an existing filesystem entry.

Candidate derivation is split out so an interactive caller can inspect the
proposals (for example, to warn about projects with no detected repo markers)
before asking for consent, then materialise the same candidates without
running the inspector twice.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pipeline.plugins import PLUGIN_RELATIVE_PATH
from sdk.fine_tune import fine_tune_project
from sdk.workspace_scaffold import render_plugin_template

ProjectPluginStatus = Literal["created", "skipped", "failed"]


@dataclass(frozen=True, slots=True)
class ProjectPluginCandidate:
    """One project's read-only fine-tune proposal, derived once."""

    project_path: str
    candidate: dict[str, Any] | None
    error: str = ""

    @property
    def empty(self) -> bool:
        """True when inspection found no verification commands to propose."""
        if self.candidate is None:
            return False
        commands = (self.candidate.get("verification") or {}).get("commands") or {}
        return not commands


@dataclass(frozen=True, slots=True)
class ProjectPluginOutcome:
    """One explicit project's plugin materialisation outcome."""

    project_path: str
    destination: str
    status: ProjectPluginStatus
    detail: str = ""
    empty: bool = False


def derive_project_plugin_candidates(
    project_paths: Iterable[Path | str],
) -> tuple[ProjectPluginCandidate, ...]:
    """Inspect explicit project paths and return their plugin candidates.

    Pure read: nothing is created or modified.  An inspection problem yields
    a candidate entry carrying ``error`` instead of raising, so one broken
    project cannot hide the proposals for the others.
    """
    return tuple(_derive_candidate(project_path) for project_path in project_paths)


def materialize_project_plugins(
    projects: Iterable[Path | str | ProjectPluginCandidate],
) -> tuple[ProjectPluginOutcome, ...]:
    """Create candidate-backed plugins for explicit projects.

    Accepts raw project paths or candidates already derived by
    :func:`derive_project_plugin_candidates` (paths are derived here).
    Existing files, directories, symlinks (including broken links), and
    concurrent creators are all reported as ``skipped``.  A problem inspecting
    or creating one project yields a ``failed`` outcome without affecting the
    others.
    """
    return tuple(
        _materialize_from_candidate(
            entry
            if isinstance(entry, ProjectPluginCandidate)
            else _derive_candidate(entry)
        )
        for entry in projects
    )


def _derive_candidate(project_path: Path | str) -> ProjectPluginCandidate:
    project = Path(project_path).expanduser()
    if not project.is_dir():
        return ProjectPluginCandidate(
            project_path=str(project),
            candidate=None,
            error="project path is not a directory",
        )
    try:
        candidate = fine_tune_project(str(project), dry_run=True).candidate
    except Exception as exc:  # per-project failures must not stop other projects
        return ProjectPluginCandidate(
            project_path=str(project), candidate=None, error=str(exc),
        )
    return ProjectPluginCandidate(project_path=str(project), candidate=candidate)


def _materialize_from_candidate(
    candidate: ProjectPluginCandidate,
) -> ProjectPluginOutcome:
    project = Path(candidate.project_path)
    destination = project / PLUGIN_RELATIVE_PATH

    if candidate.candidate is None:
        return _outcome(project, destination, "failed", candidate.error)
    if _destination_exists(destination):
        return _outcome(project, destination, "skipped", "destination already exists")

    try:
        body = render_plugin_template(candidate.candidate)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as plugin_file:
            plugin_file.write(body)
    except FileExistsError:
        return _outcome(project, destination, "skipped", "destination already exists")
    except Exception as exc:  # per-project failures must not stop other projects
        return _outcome(project, destination, "failed", str(exc))
    return _outcome(project, destination, "created", empty=candidate.empty)


def _outcome(
    project: Path,
    destination: Path,
    status: ProjectPluginStatus,
    detail: str = "",
    *,
    empty: bool = False,
) -> ProjectPluginOutcome:
    return ProjectPluginOutcome(
        project_path=str(project),
        destination=str(destination),
        status=status,
        detail=detail,
        empty=empty,
    )


def _destination_exists(path: Path) -> bool:
    """Return true for every existing entry, including a broken symlink."""
    return path.exists() or path.is_symlink()


__all__ = [
    "ProjectPluginCandidate",
    "ProjectPluginOutcome",
    "ProjectPluginStatus",
    "derive_project_plugin_candidates",
    "materialize_project_plugins",
]
