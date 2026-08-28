"""sdk.fine_tune_probes — per-language candidate probes for fine-tune.

Each probe owns one repo marker (``package.json``, ``go.mod``, ``*.sln``, …)
and turns the marked project into an :class:`EnvCandidate`: a proposed
``verification_env`` plus the commands the project can actually run. The
marker→probe mapping is an ordered registry, so language support is a module
concern, not a branch in the inspection loop: built-ins register here at
import time, and any third-party package can call
:func:`register_marker_probe` to add a new language or override a built-in by
marker name. Registration order doubles as detection order — the first
detected marker's env becomes ``default_env``.

Probes are pure-read: they may open marker files (for example a manifest's
scripts table) but never create or modify anything.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from core.infra.platform import venv_python_subpath

# Conventional non-package directories skipped by package/name heuristics.
_PACKAGE_EXCLUDED = frozenset({
    "tests", "test", "docs", "doc", "examples", "example",
    "build", "dist", ".venv", "node_modules", "__pycache__",
    ".git", ".orcho", "workspace-orchestrator", "runspace",
})


@dataclass(frozen=True, slots=True)
class EnvCandidate:
    """One language's contribution to the candidate verification contract.

    ``alternates`` lists runnable-but-not-proposed commands (for example a
    project's ``test:*`` script ladder) so the printed candidate can surface
    them as commented suggestions without polluting ``commands``.
    """

    env: str
    spec: dict[str, Any]
    commands: dict[str, dict[str, Any]] = field(default_factory=dict)
    alternates: list[dict[str, str]] = field(default_factory=list)


MarkerProbe = Callable[[Path], EnvCandidate]

_MARKER_PROBES: dict[str, MarkerProbe] = {}


def register_marker_probe(marker: str, probe: MarkerProbe) -> None:
    """Register ``probe`` for ``marker`` (a filename or ``*.suffix`` glob).

    Re-registration overrides an existing probe by marker name, mirroring the
    override semantics of the other extension registries.
    """
    _MARKER_PROBES[marker] = probe


def iter_marker_probes() -> tuple[tuple[str, MarkerProbe], ...]:
    """Return ``(marker, probe)`` pairs in registration (detection) order."""
    return tuple(_MARKER_PROBES.items())


def matches_marker_filename(filename: str) -> bool:
    """True when ``filename`` matches any registered marker pattern."""
    return any(fnmatch(filename, marker) for marker in _MARKER_PROBES)


def package_excluded_dirs() -> frozenset[str]:
    """Directory names probes and discovery should skip."""
    return _PACKAGE_EXCLUDED


# ── built-in probes ──────────────────────────────────────────────────────────


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


def _probe_python(root: Path) -> EnvCandidate:
    pkg = _python_package(root)
    venv_python = root / venv_python_subpath()
    spec: dict[str, Any] = {
        "assertions": [
            {"import": pkg, "path_under": "{checkout}"},
            {"command_exists": "ruff"},
            {"command_exists": "pytest"},
        ],
    }
    if venv_python.is_file():
        spec = {"python": f"{{checkout}}/{venv_python_subpath()}", **spec}
    commands = {
        "lint": {"run": "ruff check .", "env": "py"},
        "test": {"run": "pytest -q", "env": "py"},
    }
    return EnvCandidate(env="py", spec=spec, commands=commands)


def _read_package_json(path: Path) -> dict[str, Any] | None:
    """Parse ``package.json``; None when unreadable or not a JSON object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _probe_node(root: Path) -> EnvCandidate:
    spec: dict[str, Any] = {
        "assertions": [
            {"command_exists": "node"},
            {"command_exists": "npm"},
        ],
    }
    manifest = _read_package_json(root / "package.json")
    if manifest is None:
        # Unreadable manifest: fall back to the npm convention only.
        return EnvCandidate(
            env="node",
            spec=spec,
            commands={"node_test": {"run": "npm test", "env": "node"}},
        )

    scripts = manifest.get("scripts")
    scripts = scripts if isinstance(scripts, dict) else {}
    commands: dict[str, dict[str, Any]] = {}
    if "test" in scripts:
        commands["node_test"] = {"run": "npm test", "env": "node"}
    if "lint" in scripts:
        commands["node_lint"] = {"run": "npm run lint", "env": "node"}
    if "typecheck" in scripts:
        commands["node_typecheck"] = {"run": "npm run typecheck", "env": "node"}
    elif _has_dependency(manifest, "typescript"):
        commands["node_typecheck"] = {"run": "npx tsc --noEmit", "env": "node"}

    alternates = [
        {"name": name, "run": f"npm run {name}", "env": "node"}
        for name in sorted(scripts)
        if name.startswith("test:")
    ]
    return EnvCandidate(env="node", spec=spec, commands=commands, alternates=alternates)


def _has_dependency(manifest: dict[str, Any], name: str) -> bool:
    for key in ("devDependencies", "dependencies"):
        deps = manifest.get(key)
        if isinstance(deps, dict) and name in deps:
            return True
    return False


def _probe_php(root: Path) -> EnvCandidate:
    spec: dict[str, Any] = {
        "assertions": [
            {"command_exists": "php"},
            {"file_exists": "vendor/bin/phpunit"},
        ],
    }
    commands = {
        "php_test": {"run": "vendor/bin/phpunit", "env": "php"},
    }
    return EnvCandidate(env="php", spec=spec, commands=commands)


def _probe_go(root: Path) -> EnvCandidate:
    spec: dict[str, Any] = {
        "assertions": [
            {"command_exists": "go"},
        ],
    }
    commands = {
        "go_test": {"run": "go test ./...", "env": "go"},
    }
    return EnvCandidate(env="go", spec=spec, commands=commands)


def _probe_rust(root: Path) -> EnvCandidate:
    spec: dict[str, Any] = {
        "assertions": [
            {"command_exists": "cargo"},
        ],
    }
    commands = {
        "rust_test": {"run": "cargo test", "env": "rust"},
    }
    return EnvCandidate(env="rust", spec=spec, commands=commands)


def _probe_dotnet(root: Path) -> EnvCandidate:
    spec: dict[str, Any] = {
        "assertions": [
            {"command_exists": "dotnet"},
        ],
    }
    commands: dict[str, dict[str, Any]] = {
        "dotnet_build": {"run": "dotnet build", "env": "dotnet"},
        "dotnet_test": {"run": "dotnet test", "env": "dotnet"},
    }
    if (root / "libs").is_dir():
        spec["assertions"].append({"path_exists": "libs"})
        commands["worktree_bootstrap_hint"] = {
            "note": "local ignored dependencies detected; consider worktree_bootstrap",
            "worktree_bootstrap": [{"copy": "libs"}],
        }
    return EnvCandidate(env="dotnet", spec=spec, commands=commands)


register_marker_probe("pyproject.toml", _probe_python)
register_marker_probe("package.json", _probe_node)
register_marker_probe("composer.json", _probe_php)
register_marker_probe("go.mod", _probe_go)
register_marker_probe("Cargo.toml", _probe_rust)
register_marker_probe("*.sln", _probe_dotnet)
register_marker_probe("*.csproj", _probe_dotnet)
