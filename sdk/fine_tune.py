"""sdk.fine_tune — propose a verification contract from a project's shape.

``fine_tune_project`` inspects a project by common repo markers and assembles
a *candidate* verification contract — ``verification_envs`` +
``verification.commands`` + ``default_env`` + ``work_mode`` — expressed in the
generic assertion vocabulary from :mod:`pipeline.verification_env`. Language
knowledge lives in :mod:`sdk.fine_tune_probes`: each marker maps to a
registered probe, and new languages plug in through
:func:`sdk.fine_tune_probes.register_marker_probe` rather than edits here.
When the inspected directory is a workspace root rather than a project root,
the result lists suggested child projects instead of pretending that no setup
is possible.

Stage 2 supports inspection only: the function is **pure-read** and writes
nothing. Materialising the candidate into a ``plugin.py`` is deliberately out
of scope, so even the non-``--dry-run`` path only prints the proposal and a
deferred-materialisation note. Boundary discipline (ADR 0021): returns a typed
result, never prints, never calls ``sys.exit``.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline.verification_cost import resolve_verification_cost
from sdk.fine_tune_probes import (
    iter_marker_probes,
    matches_marker_filename,
    package_excluded_dirs,
)

_DISCOVERY_MAX_DEPTH = 3

_DEFERRED_NOTE = (
    "Candidate only — Stage 2 does not write plugin.py. "
    "Review and materialise the contract yourself."
)

_BOOTSTRAP_LOCKFILES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("package-lock.json", "node_modules", ("npm", "ci")),
    ("poetry.lock", ".venv", ("poetry", "install")),
    ("composer.lock", "vendor", ("composer", "install", "--no-interaction")),
)


@dataclass(frozen=True, slots=True)
class FineTuneResult:
    """Typed outcome of one ``orcho workspace fine-tune`` inspection."""

    project: str
    dry_run: bool
    wrote: bool
    markers: list[str] = field(default_factory=list)
    candidate: dict[str, Any] = field(default_factory=dict)
    suggested_projects: list[str] = field(default_factory=list)
    note: str = ""


def fine_tune_project(project: str, *, dry_run: bool = True) -> FineTuneResult:
    """Inspect ``project`` and return a candidate verification contract.

    Pure-read: no file is created or modified regardless of ``dry_run``.
    ``dry_run`` is the only materialisation mode Stage 2 supports; the flag is
    surfaced on the result so the CLI can report it, but both paths leave the
    project tree byte-identical.
    """
    root = Path(project)
    git_root = _git_toplevel(root)
    nested_path = _nested_project_path(root, git_root)

    envs: dict[str, dict[str, Any]] = {}
    commands: dict[str, dict[str, Any]] = {}
    alternates: list[dict[str, str]] = []
    markers: list[str] = []

    for marker, probe in _direct_markers(root):
        markers.append(marker)
        env = probe(root)
        envs[env.env] = _enrich_env_spec(env.spec, nested_path)
        commands.update(env.commands)
        alternates.extend(env.alternates)

    default_env = next(iter(envs), "")
    candidate: dict[str, Any] = {
        "work_mode": "pro",
        "verification_envs": envs,
        "verification": {
            "default_env": default_env,
            "commands": commands,
            "required": list(commands),
            "schedule": _candidate_schedule(commands),
        },
    }
    bootstrap = _worktree_bootstrap(root, git_root, nested_path)
    if bootstrap:
        candidate["worktree_bootstrap"] = bootstrap
    if alternates:
        candidate["suggested_alternates"] = alternates

    return FineTuneResult(
        project=str(root),
        dry_run=dry_run,
        wrote=False,
        markers=markers,
        candidate=candidate,
        suggested_projects=_discover_project_roots(root) if not markers else [],
        note=_DEFERRED_NOTE,
    )


def _git_toplevel(root: Path) -> Path:
    """Return the repository root, or ``root`` when Git cannot identify one.

    Inspection must remain usable for standalone directories and must never
    depend on shell parsing.  A failed read-only Git query therefore means the
    target is treated as its own worktree root.
    """
    resolved_root = root.resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(resolved_root), "rev-parse", "--show-toplevel"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return resolved_root
    if result.returncode != 0 or not result.stdout.strip():
        return resolved_root
    return Path(result.stdout.strip()).resolve()


def _nested_project_path(root: Path, git_root: Path) -> Path | None:
    """Return a project path relative to its Git root, when genuinely nested."""
    try:
        relative = root.resolve().relative_to(git_root)
    except ValueError:
        return None
    return None if relative == Path(".") else relative


def _enrich_env_spec(spec: dict[str, Any], nested_path: Path | None) -> dict[str, Any]:
    """Copy an environment spec and bind nested projects to their checkout cwd."""
    enriched = dict(spec)
    if nested_path is not None:
        enriched["cwd"] = f"{{checkout}}/{nested_path.as_posix()}"
    return enriched


def _worktree_bootstrap(
    root: Path,
    git_root: Path,
    nested_path: Path | None,
) -> list[dict[str, Any]]:
    """Propose dependency installation for an untracked nested project setup."""
    if nested_path is None:
        return []

    steps: list[dict[str, Any]] = []
    for lockfile, install_artifact, command in _BOOTSTRAP_LOCKFILES:
        if not (root / lockfile).is_file():
            continue
        if _git_index_has_entries(git_root, nested_path / install_artifact):
            continue
        steps.append({"run": list(command), "cwd": nested_path.as_posix()})
    return steps


def _git_index_has_entries(git_root: Path, path: Path) -> bool:
    """Return whether Git tracks a file below ``path`` without inspecting disk."""
    try:
        result = subprocess.run(
            ["git", "-C", str(git_root), "ls-files", "--", path.as_posix()],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _candidate_schedule(commands: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Schedule explicit fast checks early and all other costs at delivery."""
    fast: list[str] = []
    deferred: list[str] = []
    for name, command in commands.items():
        cost = resolve_verification_cost(command.get("cost"), ())
        (fast if cost == "fast" else deferred).append(name)

    schedule: list[dict[str, Any]] = []
    if fast:
        schedule.append({
            "after_phase": "implement", "policy": "warn", "commands": fast,
        })
    if deferred:
        schedule.append({
            "before_delivery": True, "policy": "warn", "commands": deferred,
        })
    return schedule


def _direct_markers(root: Path) -> list[tuple[str, Any]]:
    """Return registered ``(marker, probe)`` pairs present under ``root``."""
    found: list[tuple[str, Any]] = []
    for marker, probe in iter_marker_probes():
        if "*" in marker:
            if any(root.glob(marker)):
                found.append((marker, probe))
        elif (root / marker).is_file():
            found.append((marker, probe))
    return found


def _discover_project_roots(root: Path) -> list[str]:
    """Find likely child project roots under a workspace directory."""
    candidates: set[Path] = set()
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        try:
            rel = current.relative_to(root)
        except ValueError:
            continue
        depth = len(rel.parts)
        dirnames[:] = [
            name for name in dirnames
            if not _skip_discovery_dir(name) and depth < _DISCOVERY_MAX_DEPTH
        ]
        for filename in filenames:
            if matches_marker_filename(filename):
                candidates.add(current)

    ordered = sorted(
        candidates,
        key=lambda item: (len(item.relative_to(root).parts), str(item)),
    )
    kept: list[Path] = []
    for candidate in ordered:
        if any(_is_relative_to(candidate, parent) for parent in kept):
            continue
        kept.append(candidate)
    return [str(path) for path in kept]


def _skip_discovery_dir(name: str) -> bool:
    return name in package_excluded_dirs() or name.startswith(".")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
