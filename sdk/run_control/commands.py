"""Operator-decision command model for phase handoffs.

This module builds and validates a :class:`PhaseHandoffDecisionCommand`
from an observed :class:`PendingOperatorAction`. It is pure data +
validation: it never calls :func:`sdk.phase_handoff.phase_handoff_decide`,
never writes to disk, and never spawns a process. Execution is the sole
job of ``phase_handoff_decide``; ``command.to_decide_kwargs()`` adapts the
DTO to that executor's keyword arguments.

The command DTO itself (:class:`PhaseHandoffDecisionCommand`) lives in
:mod:`sdk.run_control.types` (its single home) and is re-exported here for
discoverability.

Gate boundary (Stage 4, first half): gate decisions go through
``core.resolve_gate_decision`` with ``run`` / ``skip`` choices and do not
reduce to ``phase_handoff_decide``. A typed gate command is intentionally
out of scope here and will get its own adapter in the second half; the
pending gate stays observable in the read model
(:class:`PendingOperatorAction` with ``kind='gate'``), so this builder
rejects gate inputs with a clear error rather than coercing them.
"""
from __future__ import annotations

from sdk.phase_handoff import PhaseHandoffActionValue, _validate_feedback
from sdk.run_control.types import PendingOperatorAction, PhaseHandoffDecisionCommand

__all__ = ["PhaseHandoffDecisionCommand", "build_decision_command"]


def build_decision_command(
    pending: PendingOperatorAction,
    action: PhaseHandoffActionValue,
    *,
    feedback: str | None = None,
    note: str | None = None,
) -> PhaseHandoffDecisionCommand:
    """Build a validated phase-handoff decision command.

    Validates that ``pending`` is a phase-handoff pause, that ``action`` is
    one of the runtime-published ``available_actions`` (the only sanctioned
    source of allowed verbs), and that feedback-required actions carry a
    non-empty ``feedback`` — reusing
    :data:`sdk.phase_handoff._FEEDBACK_REQUIRED_ACTIONS` and
    :func:`sdk.phase_handoff._validate_feedback` rather than restating the
    rule.

    Raises:
        ValueError: ``pending.kind`` is ``'gate'`` (out of scope, see the
            module docstring) or any non-handoff kind; ``action`` is not in
            ``pending.available_actions``; ``pending`` lacks a
            ``handoff_id``; or a feedback-required action has empty
            feedback.
    """
    if pending.kind == "gate":
        raise ValueError(
            "build_decision_command: gate decisions are out of scope for "
            "the first half of Stage 4. A gate pause resolves through "
            "core.resolve_gate_decision (run / skip choices), not "
            "phase_handoff_decide; it stays observable in the read model "
            "but has no command adapter yet."
        )
    if pending.kind != "phase_handoff":
        raise ValueError(
            f"build_decision_command: unsupported pending kind "
            f"{pending.kind!r}; only 'phase_handoff' produces a decision "
            "command."
        )
    if action not in pending.available_actions:
        raise ValueError(
            f"build_decision_command: action {action!r} is not in the "
            f"runtime-published available_actions "
            f"{list(pending.available_actions)!r}. available_actions is the "
            "only sanctioned source of allowed handoff verbs."
        )
    if pending.handoff_id is None:
        raise ValueError(
            "build_decision_command: pending action has no handoff_id; "
            "cannot address a decision without it."
        )
    # Reuse the canonical feedback-required rule (retry_feedback /
    # continue_with_waiver need non-empty feedback): _validate_feedback owns
    # _FEEDBACK_REQUIRED_ACTIONS, so the list is not restated here.
    _validate_feedback(action, feedback)

    return PhaseHandoffDecisionCommand(
        run_id=pending.run_id,
        handoff_id=pending.handoff_id,
        action=action,
        feedback=feedback,
        note=note,
    )
