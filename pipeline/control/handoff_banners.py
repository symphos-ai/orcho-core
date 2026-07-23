"""pipeline.control.handoff_banners — operator banners for retry_feedback.

A ``retry_feedback`` decision runs exactly one human-directed round. These
banners make the transition legible:

* **before** — what is about to run: run id, handoff id, the originally
  rejected phase, the ``retry_feedback`` action, whether this is a plan or
  a repair retry, a sanitized one-line preview of the operator feedback,
  and whether the provider session resumes or starts fresh;
* **after** — the outcome: ``approved`` (handoff closed, remaining phases
  continue), ``rejected_again`` (the retry did not satisfy the reviewer —
  the run pauses for a new operator decision), or ``provider_fallback``
  (provider-session resume missed but the fresh-session fallback succeeded
  on persisted run context).

Pure render helpers return strings; the ``print_*`` wrappers are the only
I/O (a thin ``print`` to a stream). Round labels come from
:mod:`pipeline.control.handoff_labels`.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
from typing import TextIO

from core.io.ansi import C, paint
from pipeline.control.handoff_labels import render_round_label

__all__ = [
    "RetryOutcome",
    "print_retry_feedback_banner",
    "print_retry_outcome_banner",
    "render_advice_summary",
    "render_retry_feedback_banner",
    "render_retry_outcome_banner",
    "sanitize_feedback_preview",
]

_PREVIEW_MAX_LEN = 200
_ADVICE_SUMMARY_MAX_LEN = 240
_RULE = "─" * 60

_RETRY_KIND_LABEL: dict[str, str] = {
    "plan": "plan → validate_plan retry",
    "repair": "repair_changes → review_changes retry",
}


class RetryOutcome(Enum):
    """Outcome of a human-directed ``retry_feedback`` round."""

    APPROVED = "approved"
    REJECTED_AGAIN = "rejected_again"
    PROVIDER_FALLBACK = "provider_fallback"


def sanitize_feedback_preview(
    feedback: str, *, max_len: int = _PREVIEW_MAX_LEN,
) -> str:
    """Collapse a (possibly multi-line) operator verdict to one safe line.

    Replaces non-printable characters with a space, collapses any run of
    whitespace / newlines to a single space, and truncates to ``max_len``
    with an ellipsis. Keeps the banner to one tidy line and never leaks
    raw control bytes into the terminal.
    """
    if not feedback:
        return "(none)"
    cleaned = "".join(
        ch if (ch.isprintable() or ch == " ") else " " for ch in feedback
    )
    collapsed = " ".join(cleaned.split())
    if not collapsed:
        return "(none)"
    if len(collapsed) > max_len:
        return collapsed[: max_len - 1].rstrip() + "…"
    return collapsed


def render_advice_summary(
    *,
    recommended_action: str,
    confidence: str,
    rationale: str,
    retry_feedback_preview: str,
    risks: Sequence[str] = (),
    expected_files: Sequence[str] = (),
    operator_note: str = "",
    disposition: str = "",
    conflict_details: Sequence[str] = (),
    color: bool | None = None,
) -> str:
    """Render a compact one-block summary of an advisor recommendation.

    Input is primitives only — the caller (the advice follow-up prompt) passes
    the parsed advice fields, never the raw reviewer output, so this never
    duplicates the full review transcript. ``rationale`` /
    ``retry_feedback_preview`` and the list fields are collapsed to a single
    line and truncated via :func:`sanitize_feedback_preview`.
    """
    rec = recommended_action or "(none)"
    conf = confidence or "(none)"
    lines = [
        f"┌─ {paint('advice', C.CYAN, C.BOLD, color=color)} {_RULE[:48]}",
        f"  {paint('recommended', C.CYAN, color=color)} : "
        f"{paint(rec, C.YELLOW, C.BOLD, color=color)} "
        f"({paint('confidence', C.CYAN, color=color)}: "
        f"{paint(conf, C.GREEN, C.BOLD, color=color)})",
        f"  {paint('rationale', C.CYAN, color=color)}   : "
        + sanitize_feedback_preview(rationale, max_len=_ADVICE_SUMMARY_MAX_LEN),
        f"  {paint('feedback', C.CYAN, color=color)}    : "
        + sanitize_feedback_preview(
            retry_feedback_preview, max_len=_ADVICE_SUMMARY_MAX_LEN,
        ),
    ]
    if disposition:
        lines.append(f"  {paint('disposition', C.CYAN, color=color)} : {disposition}")
    if conflict_details:
        lines.append(f"  {paint('conflicts', C.YELLOW, color=color)}   : " + sanitize_feedback_preview("; ".join(conflict_details), max_len=_ADVICE_SUMMARY_MAX_LEN))
    if risks:
        lines.append(
            f"  {paint('risks', C.YELLOW, color=color)}       : "
            + paint(sanitize_feedback_preview(
                "; ".join(risks), max_len=_ADVICE_SUMMARY_MAX_LEN,
            ), C.YELLOW, color=color)
        )
    if expected_files:
        lines.append(
            f"  {paint('files', C.CYAN, color=color)}       : "
            + paint(sanitize_feedback_preview(
                ", ".join(expected_files), max_len=_ADVICE_SUMMARY_MAX_LEN,
            ), C.GREY, color=color)
        )
    if operator_note:
        lines.append(
            f"  {paint('note', C.CYAN, color=color)}        : "
            + paint(sanitize_feedback_preview(
                operator_note, max_len=_ADVICE_SUMMARY_MAX_LEN,
            ), C.GREY, color=color)
        )
    lines.append(f"└{_RULE}")
    return "\n".join(lines)


def _render_worktree_subject_line(
    worktree_subject: str | None, *, worktree_isolated: bool,
) -> str:
    """Describe the worktree the retry repairs in.

    A review-retry repairs the *retained rejected diff subject* — the exact
    isolated worktree the reviewer saw (``retained retry subject``). When the
    run had isolation off there is no retained worktree; the retry edits the
    source checkout in place (``in-place checkout``). The subject is decided
    by the persisted ``meta.worktree`` block, independent of whether the
    provider session resumes or falls back to fresh, so this line never
    changes with ``resume_provider_session``.
    """
    if not worktree_subject:
        return "(not recorded)"
    if worktree_isolated:
        return f"retained retry subject {worktree_subject}"
    return f"in-place checkout {worktree_subject}"


def render_retry_feedback_banner(
    *,
    run_id: str,
    handoff_id: str,
    rejected_phase: str,
    retry_kind: str,
    retry_round: int,
    loop_max_rounds: int,
    feedback: str,
    resume_provider_session: bool,
    worktree_subject: str | None = None,
    worktree_isolated: bool = True,
) -> str:
    """Render the pre-retry banner (see module docstring for fields).

    ``worktree_subject`` is the path the repair round runs in (the retained
    isolated worktree, or the in-place checkout when ``worktree_isolated`` is
    False). It distinguishes the *change subject* from the *provider session*:
    a fresh-session fallback never moves the worktree, so the two lines are
    reported independently and in every provider-session combination.
    """
    # Summary mode: a two-line handoff card via the presenter. The retry is
    # a rejected-phase handoff being executed with operator feedback, so the
    # head carries the REJECTED verdict and the action line the feedback.
    # live/debug fall through to the full multi-line banner below.
    from core.observability.logging import get_output_mode
    if get_output_mode() == "summary":
        from core.io import summary_lines
        head = summary_lines.handoff_line(handoff_id, rejected_phase, "REJECTED")
        action = summary_lines.handoff_action_line(
            "retry_feedback", feedback or None,
        )
        return f"{head}\n  {action}"
    kind_label = _RETRY_KIND_LABEL.get(retry_kind, f"{retry_kind} retry")
    round_label = render_round_label(
        phase=rejected_phase,
        round=retry_round,
        loop_max_rounds=loop_max_rounds,
        human_directed=True,
    )
    session_line = (
        "resume (falls back to a fresh session on miss)"
        if resume_provider_session
        else "fresh session (persisted run context preserved)"
    )
    worktree_line = _render_worktree_subject_line(
        worktree_subject, worktree_isolated=worktree_isolated,
    )
    return "\n".join([
        f"┌─ retry_feedback {_RULE[:42]}",
        f"  run             : {run_id}",
        f"  handoff         : {handoff_id}",
        f"  rejected phase  : {rejected_phase}",
        f"  action          : retry_feedback ({kind_label})",
        f"  round           : {round_label}",
        f"  provider session: {session_line}",
        f"  worktree        : {worktree_line}",
        f"  feedback        : {sanitize_feedback_preview(feedback)}",
        f"└{_RULE}",
    ])


def render_retry_outcome_banner(
    *,
    run_id: str,
    handoff_id: str,
    rejected_phase: str,
    outcome: RetryOutcome,
) -> str:
    """Render the post-retry banner for ``outcome``."""
    # Summary mode: a two-line handoff card via the presenter — the head
    # carries the outcome verdict, the action line the consequence note.
    # live/debug fall through to the full multi-line banner below.
    from core.observability.logging import get_output_mode
    if get_output_mode() == "summary":
        from core.io import summary_lines
        note = {
            RetryOutcome.APPROVED: "handoff closed; run continues",
            RetryOutcome.REJECTED_AGAIN: "run parks awaiting_phase_handoff",
            RetryOutcome.PROVIDER_FALLBACK: "fresh provider session; run continues",
        }[outcome]
        head = summary_lines.handoff_line(
            handoff_id, rejected_phase, outcome.value.upper(),
        )
        action = summary_lines.handoff_action_line(outcome.value, note=note)
        return f"{head}\n  {action}"
    if outcome is RetryOutcome.APPROVED:
        body = (
            "approved — handoff closed; the run continues with the "
            "remaining phases."
        )
    elif outcome is RetryOutcome.REJECTED_AGAIN:
        body = (
            "rejected again — the retry did not satisfy the reviewer; the "
            "run is paused for a new operator decision."
        )
    else:  # PROVIDER_FALLBACK
        body = (
            "provider-session resume was unavailable; the retry ran on a "
            "fresh provider session with persisted run context."
        )
    return "\n".join([
        f"┌─ retry_feedback result: {outcome.value} {_RULE[:24]}",
        f"  run     : {run_id}",
        f"  handoff : {handoff_id}",
        f"  phase   : {rejected_phase}",
        f"  outcome : {body}",
        f"└{_RULE}",
    ])


def print_retry_feedback_banner(
    *, out: TextIO | None = None, **fields: object,
) -> None:
    """Print the pre-retry banner to ``out`` (default stdout)."""
    print(render_retry_feedback_banner(**fields), file=out)  # type: ignore[arg-type]


def print_retry_outcome_banner(
    *, out: TextIO | None = None, **fields: object,
) -> None:
    """Print the post-retry banner to ``out`` (default stdout)."""
    print(render_retry_outcome_banner(**fields), file=out)  # type: ignore[arg-type]
