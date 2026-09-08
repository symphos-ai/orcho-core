# SPDX-License-Identifier: Apache-2.0
"""The ``Next:`` tail of ``orcho status`` — a rendering of ``RunDiagnosis``.

``next_step_lines`` turns core's typed diagnosis into the operator's next
command. It is a pure projection: every branch keys on a ``CONDITION_*``
constant imported from ``sdk.run_control.diagnosis`` and reads only the
diagnosis fields (``condition`` / ``reason`` / ``recommended_next_action`` /
``recommended_run_id`` / ``available_actions`` / ``handoff_id``). It never
reads run meta or Git and never classifies a run on its own — a condition
this module does not recognise falls back to core's ``reason`` verbatim.

The returned lines carry only the text after the ``Next:`` label; the
``format_status`` renderer owns the label and its color.
"""
from __future__ import annotations

import re
from typing import Any

from sdk.run_control.diagnosis import (
    CONDITION_ACTIVE,
    CONDITION_BLOCKED_WORKTREE,
    CONDITION_CLOSED_BY_FOLLOWUP,
    CONDITION_CORRECTION_FOLLOWUP_REQUIRED,
    CONDITION_DELIVERY_INCONSISTENT,
    CONDITION_NEEDS_DECISION,
    CONDITION_NEEDS_DELIVERY_DECISION,
    CONDITION_RECOVER_VIA_SOURCE_RUN,
    CONDITION_RESUME_INERT_TERMINAL,
    CONDITION_STALLED,
    CONDITION_SUPERSEDED_BY_CHILD,
)
from sdk.run_control.recovery_lineage import (
    ACTION_PLAN_ARTIFACT_CONTINUATION,
    ACTION_RESUME_SOURCE_RUN,
)

# The command in a diagnosis reason, quoted in backticks by core (ADR 0191).
_BACKTICKED = re.compile(r"`([^`]+)`")


def diagnosis_unavailable_line(reason: str) -> str:
    """The ``Next:`` text when ``run_diagnosis`` itself failed."""
    return f"(diagnosis unavailable: {reason})"


def next_step_lines(diagnosis: Any) -> list[str]:
    """Render the operator's next step from a ``RunDiagnosis``.

    Returns the text lines after the ``Next:`` label (the first line is the
    step, later lines are indented detail), or an empty list when there is
    nothing to suggest (``active``).
    """
    condition = diagnosis.condition
    run_id = diagnosis.run_id
    reason = diagnosis.reason or ""

    if condition == CONDITION_ACTIVE:
        return []

    if condition == CONDITION_NEEDS_DELIVERY_DECISION:
        actions = ", ".join(diagnosis.available_actions) or "-"
        return [
            f"decide the parked delivery gate — orcho delivery decide {run_id} <action>",
            f"available actions: {actions}",
            f"details: orcho delivery gate {run_id}",
        ]

    if condition == CONDITION_CORRECTION_FOLLOWUP_REQUIRED:
        return [reason]

    if condition == CONDITION_DELIVERY_INCONSISTENT:
        match = _BACKTICKED.search(reason)
        command = match.group(1) if match else reason
        return [f"record the existing delivery commit — {command}"]

    if condition == CONDITION_NEEDS_DECISION:
        handoff = diagnosis.handoff_id or "?"
        actions = ", ".join(diagnosis.available_actions) or "-"
        return [
            f"decide the pending phase handoff {handoff} (actions: {actions}) "
            f"then orcho run --resume {run_id}",
        ]

    if condition == CONDITION_STALLED:
        return [f"orcho repair-state {run_id}"]

    if condition == CONDITION_RESUME_INERT_TERMINAL:
        return [f"inspect only — orcho evidence {run_id}"]

    if condition == CONDITION_CLOSED_BY_FOLLOWUP:
        superseded = diagnosis.recommended_run_id
        suffix = f" (superseded by {superseded})" if superseded else ""
        return [f"inspect only — orcho evidence {run_id}{suffix}"]

    if condition == CONDITION_SUPERSEDED_BY_CHILD:
        child = diagnosis.recommended_run_id or run_id
        return [f"orcho run --resume {child}"]

    if condition == CONDITION_RECOVER_VIA_SOURCE_RUN:
        target = diagnosis.recommended_run_id or run_id
        if diagnosis.recommended_next_action == ACTION_RESUME_SOURCE_RUN:
            return [f"orcho run --resume {target}"]
        if diagnosis.recommended_next_action == ACTION_PLAN_ARTIFACT_CONTINUATION:
            return [f"orcho run --from-run-plan {target} --project <dir>"]
        return [reason]

    if condition == CONDITION_BLOCKED_WORKTREE:
        return [reason]

    # Residual stop: core sets the condition to the status itself. Core may
    # still rule out a plain resume (a phase interrupted before a resumable
    # checkpoint) and recommend continuing from the persisted plan instead.
    if condition == diagnosis.status and condition:
        if diagnosis.recommended_next_action == ACTION_PLAN_ARTIFACT_CONTINUATION:
            target = diagnosis.recommended_run_id or run_id
            return [f"orcho run --from-run-plan {target} --project <dir>"]
        return [f"orcho run --resume {run_id}"]

    return [reason]
