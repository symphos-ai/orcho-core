"""pipeline.control.implement_handoff_digest — decision-oriented digest for
an ``implement`` phase handoff paused with ``trigger='incomplete'``.

A ``subtask_dag`` delivery that exhausts its repair budget pauses with a
``PhaseHandoffRequested`` whose ``artifacts`` carry the structured reason the
delivery is incomplete: the still-incomplete subtask ids, their per-subtask
attestation-gate reasons, and the ids that never produced a delivery receipt.
The raw ``last_output`` (the agent's final implementation transcript) is verbose
and buries the decision the operator actually faces.

This module turns those primitives into a compact, decision-first digest:

* a **classifier** (:func:`classify_implement_incomplete`) that reads the signal
  primitives and returns a frozen :class:`ImplementIncompleteDigest` — the
  incomplete subtasks, the unmet criteria (id + reason), the missing receipts,
  whether the gap is a *baseline / pre-existing verification exception* rather
  than real unfinished work, and the recommended next action; and
* a pure **renderer** (:func:`render_implement_incomplete_digest`) that prints a
  ``Why paused`` block (subtasks / unmet criteria first), the verification
  exception note when applicable, and an explicit ``Recommended`` action.

Both are pure: primitives in, strings out. No filesystem / SDK I/O, no import of
``pipeline.project``. Long reasons are collapsed via
:func:`pipeline.control.handoff_banners.sanitize_feedback_preview`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.io.ansi import C, paint
from pipeline.control.handoff_banners import sanitize_feedback_preview

__all__ = [
    "BASELINE_EXCEPTION_MARKERS",
    "ImplementIncompleteDigest",
    "classify_implement_incomplete",
    "render_implement_incomplete_digest",
]

# Action vocabulary (mirrors ``PhaseHandoffAction`` values verbatim so the
# digest never has to translate between the recommendation and the persisted
# ``action`` field).
_CONTINUE_WITH_WAIVER = "continue_with_waiver"
_RETRY_FEEDBACK = "retry_feedback"

#: Conservative, case-insensitive substrings that mark an incomplete delivery as
#: a *verification exception* — the gate did not close because of baseline /
#: pre-existing breakage unrelated to this diff, not because the agent left real
#: work unfinished. Kept deliberately narrow: a false positive would steer the
#: operator toward a waiver when a retry is the correct path, so anything
#: ambiguous falls through to the real-gap branch.
BASELINE_EXCEPTION_MARKERS: tuple[str, ...] = (
    "baseline",
    "pre-existing",
    "preexisting",
    "unrelated to this diff",
    "baseline-identical",
    "baseline also red",
)

_RULE = "─" * 60
_REASON_MAX_LEN = 200


@dataclass(frozen=True, slots=True)
class ImplementIncompleteDigest:
    """Decision-oriented summary of an ``implement`` + ``incomplete`` handoff.

    All fields are derived purely from the signal primitives:

    * ``incomplete_subtasks`` — subtask ids whose criteria never closed;
    * ``unmet_criteria`` — ``(subtask_id, reason)`` pairs from
      ``artifacts['attestation_incomplete']``;
    * ``missing_receipts`` — subtask ids that produced no delivery receipt;
    * ``is_verification_exception`` — the gap matches a conservative
      baseline / pre-existing marker (see :data:`BASELINE_EXCEPTION_MARKERS`);
    * ``recommended_action`` — ``continue_with_waiver`` for a baseline
      exception when that action is available, otherwise ``retry_feedback``
      (and never an action absent from ``available_actions``).
    """

    incomplete_subtasks: tuple[str, ...]
    unmet_criteria: tuple[tuple[str, str], ...]
    missing_receipts: tuple[str, ...]
    is_verification_exception: bool
    recommended_action: str


def _as_str_tuple(value: object) -> tuple[str, ...]:
    """Coerce an artifact list field to a tuple of stripped non-empty strings."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _unmet_criteria(attestation_incomplete: object) -> tuple[tuple[str, str], ...]:
    """Build ``(id, reason)`` pairs from the attestation-incomplete mapping."""
    if not isinstance(attestation_incomplete, Mapping):
        return ()
    return tuple(
        (str(sid), str(reason))
        for sid, reason in attestation_incomplete.items()
    )


def _has_baseline_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in BASELINE_EXCEPTION_MARKERS)


def _detect_verification_exception(
    unmet_criteria: tuple[tuple[str, str], ...], last_output: str,
) -> bool:
    """True when a baseline / pre-existing marker appears in any unmet-criterion
    reason or in the raw ``last_output``."""
    if any(_has_baseline_marker(reason) for _sid, reason in unmet_criteria):
        return True
    return _has_baseline_marker(last_output or "")


