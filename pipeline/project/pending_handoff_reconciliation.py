"""Reconcile an existing implement handoff with post-phase gate repair.

An implement handler can leave a valid ``incomplete`` handoff before the
``after_phase(implement)`` verification hook runs.  If that hook repairs a
different failure, the original handoff must remain, but its payload must make
the two causes explicit: the verification repair passed and a separate blocker
still prevents implementation completion.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from pipeline.runtime.handoff import PhaseHandoffRequested


@dataclass(frozen=True, slots=True)
class PendingHandoffReconciliation:
    """A surviving handoff annotated with the gates repaired before pausing."""

    signal: PhaseHandoffRequested
    repaired_commands: tuple[str, ...]


def _event_value(event: object, name: str) -> object:
    if isinstance(event, Mapping):
        return event.get(name)
    return getattr(event, name, None)


def _event_decision(event: object) -> str:
    decision = _event_value(event, "decision")
    if decision:
        return str(decision)
    kind = _event_value(event, "kind")
    outcome = _event_value(event, "outcome")
    return {
        ("execution", "fail"): "executed_fail",
        ("execution", "pass"): "executed_pass",
    }.get((kind, outcome), "")


def _repaired_commands(events: Sequence[object]) -> tuple[str, ...]:
    failed: set[str] = set()
    repaired: list[str] = []
    for event in events:
        command = str(_event_value(event, "command") or "").strip()
        decision = _event_decision(event)
        if not command:
            continue
        if decision == "executed_fail":
            failed.add(command)
        elif (
            decision == "executed_pass"
            and command in failed
            and command not in repaired
        ):
            repaired.append(command)
    return tuple(repaired)


def reconcile_pending_handoff_after_gates(
    state: Any,
    *,
    previous_signal: PhaseHandoffRequested | None,
    gate_events: Sequence[object],
) -> PendingHandoffReconciliation | None:
    """Annotate a distinct pending implement handoff after gate repair passes.

    A gate-owned handoff may replace the previous signal when its own repair
    budget is exhausted.  That replacement is left untouched.  Reconciliation
    applies only when the exact same implement/incomplete signal survives a
    fail-then-pass verification repair.
    """
    current = getattr(state, "phase_handoff_request", None)
    if previous_signal is None or current is None:
        return None
    if current.handoff_id != previous_signal.handoff_id:
        return None
    if current.phase != "implement" or current.trigger != "incomplete":
        return None

    repaired_commands = _repaired_commands(gate_events)
    if not repaired_commands:
        return None

    artifacts = dict(current.artifacts)
    artifacts["post_phase_gate_repair"] = {
        "status": "passed",
        "commands": list(repaired_commands),
        "handoff_cause": "separate_remaining_blocker",
    }
    reconciled = replace(current, artifacts=artifacts)
    state.phase_handoff_request = reconciled
    return PendingHandoffReconciliation(
        signal=reconciled,
        repaired_commands=repaired_commands,
    )


__all__ = [
    "PendingHandoffReconciliation",
    "reconcile_pending_handoff_after_gates",
]
