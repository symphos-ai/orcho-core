"""pipeline.control.handoff_labels — operator-facing round labels.

A handoff loop has two kinds of round:

* **Automatic rounds** consume the loop's auto-retry budget. Their
  number is always ``<= loop_max_rounds`` by construction, so the
  fraction ``round/loop_max_rounds`` is well-formed.

* **Human-directed retry rounds** are the one-shot ``retry_feedback``
  rounds the operator injects on top of ``LoopStep.max_rounds`` (see
  :data:`pipeline.runtime.handoff.HUMAN_DIRECTED_ROUNDS_KEY`). Their
  ``round`` is ``loop_max_rounds + K`` for the ``K``-th human retry, so
  rendering them as ``round/loop_max_rounds`` produces an impossible
  fraction like ``round 2/1``.

This module owns the single coherent render of both kinds so no
operator surface ever prints ``N/M`` with ``N > M``. The durable
payload fields (``signal.round`` / ``loop_max_rounds``) are unchanged —
this is presentation only.

The public API (:func:`render_round_label`,
:func:`is_human_directed_round`, :func:`human_directed_flag_from_state`)
is the contract consumed by the prompt summary, the pause warning, and
the dispatch banner; it is also the label contract reused by later
resume / banner surfaces.
"""

from __future__ import annotations

from typing import Any

from pipeline.runtime.handoff import HUMAN_DIRECTED_FLAG_KEY

__all__ = [
    "human_directed_flag_from_state",
    "is_human_directed_round",
    "render_round_label",
]


def is_human_directed_round(
    *,
    round: int,
    loop_max_rounds: int,
    human_directed_flag: bool = False,
) -> bool:
    """Return whether this round is a human-directed retry round.

    A round is human-directed when either the loop driver flagged it as
    such (``human_directed_flag`` — sourced from
    :data:`pipeline.runtime.handoff.HUMAN_DIRECTED_FLAG_KEY` or a
    ``retry_feedback`` override) **or** structurally when ``round``
    exceeds ``loop_max_rounds``. The structural check is the backstop
    that guarantees the impossible-fraction invariant even when no
    explicit flag reached the surface.
    """
    return bool(human_directed_flag) or round > loop_max_rounds


def render_round_label(
    *,
    phase: str,
    round: int,
    loop_max_rounds: int,
    human_directed: bool = False,
    rejected_again: bool = False,
) -> str:
    """Render the operator-facing round label for ``phase``.

    Three coherent shapes, never an ``N/M`` fraction with ``N > M``:

    * automatic round → ``"<phase> automatic round R/M"``;
    * human-directed retry → ``"<phase> human retry K after REJECTED
      verdict"``;
    * human-directed retry that was rejected again →
      ``"<phase> human retry K rejected; operator decision required"``.

    ``K`` is the human-retry ordinal (``round - loop_max_rounds``,
    floored at 1). ``rejected_again`` is the caller's classification
    that the human-directed retry itself produced a rejected verdict and
    a fresh pause — only meaningful for a human-directed round.
    """
    human = is_human_directed_round(
        round=round,
        loop_max_rounds=loop_max_rounds,
        human_directed_flag=human_directed,
    )
    if not human:
        # Automatic round: round <= loop_max_rounds holds by
        # construction; clamp defensively so a stray overflow can never
        # render an impossible fraction here.
        shown = round if round <= loop_max_rounds else loop_max_rounds
        return f"{phase} automatic round {shown}/{loop_max_rounds}"

    retry_k = max(1, round - loop_max_rounds)
    if rejected_again:
        return (
            f"{phase} human retry {retry_k} rejected; "
            f"operator decision required"
        )
    return f"{phase} human retry {retry_k} after REJECTED verdict"


def human_directed_flag_from_state(state: Any) -> bool:
    """Read the human-directed marker off ``state.extras``.

    True when the per-round loop flag
    (:data:`pipeline.runtime.handoff.HUMAN_DIRECTED_FLAG_KEY`) is set, or
    when the active ``phase_handoff_override`` records a
    ``retry_feedback`` action (the resume that injects the one-shot
    human-directed round). Tolerant of a missing / non-dict ``extras``.
    """
    extras = getattr(state, "extras", None)
    if not isinstance(extras, dict):
        return False
    if extras.get(HUMAN_DIRECTED_FLAG_KEY):
        return True
    override = extras.get("phase_handoff_override")
    return isinstance(override, dict) and override.get("action") == "retry_feedback"
