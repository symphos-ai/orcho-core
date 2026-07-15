"""Unit tests for ``render_delivery_outcome`` (ADR 0032 / ADR 0043 T2).

The renderer is a pure function from a ``CommitDeliveryDecision`` to a
sequence of strings: it pushes one or more lines into ``output_fn`` and
returns. Each terminal status prints a framed **delivery banner** — a ruled
box (``═`` × 68) around a bold headline that names the disposition (PULL
REQUEST OPENED / BRANCH PUSHED / COMMITTED TO YOUR CHECKOUT / SKIPPED /
HALTED / FAILED …) so the operator cannot miss what became of the run's work.

These tests build the smallest possible in-memory decision per status,
capture every line via a list-backed ``output_fn``, strip ANSI so colored /
non-colored runs assert identically, and check the full banner verbatim.

Silent statuses — ``disabled``, ``not_applicable``, ``no_diff``, ``pending``
— must produce zero lines (the caller is mid-flow; the next step owns the
user-visible signal).
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from core.io.ansi import C, strip_ansi
from pipeline.engine.commit_delivery import (
    CommitDeliveryDecision,
    _render_delivery_diagnostics,
    _render_published_branch,
    render_delivery_outcome,
)

# The banner rule — kept in sync with ``_DELIVERY_BANNER_WIDTH``.
RULE = "═" * 68
BLANK = ""


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


# ── committed: checkout-commit shape ─────────────────────────────────────


def test_committed_checkout_banner_and_path_block() -> None:
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
        BLANK,
        RULE,
        "  📦  DELIVERY — COMMITTED TO YOUR CHECKOUT",
        RULE,
        "   Commit   abcdef1  deliver: ship feature",
        "   Where    project checkout — working tree changed",
        "   + a.txt",
        "   + b.txt",
        "   + c.txt",
        RULE,
    ]


def test_committed_checkout_dedups_untracked_against_files_staged() -> None:
    """``untracked_delivered`` items already in ``files_staged`` must
    not appear twice in the ``  + <path>`` block."""
    lines, out = _capture()
    decision = _decision(
        action="approve",
        status="committed",
        commit_sha="0123456abcdef",
        final_message="dedup check",
        files_staged=("a.txt", "b.txt"),
        untracked_delivered=("b.txt", "c.txt"),
    )

    render_delivery_outcome(decision, output_fn=out)

    assert lines == [
        BLANK,
        RULE,
        "  📦  DELIVERY — COMMITTED TO YOUR CHECKOUT",
        RULE,
        "   Commit   0123456  dedup check",
        "   Where    project checkout — working tree changed",
        "   + a.txt",
        "   + b.txt",
        "   + c.txt",
        RULE,
    ]


# ── committed: published-branch shape (ADR 0119 / 0121) ──────────────────


def test_committed_published_branch_banner_names_pr_opened() -> None:
    """A pushed delivery branch with an opened PR shows a PULL REQUEST
    OPENED banner carrying the branch and PR URL, and must NOT claim a
    project-checkout commit."""
    lines, out = _capture()
    decision = _decision(
        action="approve",
        status="committed",
        commit_sha=None,
        delivery_branch="orcho/deliver/r1-abc",
        pr_url="https://example.test/pr/45",
        # the typed twin of pr_url — must not be double-printed by the tail.
        delivery_notices=("PR opened: https://example.test/pr/45",),
    )

    render_delivery_outcome(decision, output_fn=out)

    joined = "\n".join(lines)
    assert "📦  DELIVERY — PULL REQUEST OPENED" in joined
    assert "orcho/deliver/r1-abc" in joined
    assert joined.count("https://example.test/pr/45") == 1
    assert "COMMITTED TO YOUR CHECKOUT" not in joined
    assert any("not modified" in line for line in lines)


def test_committed_published_branch_no_pr_does_not_claim_push() -> None:
    """Without a PR URL the durable shape does not prove a remote push."""
    lines, out = _capture()
    decision = _decision(
        action="approve",
        status="committed",
        commit_sha=None,
        delivery_branch="orcho/deliver/r1-abc",
        pr_url=None,
        delivery_warnings=("publish skipped: git provider offline",),
        delivery_notices=(
            "delivery branch orcho/deliver/r1-abc is ready; "
            "open a pull request or push it manually",
        ),
    )

    render_delivery_outcome(decision, output_fn=out)

    joined = "\n".join(lines)
    assert "📦  DELIVERY — DELIVERY BRANCH READY" in joined
    assert "BRANCH PUSHED" not in joined
    assert "(pushed)" not in joined
    assert "COMMITTED TO YOUR CHECKOUT" not in joined
    assert "orcho/deliver/r1-abc" in joined
    assert "push the branch if needed, then open a pull request" in joined
    # the degrade reason from delivery_warnings is surfaced, not swallowed.
    assert "git provider offline" in joined


# ── banner tone (asserted on RAW, un-stripped colour codes) ──────────────
#
# The ANSI-stripped assertions above cannot see colour, so tone regressions
# slip through. These check the raw code on the headline line directly.


def test_no_pr_banner_uses_yellow_needs_attention_tone() -> None:
    """A branch without a PR is needs-attention, without claiming a push."""
    lines: list[str] = []
    decision = _decision(
        action="approve",
        status="committed",
        commit_sha=None,
        delivery_branch="orcho/deliver/r1-abc",
        pr_url=None,
    )

    _render_published_branch(decision, output_fn=lines.append, color=True)

    headline = next(line for line in lines if "DELIVERY BRANCH READY" in line)
    assert C.YELLOW in headline
    assert C.GREEN not in headline


def test_local_delivery_branch_names_checkout_mutation_and_ordered_next_step(
) -> None:
    lines, out = _capture()
    decision = _decision(
        action="approve",
        status="committed",
        commit_sha="e96b71670985",
        final_message="fix: local delivery",
        delivery_branch="orcho/deliver/r1-local",
    )

    render_delivery_outcome(decision, output_fn=out)

    joined = "\n".join(lines)
    assert "COMMITTED TO LOCAL DELIVERY BRANCH" in joined
    assert "e96b716" in joined
    assert "switched to the delivery branch" in joined
    assert "push the branch, then open a pull request if desired" in joined
    assert "untouched" not in joined
    assert "(pushed)" not in joined


def test_pr_opened_banner_uses_green_tone() -> None:
    lines: list[str] = []
    decision = _decision(
        action="approve",
        status="committed",
        commit_sha=None,
        delivery_branch="orcho/deliver/r1-abc",
        pr_url="https://example.test/pr/45",
    )

    _render_published_branch(decision, output_fn=lines.append, color=True)

    headline = next(line for line in lines if "PULL REQUEST OPENED" in line)
    assert C.GREEN in headline
    assert C.YELLOW not in headline


def test_delivery_warning_uses_yellow_needs_attention_tone() -> None:
    """A non-fatal delivery warning (degrade reason / rebase conflict) is a
    needs-attention signal, so its ``⚠`` line is YELLOW — not the default
    terminal color a plain ``bold`` would leave it, where it reads as neutral
    text instead of a warning."""
    lines: list[str] = []
    decision = _decision(
        action="approve",
        status="committed",
        commit_sha=None,
        delivery_branch="orcho/deliver/r1-abc",
        pr_url="https://example.test/pr/45",
        delivery_warnings=(
            "rebase of orcho/deliver/r1-abc onto origin/main conflicted "
            "(src/x.py); published un-rebased",
        ),
    )

    _render_delivery_diagnostics(decision, output_fn=lines.append, color=True)

    warning = next(line for line in lines if "conflicted" in line)
    assert C.YELLOW in warning


# ── other terminal statuses ──────────────────────────────────────────────


def test_applied_uncommitted_banner_path_block_and_help() -> None:
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
        BLANK,
        RULE,
        "  📦  DELIVERY — APPLIED TO CHECKOUT  ·  no commit",
        RULE,
        "   Where   project checkout — commit it manually",
        "   + a.txt",
        "   + b.txt",
        "   Review with: git -C /proj status",
        RULE,
    ]


def test_skipped_banner_uses_run_dir_when_provided() -> None:
    lines, out = _capture()
    decision = _decision(action="skip", status="skipped")

    render_delivery_outcome(decision, output_fn=out, run_dir=Path("/runs/r1"))

    assert lines == [
        BLANK,
        RULE,
        "  📦  DELIVERY — SKIPPED  ·  diff retained",
        RULE,
        "   Diff   /runs/r1",
        RULE,
    ]


def test_halted_banner() -> None:
    lines, out = _capture()
    decision = _decision(action="halt", status="halted")

    render_delivery_outcome(decision, output_fn=out)

    assert lines == [
        BLANK,
        RULE,
        "  📦  DELIVERY — HALTED  ·  nothing delivered",
        RULE,
        "   Reason   commit_decision_halt",
        RULE,
    ]


def test_fix_requested_banner_references_worktree() -> None:
    lines, out = _capture()
    decision = _decision(
        action="fix",
        status="fix_requested",
        source_path=Path("/wt/r1"),
    )

    render_delivery_outcome(decision, output_fn=out)

    assert lines == [
        BLANK,
        RULE,
        "  📦  DELIVERY — CORRECTION FOLLOW-UP REQUESTED",
        RULE,
        "   Worktree   /wt/r1",
        RULE,
    ]


def test_commit_failed_banner_includes_error() -> None:
    lines, out = _capture()
    decision = _decision(
        action="approve",
        status="commit_failed",
        error="pre-commit hook bounced",
    )

    render_delivery_outcome(decision, output_fn=out)

    assert lines == [
        BLANK,
        RULE,
        "  📦  DELIVERY — COMMIT FAILED",
        RULE,
        "   Error   pre-commit hook bounced",
        RULE,
    ]


def test_apply_failed_banner_includes_error() -> None:
    lines, out = _capture()
    decision = _decision(
        action="apply",
        status="apply_failed",
        error="patch did not apply",
    )

    render_delivery_outcome(decision, output_fn=out)

    assert lines == [
        BLANK,
        RULE,
        "  📦  DELIVERY — APPLY FAILED",
        RULE,
        "   Error   patch did not apply",
        RULE,
    ]


def test_target_dirty_banner_shows_first_three_paths() -> None:
    lines, out = _capture()
    decision = _decision(
        action="approve",
        status="target_dirty",
        target_dirty_paths=(" M one.txt", "?? two.txt", " M three.txt"),
    )

    render_delivery_outcome(decision, output_fn=out)

    assert lines == [
        BLANK,
        RULE,
        "  📦  DELIVERY — ABORTED  ·  checkout was dirty",
        RULE,
        "   Dirty    M one.txt, ?? two.txt,  M three.txt",
        RULE,
    ]


def test_target_dirty_banner_truncates_with_ellipsis_when_more_than_three() -> None:
    lines, out = _capture()
    decision = _decision(
        action="approve",
        status="target_dirty",
        target_dirty_paths=(" M a", " M b", " M c", " M d"),
    )

    render_delivery_outcome(decision, output_fn=out)

    assert lines == [
        BLANK,
        RULE,
        "  📦  DELIVERY — ABORTED  ·  checkout was dirty",
        RULE,
        "   Dirty    M a,  M b,  M c...",
        RULE,
    ]


def test_verification_blocked_banner_includes_error() -> None:
    lines, out = _capture()
    decision = _decision(
        action="approve",
        status="verification_blocked",
        error="required receipts missing",
    )

    render_delivery_outcome(decision, output_fn=out)

    assert lines == [
        BLANK,
        RULE,
        "  📦  DELIVERY — BLOCKED  ·  verification incomplete",
        RULE,
        "   Error   required receipts missing",
        RULE,
    ]


# ── silent statuses ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status", ["disabled", "not_applicable", "no_diff", "pending"],
)
def test_silent_statuses_emit_no_output(status: str) -> None:
    lines, out = _capture()
    decision = _decision(status=status)

    render_delivery_outcome(decision, output_fn=out)

    assert lines == []
