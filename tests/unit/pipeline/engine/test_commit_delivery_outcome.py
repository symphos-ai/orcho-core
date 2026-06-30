"""Unit tests for ``render_delivery_outcome`` (ADR 0032 / ADR 0043 T2).

The renderer is a pure function from a ``CommitDeliveryDecision`` to a
sequence of strings: it pushes one or more lines into ``output_fn`` and
returns. These tests build the smallest possible in-memory decision per
status, capture every line via a list-backed ``output_fn``, strip ANSI
so colored / non-colored runs assert identically, and check the full
first line verbatim.

The 12 statuses split into:

* 4 silent — ``disabled``, ``not_applicable``, ``no_diff``, ``pending`` —
  must produce zero lines (the caller is mid-flow; the next step owns
  the user-visible signal).
* 8 terminal — ``committed``, ``applied_uncommitted``, ``skipped``,
  ``halted``, ``fix_requested``, ``commit_failed``, ``apply_failed``,
  ``target_dirty`` — each prints one structured block.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from core.io.ansi import strip_ansi
from pipeline.engine.commit_delivery import (
    CommitDeliveryDecision,
    render_delivery_outcome,
)


def _decision(**overrides: object) -> CommitDeliveryDecision:
    """Minimal in-memory decision with sane defaults for every field.

    Tests override only the fields a status actually consumes so the
    full-string assertions stay readable.
    """
    base: dict[str, object] = {
        "action": "none",
        "status": "pending",
        "run_id": "r1",
        "decision_id": "r1",
        "project_path": Path("/proj"),
        "source_path": Path("/wt"),
        "baseline_ref": "HEAD",
    }
    base.update(overrides)
    return CommitDeliveryDecision(**base)  # type: ignore[arg-type]


def _capture() -> tuple[list[str], Callable[[str], None]]:
    """Return ``(lines, output_fn)`` so tests can read everything the
    renderer printed without going through stdout."""
    lines: list[str] = []

    def output_fn(text: str) -> None:
        lines.append(strip_ansi(text))

    return lines, output_fn


# ── 8 terminal statuses ─────────────────────────────────────────────────


def test_committed_first_line_and_path_block() -> None:
    lines, out = _capture()
    decision = _decision(
        action="approve",
        status="committed",
        commit_sha="abcdef1234567890",
        final_message="deliver: ship feature\n\nbody",
        files_staged=("a.txt", "b.txt"),
        untracked_delivered=("c.txt",),
    )

    render_delivery_outcome(decision, output_fn=out)

    assert lines == [
        "✓ Committed to project checkout abcdef1: deliver: ship feature",
        "  + a.txt",
        "  + b.txt",
        "  + c.txt",
    ]


def test_applied_uncommitted_first_line_path_block_and_help_line() -> None:
    lines, out = _capture()
    decision = _decision(
        action="apply",
        status="applied_uncommitted",
        project_path=Path("/proj"),
        changed_paths=("a.txt",),
        untracked_delivered=("b.txt",),
    )

    render_delivery_outcome(decision, output_fn=out)

    assert lines == [
        "📥 Applied to project checkout (no commit) — operator will commit manually",
        "  + a.txt",
        "  + b.txt",
        "  Review with: git -C /proj status",
    ]


def test_skipped_uses_run_dir_when_provided() -> None:
    lines, out = _capture()
    decision = _decision(action="skip", status="skipped")

    render_delivery_outcome(decision, output_fn=out, run_dir=Path("/runs/r1"))

    assert lines == [
        "⏭ Delivery skipped — diff retained at /runs/r1",
    ]


def test_halted_first_line() -> None:
    lines, out = _capture()
    decision = _decision(action="halt", status="halted")

    render_delivery_outcome(decision, output_fn=out)

    assert lines == [
        "🛑 Delivery halted — run marked HALTED "
        "(halt_reason=commit_decision_halt)",
    ]


def test_fix_requested_first_line_references_worktree() -> None:
    lines, out = _capture()
    decision = _decision(
        action="fix",
        status="fix_requested",
        source_path=Path("/wt/r1"),
    )

    render_delivery_outcome(decision, output_fn=out)

    assert lines == [
        "🔧 Correction follow-up requested — worktree retained at /wt/r1",
    ]


def test_commit_failed_first_line_includes_error() -> None:
    lines, out = _capture()
    decision = _decision(
        action="approve",
        status="commit_failed",
        error="pre-commit hook bounced",
    )

    render_delivery_outcome(decision, output_fn=out)

    assert lines == [
        "✗ Commit failed: pre-commit hook bounced",
    ]


def test_apply_failed_first_line_includes_error() -> None:
    lines, out = _capture()
    decision = _decision(
        action="apply",
        status="apply_failed",
        error="patch did not apply",
    )

    render_delivery_outcome(decision, output_fn=out)

    assert lines == [
        "✗ Apply failed: patch did not apply",
    ]


def test_target_dirty_first_line_shows_first_three_paths() -> None:
    lines, out = _capture()
    decision = _decision(
        action="approve",
        status="target_dirty",
        target_dirty_paths=(" M one.txt", "?? two.txt", " M three.txt"),
    )

    render_delivery_outcome(decision, output_fn=out)

    assert lines == [
        "⚠ Delivery aborted — project checkout was dirty: "
        " M one.txt, ?? two.txt,  M three.txt",
    ]


def test_target_dirty_truncates_with_ellipsis_when_more_than_three() -> None:
    lines, out = _capture()
    decision = _decision(
        action="approve",
        status="target_dirty",
        target_dirty_paths=(
            " M a", " M b", " M c", " M d",
        ),
    )

    render_delivery_outcome(decision, output_fn=out)

    assert lines == [
        "⚠ Delivery aborted — project checkout was dirty: "
        " M a,  M b,  M c...",
    ]


# ── 4 silent statuses ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status", ["disabled", "not_applicable", "no_diff", "pending"],
)
def test_silent_statuses_emit_no_output(status: str) -> None:
    lines, out = _capture()
    decision = _decision(status=status)

    render_delivery_outcome(decision, output_fn=out)

    assert lines == []


# ── Dedup invariant ─────────────────────────────────────────────────────


def test_committed_path_block_dedups_untracked_against_files_staged() -> None:
    """``untracked_delivered`` items already in ``files_staged`` must
    not appear twice in the ``  + <path>`` block."""
    lines, out = _capture()
    decision = _decision(
        action="approve",
        status="committed",
        commit_sha="0123456abcdef",
        final_message="dedup check",
        # ``b.txt`` is both staged AND in untracked_delivered — staged
        # wins, untracked drops out of the extras list.
        files_staged=("a.txt", "b.txt"),
        untracked_delivered=("b.txt", "c.txt"),
    )

    render_delivery_outcome(decision, output_fn=out)

    assert lines == [
        "✓ Committed to project checkout 0123456: dedup check",
        "  + a.txt",
        "  + b.txt",
        "  + c.txt",
    ]
