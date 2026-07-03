# SPDX-License-Identifier: Apache-2.0
"""Unattended CLI policy for pending project phase handoffs.

The interactive and supervisor-controlled paths still park on the existing
``awaiting_phase_handoff`` contract. This module is only for an explicitly
unattended CLI run, where parking for a human decision would leave the run
unable to make progress.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

UNATTENDED_HALT_REASON = "phase_handoff_unattended_halt"


@dataclass(frozen=True, slots=True)
class UnattendedHandoffResolution:
    kind: Literal["continue", "halt"]
    reason: str
    note: str


def resolve_unattended_handoff(
    signal: Any,
    *,
    ci_stop_state: str = "",
    ci_stop_reason: str = "",
) -> UnattendedHandoffResolution:
    """Resolve a pending handoff for CLI ``--no-interactive`` autonomy.

    Advisory handoffs can continue through the ordinary decision-artifact path.
    Handoffs that would sanction scope widening or operator-only waivers halt
    with a stable machine-readable reason instead of synthesizing approval.
    """
    phase = str(getattr(signal, "phase", "") or "")
    trigger = str(getattr(signal, "trigger", "") or "")
    available = tuple(getattr(signal, "available_actions", ()) or ())
    stop_part = _ci_stop_part(ci_stop_state, ci_stop_reason)

    if trigger.startswith("scope_expansion:"):
        return _halt("scope_expansion", phase, trigger, stop_part)
    if phase == "implement":
        return _halt("implement_handoff", phase, trigger, stop_part)
    if "continue" not in available:
        return _halt("continue_unavailable", phase, trigger, stop_part)

    return UnattendedHandoffResolution(
        kind="continue",
        reason="advisory_continue",
        note=(
            "auto-decided by unattended policy "
            f"(trigger={trigger or 'unknown'}{stop_part})"
        ),
    )


def _halt(
    reason: str,
    phase: str,
    trigger: str,
    stop_part: str,
) -> UnattendedHandoffResolution:
    return UnattendedHandoffResolution(
        kind="halt",
        reason=reason,
        note=(
            "auto-halted by unattended policy "
            f"(reason={reason}; phase={phase or 'unknown'}; "
            f"trigger={trigger or 'unknown'}{stop_part})"
        ),
    )


def _ci_stop_part(state: str, reason: str) -> str:
    if state and reason:
        return f"; ci_stop={state}:{reason}"
    if state:
        return f"; ci_stop={state}"
    if reason:
        return f"; ci_stop={reason}"
    return ""


__all__ = [
    "UNATTENDED_HALT_REASON",
    "UnattendedHandoffResolution",
    "resolve_unattended_handoff",
]
