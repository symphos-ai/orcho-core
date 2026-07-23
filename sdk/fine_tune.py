"""sdk.fine_tune — propose a verification contract from a project's shape.

``fine_tune_project`` inspects a project by common repo markers
(``pyproject.toml`` / ``package.json`` / ``composer.json`` / ``go.mod`` /
``Cargo.toml`` / ``*.sln`` / ``*.csproj``) and assembles a *candidate*
verification contract — ``verification_envs`` + ``verification.commands`` +
``default_env`` + ``work_mode`` — expressed in the generic assertion vocabulary
from :mod:`pipeline.verification_env`. When the inspected directory is a
workspace root rather than a project root, the result lists suggested child
projects instead of pretending that no setup is possible.

Stage 2 supports inspection only: the function is **pure-read** and writes
nothing. Materialising the candidate into a ``plugin.py`` is deliberately out
of scope, so even the non-``--dry-run`` path only prints the proposal and a
deferred-materialisation note. Boundary discipline (ADR 0021): returns a typed
result, never prints, never calls ``sys.exit``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.infra.platform import venv_python_subpath

# Repo markers, ordered so the first detected env becomes ``default_env``.
_MARKER_ORDER: tuple[str, ...] = (
    "pyproject.toml",
    "package.json",
    "composer.json",
    "go.mod",
    "Cargo.toml",
    "*.sln",
    "*.csproj",
)

_PACKAGE_EXCLUDED = frozenset({
    "tests", "test", "docs", "doc", "examples", "example",
    "build", "dist", ".venv", "node_modules", "__pycache__",
    ".git", ".orcho", "workspace-orchestrator", "runspace",
})

_DISCOVERY_MAX_DEPTH = 3

_DEFERRED_NOTE = (
    "Candidate only — Stage 2 does not write plugin.py. "
    "Review and materialise the contract yourself."
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

    envs: dict[str, dict[str, Any]] = {}
    commands: dict[str, dict[str, Any]] = {}
    markers: list[str] = []

    for marker in _direct_markers(root):
        markers.append(marker)
        builder = _MARKER_BUILDERS[marker]
        env_name, env_spec, env_commands = builder(root)
        envs[env_name] = env_spec
        commands.update(env_commands)

    default_env = next(iter(envs), "")
    candidate: dict[str, Any] = {
        "work_mode": "pro",
        "verification_envs": envs,
        "verification": {
            "default_env": default_env,
            "commands": commands,
        },
    }

    return FineTuneResult(
        project=str(root),
        dry_run=dry_run,
        wrote=False,
        markers=markers,
        candidate=candidate,
        suggested_projects=_discover_project_roots(root) if not markers else [],
        note=_DEFERRED_NOTE,
    )


def _direct_markers(root: Path) -> list[str]:
    """Return known project markers present directly under ``root``."""
    markers: list[str] = []
    for marker in _MARKER_ORDER:
        if "*" in marker:
            if any(root.glob(marker)):
                markers.append(marker)
        elif (root / marker).is_file():
            markers.append(marker)
    return markers


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
            if filename in _MARKER_BUILDERS or Path(filename).suffix in {".sln", ".csproj"}:
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
    return name in _PACKAGE_EXCLUDED or name.startswith(".")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _python_package(root: Path) -> str:
    """Best-effort top-level package name for an import assertion.

    Looks for a directory holding ``__init__.py`` (root then ``src/``),
    skipping conventional non-package dirs; falls back to the sanitised
    project directory name. This is candidate data for printing only.
    """
    for base in (root, root / "src"):
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name in _PACKAGE_EXCLUDED:
                continue
            if (child / "__init__.py").is_file():
                return child.name
    return root.name.replace("-", "_") or "package"


def _build_python(root: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
    pkg = _python_package(root)
    venv_python = root / venv_python_subpath()
    env_spec: dict[str, Any] = {
        "assertions": [
            {"import": pkg, "path_under": "{checkout}"},
            {"command_exists": "ruff"},
            {"command_exists": "pytest"},
        ],
    }
    if venv_python.is_file():
        env_spec = {"python": f"{{checkout}}/{venv_python_subpath()}", **env_spec}
    commands = {
        "lint": {"run": "ruff check .", "env": "py"},
        "test": {"run": "pytest -q", "env": "py"},
    }
    return "py", env_spec, commands


def _build_node(root: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
    env_spec: dict[str, Any] = {
        "assertions": [
            {"command_exists": "node"},
            {"command_exists": "npm"},
        ],
    }
    commands = {
        "node_test": {"run": "npm test", "env": "node"},
        "node_typecheck": {"run": "npm run typecheck", "env": "node"},
    }
    return "node", env_spec, commands


def _build_php(root: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
    env_spec: dict[str, Any] = {
        "assertions": [
            {"command_exists": "php"},
            {"file_exists": "vendor/bin/phpunit"},
        ],
    }
    commands = {
        "php_test": {"run": "vendor/bin/phpunit", "env": "php"},
    }
    return "php", env_spec, commands


def _build_go(root: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
    env_spec: dict[str, Any] = {
        "assertions": [
            {"command_exists": "go"},
        ],
    }
    commands = {
        "go_test": {"run": "go test ./...", "env": "go"},
    }
    return "go", env_spec, commands


def _build_rust(root: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
    env_spec: dict[str, Any] = {
        "assertions": [
            {"command_exists": "cargo"},
        ],
    }
    commands = {
        "rust_test": {"run": "cargo test", "env": "rust"},
    }
    return "rust", env_spec, commands


def _build_dotnet(root: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
    env_spec: dict[str, Any] = {
        "assertions": [
            {"command_exists": "dotnet"},
        ],
    }
    commands: dict[str, dict[str, Any]] = {
        "dotnet_build": {"run": "dotnet build", "env": "dotnet"},
        "dotnet_test": {"run": "dotnet test", "env": "dotnet"},
    }
    if (root / "libs").is_dir():
        env_spec["assertions"].append({"path_exists": "libs"})
        commands["worktree_bootstrap_hint"] = {
            "note": "local ignored dependencies detected; consider worktree_bootstrap",
            "worktree_bootstrap": [{"copy": "libs"}],
        }
    return "dotnet", env_spec, commands


_MARKER_BUILDERS = {
    "pyproject.toml": _build_python,
    "package.json": _build_node,
    "composer.json": _build_php,
    "go.mod": _build_go,
    "Cargo.toml": _build_rust,
    "*.sln": _build_dotnet,
    "*.csproj": _build_dotnet,
}
