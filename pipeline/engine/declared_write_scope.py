"""Canonical, provenance-aware declared write scope (pure).

This module is the single owner of declaration normalisation and matching.
It deliberately accepts duck-typed plans so it can be used at the engine
boundary without coupling to parser or plugin implementation types.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# Stable engine-owned state.extras key for the canonical run write scope.
DECLARED_WRITE_SCOPE_EXTRAS_KEY = "declared_write_scope"


class DeclaredWriteOriginKind(StrEnum):
    """The durable source that declared a writable pattern."""

    PLAN_OWNED = "plan_owned"
    SUBTASK_OWNED = "subtask_owned"
    PLAN_ALLOWANCE = "plan_allowance"
    SUBTASK_ALLOWANCE = "subtask_allowance"
    CROSS_UNIT = "cross_unit"
    PLUGIN_ALLOWANCE = "plugin_allowance"


@dataclass(frozen=True, slots=True)
class DeclaredWriteOrigin:
    """One source declaration, including its subtask identity when applicable."""

    kind: DeclaredWriteOriginKind
    subtask_id: str | None = None


@dataclass(frozen=True, slots=True)
class DeclaredWriteRule:
    """One canonical pattern and every source which declared it."""

    pattern: str
    origins: tuple[DeclaredWriteOrigin, ...]


@dataclass(frozen=True, slots=True)
class DeclaredWriteScope:
    """Immutable canonical write scope with legacy-compatible matching."""

    rules: tuple[DeclaredWriteRule, ...] = ()

    @property
    def patterns(self) -> tuple[str, ...]:
        return tuple(rule.pattern for rule in self.rules)

    def matches(self, relative_path: str) -> bool:
        """Whether ``relative_path`` matches an exact, glob, or directory rule."""
        return path_matches_declared_scope(relative_path, self.patterns)


# Strip repeated leading ``[subtask-id]`` tags, retaining the historic syntax.
_LEADING_TAG_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)+")
# The whitespace requirement protects path hyphens such as package-lock.json.
_REASON_SPLIT_RE = re.compile(r"\s+[—–-]\s+")


def extract_declared_pattern(entry: Any) -> str:
    """Return the bare pattern from a tagged or ``pattern — reason`` declaration."""
    if not isinstance(entry, str):
        return ""
    text = _LEADING_TAG_RE.sub("", entry).strip()
    if not text:
        return ""
    text = _REASON_SPLIT_RE.split(text, maxsplit=1)[0].strip()
    return text.split()[0] if text else ""


def path_matches_declared_scope(relative_path: str, patterns: Sequence[str]) -> bool:
    """Apply the established exact/wildcard/directory-prefix scope grammar."""
    for raw in patterns:
        pattern = str(raw).strip().rstrip("/")
        if not pattern:
            continue
        if pattern in ("**", "*"):
            return True
        if fnmatch.fnmatch(relative_path, pattern):
            return True
        base = pattern.rstrip("*").rstrip("/")
        if base and (relative_path == base or relative_path.startswith(base + "/")):
            return True
    return False


def resolve_declared_write_scope(
    plan: Any = None,
    plugin_allowed_modifications: Sequence[str] | None = None,
    cross_unit_files: Sequence[str] | None = None,
    *,
    project_allowed_modifications: Sequence[str] | None = None,
) -> DeclaredWriteScope:
    """Resolve all durable declarations into deterministic, provenance-rich rules.

    ``project_allowed_modifications`` is a spelling alias for callers that use
    the PluginConfig field's project terminology. Supplying both allowance
    arguments is intentionally additive.
    """
    gathered: dict[str, set[DeclaredWriteOrigin]] = {}

    def add(entries: Sequence[str] | None, origin: DeclaredWriteOrigin) -> None:
        for entry in entries or ():
            pattern = extract_declared_pattern(entry)
            if pattern:
                gathered.setdefault(pattern, set()).add(origin)

    if plan is not None:
        add(getattr(plan, "owned_files", None), DeclaredWriteOrigin(DeclaredWriteOriginKind.PLAN_OWNED))
        add(getattr(plan, "allowed_modifications", None), DeclaredWriteOrigin(DeclaredWriteOriginKind.PLAN_ALLOWANCE))
        for subtask in getattr(plan, "subtasks", None) or ():
            identifier = getattr(subtask, "id", None)
            subtask_id = identifier if isinstance(identifier, str) and identifier else None
            add(getattr(subtask, "owned_files", None), DeclaredWriteOrigin(DeclaredWriteOriginKind.SUBTASK_OWNED, subtask_id))
            add(getattr(subtask, "allowed_modifications", None), DeclaredWriteOrigin(DeclaredWriteOriginKind.SUBTASK_ALLOWANCE, subtask_id))

    add(cross_unit_files, DeclaredWriteOrigin(DeclaredWriteOriginKind.CROSS_UNIT))
    add(plugin_allowed_modifications, DeclaredWriteOrigin(DeclaredWriteOriginKind.PLUGIN_ALLOWANCE))
    add(project_allowed_modifications, DeclaredWriteOrigin(DeclaredWriteOriginKind.PLUGIN_ALLOWANCE))

    return DeclaredWriteScope(
        rules=tuple(
            DeclaredWriteRule(
                pattern=pattern,
                origins=tuple(sorted(origins, key=lambda origin: (origin.kind.value, origin.subtask_id or ""))),
            )
            for pattern, origins in sorted(gathered.items())
        )
    )


__all__ = [
    "DECLARED_WRITE_SCOPE_EXTRAS_KEY",
    "DeclaredWriteOrigin",
    "DeclaredWriteOriginKind",
    "DeclaredWriteRule",
    "DeclaredWriteScope",
    "extract_declared_pattern",
    "path_matches_declared_scope",
    "resolve_declared_write_scope",
]
