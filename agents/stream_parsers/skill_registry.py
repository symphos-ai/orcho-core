"""Helpers for recognizing skill-use prose in provider streams."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

_SKILL_ROOT_NAMES = (".agents/skills", ".claude/skills")
_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "workspace-orchestrator",
}
_MAX_DEPTH = 4
_ACTIVE_SKILL_NAMES: ContextVar[frozenset[str]] = ContextVar(
    "orcho_active_skill_names",
    default=frozenset(),
)


@contextmanager
def active_registered_skill_names(skill_names: frozenset[str]) -> Iterator[None]:
    """Expose runtime-visible skill names to stream parsers in this call."""
    token = _ACTIVE_SKILL_NAMES.set(skill_names)
    try:
        yield
    finally:
        _ACTIVE_SKILL_NAMES.reset(token)


def current_registered_skill_names() -> frozenset[str]:
    """Return skill names active for the current provider stream."""
    return _ACTIVE_SKILL_NAMES.get()


@lru_cache(maxsize=128)
def discover_registered_skill_names(cwd: str | None) -> frozenset[str]:
    """Find skill package names visible near a runtime working directory.

    The stream parser only sees provider JSONL, not the project plugin object.
    For transcript telemetry, a bounded filesystem scan is a cheap proxy for
    the registered skill registry: a skill name is accepted only when a
    matching ``SKILL.md`` exists under a canonical skill root near ``cwd``.
    """
    if not cwd:
        return frozenset()
    try:
        root = Path(cwd).resolve()
    except OSError:
        return frozenset()
    if not root.exists():
        return frozenset()

    names: set[str] = set()
    _collect_at(root, names)
    _walk(root, names, depth=0)
    return frozenset(names)


def _collect_at(base: Path, names: set[str]) -> None:
    for relative in _SKILL_ROOT_NAMES:
        skill_root = base / relative
        if not skill_root.is_dir():
            continue
        try:
            children = list(skill_root.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and (child / "SKILL.md").is_file():
                names.add(child.name)


def _walk(base: Path, names: set[str], *, depth: int) -> None:
    if depth >= _MAX_DEPTH:
        return
    try:
        children = list(base.iterdir())
    except OSError:
        return
    for child in children:
        if not child.is_dir() or child.name in _SKIP_DIRS:
            continue
        _collect_at(child, names)
        _walk(child, names, depth=depth + 1)