def _recommend_action(
    *, is_verification_exception: bool, available: Sequence[str],
) -> str:
    """Pick the next action, never naming one outside ``available``.

    A baseline / pre-existing verification exception is normally accepted with a
    durable waiver, so ``continue_with_waiver`` is recommended when the action is
    offered. A real implementation gap (or a baseline exception without the
    waiver action) recommends ``retry_feedback``.
    """
    available_set = set(available)
    if is_verification_exception and _CONTINUE_WITH_WAIVER in available_set:
        return _CONTINUE_WITH_WAIVER
    if _RETRY_FEEDBACK in available_set:
        return _RETRY_FEEDBACK
    return ""


def classify_implement_incomplete(
    artifacts: Mapping[str, object],
    last_output: str,
    available_actions: Sequence[str],
) -> ImplementIncompleteDigest:
    """Classify an ``implement`` + ``incomplete`` handoff into a digest.

    Pure: ``artifacts`` are the signal's ``artifacts`` mapping, ``last_output``
    the raw implementation transcript, and ``available_actions`` the actions the
    runtime published for this pause. Missing / malformed artifact fields degrade
    to empty tuples rather than raising.
    """
    incomplete_subtasks = _as_str_tuple(artifacts.get("incomplete_subtasks"))
    unmet_criteria = _unmet_criteria(artifacts.get("attestation_incomplete"))
    missing_receipts = _as_str_tuple(artifacts.get("missing_subtask_receipts"))
    is_verification_exception = _detect_verification_exception(
        unmet_criteria, last_output,
    )
    recommended_action = _recommend_action(
        is_verification_exception=is_verification_exception,
        available=available_actions,
    )
    return ImplementIncompleteDigest(
        incomplete_subtasks=incomplete_subtasks,
        unmet_criteria=unmet_criteria,
        missing_receipts=missing_receipts,
        is_verification_exception=is_verification_exception,
        recommended_action=recommended_action,
    )


def _recommendation_line(digest: ImplementIncompleteDigest, *, color: bool | None) -> str:
    """One-line recommendation with the rationale for waiver vs retry."""
    if digest.recommended_action == _CONTINUE_WITH_WAIVER:
        hint = (
            "accept the verification exception with a durable waiver — the "
            "normal path for a baseline / pre-existing gap, not a dirty override"
        )
        action_color = C.GREEN
    elif digest.recommended_action == _RETRY_FEEDBACK:
        hint = "send feedback and retry the incomplete implementation subtasks"
        action_color = C.YELLOW
    else:  # pragma: no cover — no recommendable action published
        return f"  {paint('Recommended', C.CYAN, color=color)}: (no action available)"
    action = paint(digest.recommended_action, action_color, C.BOLD, color=color)
    return f"  {paint('Recommended', C.CYAN, color=color)}: {action} — {hint}"


def render_implement_incomplete_digest(
    digest: ImplementIncompleteDigest, *, color: bool | None = None,
) -> str:
    """Render the decision-first ``Why paused`` digest for ``digest``.

    Order is fixed: the unclosed subtasks and unmet criteria (and any missing
    receipts) come first, then — only for a verification exception — the baseline
    cause note, then the explicit ``Recommended`` action. Pure: returns the block
    as a string; the caller owns the ``print``.
    """
    lines = [f"┌─ {paint('Why paused', C.BOLD, color=color)} {_RULE[:48]}"]
    if digest.incomplete_subtasks:
        lines.append(
            f"  {paint('Subtask', C.CYAN, color=color)}: "
            + ", ".join(digest.incomplete_subtasks)
        )
    for sid, reason in digest.unmet_criteria:
        preview = sanitize_feedback_preview(reason, max_len=_REASON_MAX_LEN)
        lines.append(
            f"  {paint('Missing', C.CYAN, color=color)}: {sid} — {preview}"
        )
    if digest.missing_receipts:
        lines.append(
            f"  {paint('Missing receipts', C.CYAN, color=color)}: "
            + ", ".join(digest.missing_receipts)
        )
    if not digest.incomplete_subtasks and not digest.unmet_criteria \
            and not digest.missing_receipts:
        lines.append(
            f"  {paint('Missing', C.CYAN, color=color)}: "
            "delivery did not close (see raw output below)"
        )
    if digest.is_verification_exception:
        lines.append(
            f"  {paint('Cause', C.YELLOW, color=color)}: "
            + paint(
                "baseline / pre-existing, unrelated to this diff "
                "(verification exception, not unfinished work)",
                C.YELLOW, color=color,
            )
        )
    lines.append(_recommendation_line(digest, color=color))
    lines.append(f"└{_RULE}")
    return "\n".join(lines)
