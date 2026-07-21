"""Pure, trigger-first classification for phase-handoff resumes."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

HandoffRoute = Literal[
    "verification_retry", "scope_expansion", "implement_incomplete",
    "review_retry", "plan_retry", "blocked",
]


@dataclass(frozen=True, slots=True)
class GateIdentity:
    command: str
    hook: str
    phase: str


@dataclass(frozen=True, slots=True)
class HandoffRouteResolution:
    route: HandoffRoute
    gate_identity: GateIdentity | None = None
    blocker: str | None = None


def classify_handoff_route(
    active: Mapping[str, object], *, ledger_identities: Sequence[GateIdentity] = (),
) -> HandoffRouteResolution:
    """Classify a handoff without inferring its meaning from phase alone.

    ``verification_gate_failed`` is intentionally first: verification may fail
    at ``final_acceptance`` but is never a scope-expansion sanction.
    """
    trigger = active.get("trigger")
    phase = active.get("phase")
    if not isinstance(phase, str):
        return HandoffRouteResolution("blocked", blocker="handoff lacks phase")
    # Historical plan/review/implement payloads predate durable triggers. Their
    # phase shapes are unambiguous; only verification and scope expansion demand
    # trigger evidence because phase=final_acceptance is otherwise ambiguous.
    if not isinstance(trigger, str):
        if phase == "implement":
            return HandoffRouteResolution("implement_incomplete")
        if phase == "review_changes":
            return HandoffRouteResolution("review_retry")
        if phase in {"plan", "validate_plan"}:
            return HandoffRouteResolution("plan_retry")
        return HandoffRouteResolution("blocked", blocker="handoff lacks trigger")
    if trigger == "verification_gate_failed":
        identity = _gate_identity(active, ledger_identities)
        if identity is None:
            return HandoffRouteResolution(
                "blocked", blocker="verification handoff has ambiguous gate identity",
            )
        return HandoffRouteResolution("verification_retry", gate_identity=identity)
    if trigger.startswith("scope_expansion:"):
        if phase != "final_acceptance":
            return HandoffRouteResolution(
                "blocked", blocker="scope-expansion trigger requires final_acceptance phase",
            )
        return HandoffRouteResolution("scope_expansion")
    if phase == "implement" and trigger == "incomplete":
        return HandoffRouteResolution("implement_incomplete")
    if phase == "review_changes":
        return HandoffRouteResolution("review_retry")
    if phase in {"plan", "validate_plan"}:
        return HandoffRouteResolution("plan_retry")
    return HandoffRouteResolution("blocked", blocker="unsupported trigger/phase handoff combination")


def _gate_identity(
    active: Mapping[str, object], ledger_identities: Sequence[GateIdentity],
) -> GateIdentity | None:
    artifacts = active.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return None
    raw = artifacts.get("gate_identity")
    if isinstance(raw, Mapping):
        command, hook, phase = raw.get("command"), raw.get("hook"), raw.get("phase")
        if (
            isinstance(command, str) and command
            and isinstance(hook, str) and hook
            and isinstance(phase, str)
        ):
            return GateIdentity(command, hook, phase)
        return None
    # Legacy handoffs lacked hook/phase. Recover only where the durable ledger
    # supplies exactly one identity for their recorded command.
    command = artifacts.get("gate_command")
    if not isinstance(command, str) or not command:
        return None
    candidates = [identity for identity in ledger_identities if identity.command == command]
    return candidates[0] if len(candidates) == 1 else None


__all__ = ["GateIdentity", "HandoffRouteResolution", "classify_handoff_route"]
