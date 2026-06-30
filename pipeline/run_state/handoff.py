"""Pure active phase-handoff transition writers for a run's state mapping.

The *active* (non-terminal) counterpart to :mod:`pipeline.run_state.terminal`.
Where ``terminal.py`` owns the field-level mutation that flips a run to a
settled status (``done`` / ``halted`` / ``failed`` / ``interrupted``), this
module owns the field-level mutation for the transitions that keep a run
*alive* across a phase-handoff pause: requesting a handoff
(``status='awaiting_phase_handoff'`` + active payload) and resolving one to
``continue`` / ``continue_with_waiver`` / ``retry_feedback``
(``status='running'`` + cleared payload).

Two shapes of state, two return contracts:

- The ``status`` field and the active ``phase_handoff`` payload live on the
  flat top-level mapping (the in-memory ``session`` dict or a ``meta.json``
  body). Those are mutated **in place**, exactly like ``terminal.py``.
- The ``phase_handoff_override`` / ``phase_handoff_waiver`` /
  ``human_feedback`` markers live on a *separate* object (``state.extras`` /
  the session under different keys). A mutator here cannot reach across to
  that object, so the transition functions **return** those dicts (wrapped in
  a :class:`HandoffTransition`) for the caller to place.

Halt is **not** here. Halt is terminal: it is owned exclusively by
:func:`pipeline.run_state.terminal.mark_run_halted`. Duplicating the
status-to-``halted`` write here would create a second source of the post-halt
shape that could drift from the one ``repair_run_state`` heals to.

These helpers do **no** file IO, spawn **no** subprocess, call **no**
provider, render **no** prompt, and print nothing: persistence, events,
checkpoint status, and the round dispatch stay with the caller. Package
discipline matches the rest of ``run_state`` — this module depends only on
:mod:`pipeline.run_state.types` and never imports runtime / resume /
finalization paths.
"""
from __future__ import annotations

from typing import Any

from pipeline.run_state.types import (
    HandoffAction,
    HandoffRetryMode,
    HandoffTransition,
)

# ── pure builders (no state mutation) ──────────────────────────────────


def build_handoff_payload(
    *,
    handoff_id: str,
    phase: str,
    handoff_type: str,
    trigger: str,
    verdict: Any,
    approved: Any,
    round_extras_key: str,
    round_n: int,
    loop_max_rounds: int,
    available_actions: Any,
    artifacts: Any,
    last_output: Any,
) -> dict[str, Any]:
    """Build the canonical active ``phase_handoff`` payload dict.

    The single home for the payload shape persisted under
    ``session['phase_handoff']`` / ``meta.phase_handoff`` on a pause. Key
    order is load-bearing for byte-equivalent on-disk snapshots and matches
    the historical inline construction exactly.
    """
    return {
        "id":                 handoff_id,
        "phase":              phase,
        "type":               handoff_type,
        "trigger":            trigger,
        "verdict":            verdict,
        "approved":           approved,
        "round_extras_key":   round_extras_key,
        "round":              round_n,
        "loop_max_rounds":    loop_max_rounds,
        "available_actions":  list(available_actions),
        "artifacts":          dict(artifacts),
        "last_output":        last_output,
    }


def build_phase_handoff_override(
    *,
    handoff_id: str,
    action: HandoffAction,
    feedback: str | None,
    note: str | None,
    decided_at: str | None,
) -> dict[str, Any]:
    """Build a ``phase_handoff_override`` marker.

    ``action`` is stored as its plain string value (not the enum member) so
    the dict is byte-identical to the historical literal construction. Key
    order is load-bearing.
    """
    return {
        "handoff_id": handoff_id,
        "action":     action.value,
        "feedback":   feedback,
        "note":       note,
        "decided_at": decided_at,
    }


def build_phase_handoff_waiver(
    *,
    handoff_id: str,
    phase: Any,
    waiver_text: str,
    note: str | None,
    decided_at: str | None,
    findings: Any,
    critique: str,
) -> dict[str, Any]:
    """Build a durable ``phase_handoff_waiver`` record.

    The reviewer verdict is preserved as ``waiver_text`` (the reason the
    waived findings are accepted). Key order is load-bearing.
    """
    return {
        "handoff_id":  handoff_id,
        "phase":       phase,
        "waiver_text": waiver_text,
        "note":        note,
        "decided_at":  decided_at,
        "findings":    findings,
        "critique":    critique,
    }


