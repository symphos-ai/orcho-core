"""Tests for the canonical provenance-aware declared write scope."""

from dataclasses import dataclass, field

from pipeline.engine.declared_write_scope import (
    DeclaredWriteOrigin,
    DeclaredWriteOriginKind,
    path_matches_declared_scope,
    resolve_declared_write_scope,
)


@dataclass
class _Subtask:
    id: str
    owned_files: tuple[str, ...] = ()
    allowed_modifications: tuple[str, ...] = ()


@dataclass
class _Plan:
    owned_files: tuple[str, ...] = ()
    allowed_modifications: tuple[str, ...] = ()
    subtasks: tuple[_Subtask, ...] = field(default_factory=tuple)


def test_resolver_deduplicates_patterns_without_losing_origins() -> None:
    scope = resolve_declared_write_scope(
        _Plan(
            owned_files=("src/a.py",),
            allowed_modifications=("src/a.py — generated",),
            subtasks=(
                _Subtask(
                    "one",
                    owned_files=("[one] src/a.py",),
                    allowed_modifications=("src/a.py — fixture",),
                ),
            ),
        ),
        plugin_allowed_modifications=("src/a.py",),
        cross_unit_files=("src/a.py",),
    )
    assert scope.patterns == ("src/a.py",)
    assert scope.rules[0].origins == (
        DeclaredWriteOrigin(DeclaredWriteOriginKind.CROSS_UNIT),
        DeclaredWriteOrigin(DeclaredWriteOriginKind.PLAN_ALLOWANCE),
        DeclaredWriteOrigin(DeclaredWriteOriginKind.PLAN_OWNED),
        DeclaredWriteOrigin(DeclaredWriteOriginKind.PLUGIN_ALLOWANCE),
        DeclaredWriteOrigin(DeclaredWriteOriginKind.SUBTASK_ALLOWANCE, "one"),
        DeclaredWriteOrigin(DeclaredWriteOriginKind.SUBTASK_OWNED, "one"),
    )


def test_resolver_is_empty_for_empty_inputs() -> None:
    assert resolve_declared_write_scope().rules == ()


def test_matching_retains_exact_wildcard_and_directory_prefix_semantics() -> None:
    patterns = ("a.py", "src/*.py", "tests/unit")
    assert path_matches_declared_scope("a.py", patterns)
    assert path_matches_declared_scope("src/b.py", patterns)
    assert path_matches_declared_scope("tests/unit/test_a.py", patterns)
    assert not path_matches_declared_scope("docs/a.md", patterns)
