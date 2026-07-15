"""Pure execution-eligibility algebra for normalized verification identities.

This module deliberately classifies only *eligibility*. It has no external
state inputs; routers will adopt this single result in a later slice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ExecutionPolicy = Literal["manual", "suggest", "warn", "require"]
ExecutionHook = Literal[
    "before_phase", "after_phase", "before_delivery", "manual_only", "on_resume",
]
Executor = Literal["engine", "operator"]
Trigger = Literal["before_phase", "after_phase", "pre_final", "operator", "on_resume"]
BaseConsequence = Literal["none", "warning", "required_action"]

_POLICIES: frozenset[str] = frozenset({"manual", "suggest", "warn", "require"})
_HOOKS: frozenset[str] = frozenset({
    "before_phase", "after_phase", "before_delivery", "manual_only", "on_resume",
})


class ExecutionEligibilityError(ValueError):
    """Raised when a normalized policy/hook identity is not executable."""


@dataclass(frozen=True)
class VerificationIdentity:
    """One selected verification identity; command, hook, and phase never collapse."""

    command: str
    hook: ExecutionHook
    phase: str
    policy: ExecutionPolicy


@dataclass(frozen=True)
class ExecutionEligibility:
    """Executor, trigger, and base consequence for one selected identity.

    ``trigger`` retains the timing vocabulary while mapping the delivery boundary
    to ``pre_final``. ``phase`` is preserved unchanged so phase-scoped identities
    remain distinguishable without consulting a schedule again.
    """

    selected: bool
    executor: Executor | None
    trigger: Trigger
    phase: str
    consequence: BaseConsequence


@dataclass(frozen=True)
class ResolvedExecution:
    """A complete selected identity together with its pure execution decision."""

    identity: VerificationIdentity
    eligibility: ExecutionEligibility

    @property
    def executor(self) -> Executor | None:
        return self.eligibility.executor

    @property
    def consequence(self) -> BaseConsequence:
        return self.eligibility.consequence


def resolve_selected_execution(
    identity: VerificationIdentity,
    *,
    selected: bool = True,
) -> ResolvedExecution:
    """Attach the canonical eligibility decision to a complete identity."""
    return ResolvedExecution(
        identity=identity,
        eligibility=resolve_execution_eligibility(
            selected,
            identity.policy,
            identity.hook,
            identity.phase,
        ),
    )


def resolve_execution_eligibility(
    selected: bool,
    policy: ExecutionPolicy,
    hook: ExecutionHook,
    phase: str,
) -> ExecutionEligibility:
    """Resolve the ADR 0132 policy × hook execution matrix without side effects.

    Invalid values and the forbidden ``warn``/``require`` + ``manual_only`` rows
    fail before selection is considered.  An unselected valid identity never has
    an executor or a blocking consequence.
    """
    if policy not in _POLICIES:
        raise ExecutionEligibilityError(f"unknown execution policy {policy!r}")
    if hook not in _HOOKS:
        raise ExecutionEligibilityError(f"unknown execution hook {hook!r}")
    if not isinstance(selected, bool):
        raise ExecutionEligibilityError("selected must be bool")
    if not isinstance(phase, str):
        raise ExecutionEligibilityError("phase must be str")
    if hook in ("before_phase", "after_phase") and not phase:
        raise ExecutionEligibilityError(f"{hook} requires a non-empty phase")
    if hook not in ("before_phase", "after_phase") and phase:
        raise ExecutionEligibilityError(f"{hook} does not accept a phase")
    if hook == "manual_only" and policy in ("warn", "require"):
        raise ExecutionEligibilityError(
            f"manual_only only accepts manual or suggest policy, got {policy!r}",
        )

    trigger = _trigger_for(hook, policy)
    if not selected:
        return ExecutionEligibility(False, None, trigger, phase, "none")
    if policy in ("manual", "suggest"):
        return ExecutionEligibility(True, "operator", trigger, phase, "none")
    if policy == "warn":
        return ExecutionEligibility(True, "engine", trigger, phase, "warning")
    return ExecutionEligibility(True, "engine", trigger, phase, "required_action")


def _trigger_for(hook: ExecutionHook, policy: ExecutionPolicy) -> Trigger:
    """Map a normalized identity to its stable execution trigger."""
    if hook == "before_delivery":
        return "pre_final"
    if hook == "manual_only":
        return "operator"
    if hook == "on_resume":
        return "on_resume"
    if policy in ("manual", "suggest"):
        return "operator"
    return hook