def build_human_feedback(
    *,
    handoff_id: str,
    feedback: str,
    decided_at: str | None,
) -> dict[str, Any]:
    """Build a ``human_feedback`` extras marker. Key order is load-bearing."""
    return {
        "handoff_id": handoff_id,
        "feedback":   feedback,
        "decided_at": decided_at,
    }


# ── in-place state-mapping mutators ────────────────────────────────────


def request_active_handoff(state: dict[str, Any], *, payload: dict[str, Any]) -> None:
    """Mark ``state`` awaiting a handoff and stamp the active payload.

    Sets ``status='awaiting_phase_handoff'`` and ``state['phase_handoff']``
    to ``payload`` — the only two top-level fields the pause owns. Event
    emission, checkpoint status, and persistence stay with the caller.
    """
    state["status"] = "awaiting_phase_handoff"
    state["phase_handoff"] = payload


def clear_active_handoff(state: dict[str, Any]) -> None:
    """Resolve an active handoff back to ``running`` and clear its payload.

    Sets ``status='running'`` and removes the active ``phase_handoff`` so a
    re-launch without progress will not loop on the same decision. This is
    the shared tail of every non-terminal resume (continue /
    continue_with_waiver / retry_feedback).
    """
    state["status"] = "running"
    state.pop("phase_handoff", None)


# ── canonical active transitions (mutate state + return derived dicts) ──


def continue_handoff(
    state: dict[str, Any],
    *,
    handoff_id: str,
    note: str | None,
    decided_at: str | None,
) -> HandoffTransition:
    """Resolve to a bare ``continue``: clear the payload, no waiver.

    The machine verdict is left untouched (not rewritten to approved); the
    returned override marker (``feedback=None``) is the only record of the
    manual override the caller places onto ``state.extras``.
    """
    clear_active_handoff(state)
    return HandoffTransition(
        override=build_phase_handoff_override(
            handoff_id=handoff_id,
            action=HandoffAction.CONTINUE,
            feedback=None,
            note=note,
            decided_at=decided_at,
        ),
    )


def continue_with_waiver_handoff(
    state: dict[str, Any],
    *,
    handoff_id: str,
    phase: Any,
    feedback: str,
    note: str | None,
    decided_at: str | None,
    findings: Any,
    critique: str,
) -> HandoffTransition:
    """Resolve to ``continue_with_waiver``: clear the payload + durable waiver.

    Like :func:`continue_handoff` for the payload clear, plus a durable
    ``phase_handoff_waiver`` (the operator verdict is the waiver reason) and
    an override whose ``action`` is ``continue_with_waiver``. The caller is
    responsible for rejecting an empty ``feedback`` before calling — the
    waiver must carry a non-empty reason.
    """
    clear_active_handoff(state)
    return HandoffTransition(
        override=build_phase_handoff_override(
            handoff_id=handoff_id,
            action=HandoffAction.CONTINUE_WITH_WAIVER,
            feedback=feedback,
            note=note,
            decided_at=decided_at,
        ),
        waiver=build_phase_handoff_waiver(
            handoff_id=handoff_id,
            phase=phase,
            waiver_text=feedback,
            note=note,
            decided_at=decided_at,
            findings=findings,
            critique=critique,
        ),
    )


def retry_feedback_handoff(
    state: dict[str, Any],
    *,
    handoff_id: str,
    mode: HandoffRetryMode,
    feedback: str,
    note: str | None,
    decided_at: str | None,
) -> HandoffTransition:
    """Resolve to ``retry_feedback``: clear the payload before the retry round.

    Clears the active payload (so a no-progress re-launch will not loop) and
    returns the override, the ``human_feedback`` extras marker, and the typed
    ``retry_mode`` (``PLAN`` vs ``REPAIR``) so the caller can dispatch the
    correct loop **without** parsing the paused phase string. The caller owns
    the actual retry-round dispatch and decides whether to place the
    ``human_feedback`` marker.
    """
    clear_active_handoff(state)
    return HandoffTransition(
        override=build_phase_handoff_override(
            handoff_id=handoff_id,
            action=HandoffAction.RETRY_FEEDBACK,
            feedback=feedback,
            note=note,
            decided_at=decided_at,
        ),
        human_feedback=build_human_feedback(
            handoff_id=handoff_id,
            feedback=feedback,
            decided_at=decided_at,
        ),
        retry_mode=mode,
    )


__all__ = [
    "build_handoff_payload",
    "build_human_feedback",
    "build_phase_handoff_override",
    "build_phase_handoff_waiver",
    "clear_active_handoff",
    "continue_handoff",
    "continue_with_waiver_handoff",
    "request_active_handoff",
    "retry_feedback_handoff",
]
