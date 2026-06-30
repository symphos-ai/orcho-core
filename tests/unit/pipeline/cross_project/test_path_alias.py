"""Unit tests for ``pipeline.cross_project.path_alias.aliasize_plan_paths``."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.cross_project.path_alias import aliasize_plan_paths


@pytest.fixture
def projects() -> dict[str, Path]:
    return {
        "api": Path("/Users/op/www/demo/api"),
        "web": Path("/Users/op/www/demo/web"),
    }


def test_rewrites_path_continuation(projects: dict[str, Path]) -> None:
    text = "see /Users/op/www/demo/api/server/handlers.py for the bug"
    out = aliasize_plan_paths(text, projects)
    assert out == "see [api]/server/handlers.py for the bug"


def test_rewrites_bare_root_at_boundary(projects: dict[str, Path]) -> None:
    text = "project root: /Users/op/www/demo/api."
    out = aliasize_plan_paths(text, projects)
    assert out == "project root: [api]."


def test_does_not_eat_adjacent_identifier(projects: dict[str, Path]) -> None:
    # ``/Users/op/www/demo/api`` must NOT match inside a longer token
    # like ``/Users/op/www/demo/apiserver``.
    text = "stray: /Users/op/www/demo/apiserver/foo"
    out = aliasize_plan_paths(text, projects)
    assert out == "stray: /Users/op/www/demo/apiserver/foo"


def test_handles_multiple_aliases(projects: dict[str, Path]) -> None:
    text = (
        "[api]/old should not double; touch "
        "/Users/op/www/demo/api/x.py and /Users/op/www/demo/web/y.ts"
    )
    out = aliasize_plan_paths(text, projects)
    assert "/Users/op/www/demo/api/x.py" not in out
    assert "/Users/op/www/demo/web/y.ts" not in out
    assert "[api]/x.py" in out
    assert "[web]/y.ts" in out


def test_nested_roots_match_longest_first() -> None:
    projects = {
        "parent": Path("/ws/api"),
        "child":  Path("/ws/api/sub"),
    }
    text = "edit /ws/api/sub/file.py and /ws/api/other.py"
    out = aliasize_plan_paths(text, projects)
    assert "[child]/file.py" in out
    assert "[parent]/other.py" in out
    # The child path must not bleed into a [parent]/sub/... fallback.
    assert "[parent]/sub/file.py" not in out


def test_idempotent_on_already_aliased(projects: dict[str, Path]) -> None:
    text = "see [api]/server/handlers.py — already aliased"
    out = aliasize_plan_paths(text, projects)
    assert out == text


def test_empty_inputs() -> None:
    assert aliasize_plan_paths("", {"api": Path("/x")}) == ""
    assert aliasize_plan_paths("plain text", {}) == "plain text"


def test_trailing_slash_root_normalised() -> None:
    projects = {"api": Path("/ws/api/")}
    text = "edit /ws/api/server.py"
    out = aliasize_plan_paths(text, projects)
    assert out == "edit [api]/server.py"


# ── Windows path tests ────────────────────────────────────────────────
#
# The cross CLI is supported on Windows where ``str(Path("C:\\ws\\api"))``
# produces backslash form. The rewrite must catch both:
#   * Backslash form (what the prompt to the agent carried, and what a
#     Windows agent might echo back literally).
#   * Forward-slash form (what an agent conventionally writes in
#     markdown regardless of host OS).
# Output is always forward-slash ``[alias]/...`` so the alias-prefixed
# form is portable across platforms.
#
# These tests pass strings rather than ``Path`` objects so they run on
# POSIX CI without instantiating ``WindowsPath``. The function's
# ``str(Path(p))`` call preserves backslashes on POSIX because the
# whole string is treated as a single component.


def test_windows_backslash_continuation_rewritten() -> None:
    projects = {"api": Path("C:\\ws\\api")}
    text = "see C:\\ws\\api\\server\\handlers.py for the bug"
    out = aliasize_plan_paths(text, projects)
    assert "C:\\ws\\api" not in out, (
        "Windows root must not leak into rewritten text; "
        f"got {out!r}"
    )
    assert "[api]/server\\handlers.py" in out, (
        f"continuation rewrite missed; got {out!r}"
    )


def test_windows_forward_slash_form_rewritten() -> None:
    # Agent conventionally emits forward slashes in markdown even on
    # Windows; the function must match this form against the
    # backslash-rooted project entry.
    projects = {"api": Path("C:\\ws\\api")}
    text = "see C:/ws/api/server/handlers.py for the bug"
    out = aliasize_plan_paths(text, projects)
    assert out == "see [api]/server/handlers.py for the bug"


def test_windows_bare_root_rewritten_at_boundary() -> None:
    projects = {"api": Path("C:\\ws\\api")}
    text = "project root: C:\\ws\\api."
    out = aliasize_plan_paths(text, projects)
    assert out == "project root: [api]."


def test_windows_does_not_eat_adjacent_identifier() -> None:
    # Same prefix discipline as POSIX: ``C:\\ws\\api`` must NOT match
    # inside a longer token like ``C:\\ws\\apiserver``.
    projects = {"api": Path("C:\\ws\\api")}
    text = "stray: C:\\ws\\apiserver\\foo"
    out = aliasize_plan_paths(text, projects)
    assert "[api]" not in out
    assert "C:\\ws\\apiserver\\foo" in out


def test_windows_nested_roots_match_longest_first() -> None:
    projects = {
        "parent": Path("C:\\ws\\api"),
        "child":  Path("C:\\ws\\api\\sub"),
    }
    text = (
        "edit C:\\ws\\api\\sub\\file.py and C:\\ws\\api\\other.py"
    )
    out = aliasize_plan_paths(text, projects)
    # Tail components keep their original separator after rewrite —
    # rewrite normalises the prefix only.
    assert "[child]/file.py" in out
    assert "[parent]/other.py" in out
    # The child path must not bleed into a [parent]/sub/... fallback.
    assert "[parent]/sub" not in out


def test_windows_trailing_backslash_root_normalised() -> None:
    projects = {"api": Path("C:\\ws\\api\\")}
    text = "edit C:\\ws\\api\\server.py"
    out = aliasize_plan_paths(text, projects)
    assert out == "edit [api]/server.py"


def test_windows_mixed_slash_idempotent() -> None:
    projects = {"api": Path("C:\\ws\\api")}
    # Already-aliased text passes through unchanged regardless of
    # how an upstream pass mixed slashes — the bracket form does not
    # match the absolute prefix grammar.
    text = "see [api]/server/handlers.py and [api]\\other.py"
    out = aliasize_plan_paths(text, projects)
    assert out == text
