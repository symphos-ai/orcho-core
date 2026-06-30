# SPDX-License-Identifier: Apache-2.0
"""Lock the canonical run-status vocabulary (values + consumer identity).

``pipeline.run_state.status_vocab`` is the single home for the named status
sets that drive resume / terminal classification. These tests pin the exact
membership of each set (a silent classification change must fail) and assert
that every consumer references the *same object* — so a regression that
reintroduces a local ``frozenset`` duplicate breaks identity and trips here.
"""
from __future__ import annotations

from pipeline.run_state import status_vocab

# ── Value pins ──────────────────────────────────────────────────────────────


def test_terminal_success_statuses_exact():
    assert frozenset({"done", "success", "completed"}) == (
        status_vocab.TERMINAL_SUCCESS_STATUSES
    )


def test_resumable_terminal_statuses_exact():
    assert frozenset({"halted", "failed", "interrupted"}) == (
        status_vocab.RESUMABLE_TERMINAL_STATUSES
    )


def test_failure_terminal_statuses_exact():
    assert frozenset({"failed", "halted", "interrupted"}) == (
        status_vocab.FAILURE_TERMINAL_STATUSES
    )


def test_terminal_cross_statuses_exact():
    assert frozenset({"done", "failed", "halted", "cancelled"}) == (
        status_vocab.TERMINAL_CROSS_STATUSES
    )


def test_all_sets_are_frozensets():
    for name in (
        "TERMINAL_SUCCESS_STATUSES",
        "RESUMABLE_TERMINAL_STATUSES",
        "FAILURE_TERMINAL_STATUSES",
        "TERMINAL_CROSS_STATUSES",
    ):
        assert isinstance(getattr(status_vocab, name), frozenset)


def test_failure_and_resumable_are_distinct_names():
    # They currently share members but are semantically different concepts;
    # they must remain two separately-named objects so they can diverge.
    assert (
        status_vocab.FAILURE_TERMINAL_STATUSES
        is not status_vocab.RESUMABLE_TERMINAL_STATUSES
    )


# ── Consumer identity (no duplicate frozensets) ─────────────────────────────


def test_resume_context_uses_canonical_success_set():
    from pipeline.control import resume_context

    assert (
        resume_context.TERMINAL_SUCCESS_STATUSES
        is status_vocab.TERMINAL_SUCCESS_STATUSES
    )


def test_sdk_actions_uses_canonical_sets():
    from sdk import actions

    assert (
        actions.TERMINAL_SUCCESS_STATUSES is status_vocab.TERMINAL_SUCCESS_STATUSES
    )
    assert (
        actions.RESUMABLE_TERMINAL_STATUSES
        is status_vocab.RESUMABLE_TERMINAL_STATUSES
    )


def test_setup_failure_uses_canonical_failure_set():
    from pipeline.run_state import setup_failure

    assert (
        setup_failure.FAILURE_TERMINAL_STATUSES
        is status_vocab.FAILURE_TERMINAL_STATUSES
    )


def test_cross_uses_canonical_cross_set():
    from pipeline.run_state import cross

    assert cross.TERMINAL_CROSS_STATUSES is status_vocab.TERMINAL_CROSS_STATUSES


def test_status_vocab_is_a_leaf_no_sdk_or_runtime_import():
    # Layer invariant: run_state.status_vocab must not pull sdk/runtime. Parse
    # the actual import statements (not the prose) so the docstring may name
    # the forbidden layers without tripping the guard.
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(status_vocab))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    for mod in imported:
        root = mod.split(".", 1)[0]
        assert root not in {"sdk"}, f"status_vocab must not import sdk: {mod}"
        assert "runtime" not in mod, f"status_vocab must not import runtime: {mod}"
