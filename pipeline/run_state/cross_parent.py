"""Pure, canonical reduction of a cross parent's durable facts.

This module deliberately has no adapter dependencies.  Runtime and disk readers
must turn their observations into :class:`CrossParentFacts`; this reducer is the
one place that gives those observations cross-run meaning.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pipeline.run_state.status_vocab import (
    FAILURE_TERMINAL_STATUSES,
    PAUSE_STATUS,
    TERMINAL_SUCCESS_STATUSES,
)


class Observation(StrEnum):
    MISSING = "missing"
    PRESENT = "present"
    MALFORMED = "malformed"


class ChildExecution(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    TERMINAL = "terminal"
    INCONSISTENT = "inconsistent"


class ReleaseDisposition(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"


class ParentClass(StrEnum):
    RUNNING = "running"
    AWAITING_OPERATOR = "awaiting_operator"
    BLOCKED = "blocked"
    READY = "ready"
    TERMINAL_SUCCESS = "terminal_success"
    TERMINAL_FAILURE = "terminal_failure"
    TERMINAL_HALTED = "terminal_halted"
    INCONSISTENT = "inconsistent"


class TerminalDisposition(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class PhaseIdentity:
    phase: str
    alias: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduledGateIdentity:
    """Stable engine-owned gate identity; command is intentionally ordered."""

    phase: str
    hook: str
    command: tuple[str, ...]
    alias: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", tuple(self.command))


@dataclass(frozen=True, slots=True)
class ActiveOperation:
    """An active normal phase or scheduled gate, in adapter-observed order."""

    phase: PhaseIdentity | None = None
    gate: ScheduledGateIdentity | None = None

    def __post_init__(self) -> None:
        if (self.phase is None) == (self.gate is None):
            raise ValueError("an active operation has exactly one identity")


@dataclass(frozen=True, slots=True)
class PendingDecision:
    handoff_id: str
    kind: str
    available_actions: tuple[str, ...]
    alias: str | None = None
    parent_handoff_id: str | None = None
    child_handoff_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "available_actions", tuple(self.available_actions))


@dataclass(frozen=True, slots=True)
class CheckpointHandoff:
    pending: bool = False
    kind: str | None = None
    alias: str | None = None
    parent_handoff_id: str | None = None
    child_handoff_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChildFacts:
    """Adapter-normalized child facts; no mapping or mutable collection leaks in."""

    alias: str
    physical: Observation = Observation.MISSING
    physical_status: str | None = None
    embedded: Observation = Observation.MISSING
    embedded_status: str | None = None
    halt_reason: str | None = None
    release_verdict: str | None = None
    release_ship_ready: bool | None = None
    pending_decision: PendingDecision | None = None
    active_operations: tuple[ActiveOperation, ...] = ()
    checkpoint_sub_status: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "active_operations", tuple(self.active_operations))


@dataclass(frozen=True, slots=True)
class CrossParentFacts:
    declared_aliases: tuple[str, ...]
    children: tuple[ChildFacts, ...] = ()
    parent_status: str | None = None
    active_operations: tuple[ActiveOperation, ...] = ()
    pending_decision: PendingDecision | None = None
    checkpoint_handoff: CheckpointHandoff = CheckpointHandoff()

    def __post_init__(self) -> None:
        object.__setattr__(self, "declared_aliases", tuple(self.declared_aliases))
        object.__setattr__(self, "children", tuple(self.children))
        object.__setattr__(self, "active_operations", tuple(self.active_operations))


@dataclass(frozen=True, slots=True)
class ConsistencyViolation:
    code: str
    alias: str | None = None


@dataclass(frozen=True, slots=True)
class ChildBlocker:
    code: str
    alias: str


@dataclass(frozen=True, slots=True)
class ChildState:
    alias: str
    execution: ChildExecution
    status: str | None
    halt_reason: str | None
    active_operations: tuple[ActiveOperation, ...]
    contract_evaluable: bool
    release_disposition: ReleaseDisposition
    release_ready: bool
    pending_decision: PendingDecision | None
    blockers: tuple[ChildBlocker, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "active_operations", tuple(self.active_operations))
        object.__setattr__(self, "blockers", tuple(self.blockers))


@dataclass(frozen=True, slots=True)
class CrossParentState:
    children: tuple[ChildState, ...]
    active_operations: tuple[ActiveOperation, ...]
    pending_decision: PendingDecision | None
    blockers: tuple[ChildBlocker, ...]
    violations: tuple[ConsistencyViolation, ...]
    parent_class: ParentClass
    terminal_disposition: TerminalDisposition | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "children", tuple(self.children))
        object.__setattr__(self, "active_operations", tuple(self.active_operations))
        object.__setattr__(self, "blockers", tuple(self.blockers))
        object.__setattr__(self, "violations", tuple(self.violations))


def classify_child_outcome(facts: ChildFacts) -> tuple[str, str]:
    """Return ADR-0146 dispatch classification from canonical child facts.

    This is intentionally small so a future dispatch consumer can use the
    same authority rules without importing its current session-shaped helper.
    """
    status = facts.physical_status
    if facts.physical is Observation.MISSING:
        return "failure", "physical_missing"
    if facts.physical is Observation.MALFORMED:
        return "failure", "physical_malformed"
    if not isinstance(status, str):
        return "failure", "status_missing_or_invalid"
    if status in TERMINAL_SUCCESS_STATUSES:
        return "success", f"status:{status}"
    if _is_rejected_release(facts):
        return "release_rejected", f"status:{status}:{facts.halt_reason}"
    if status == PAUSE_STATUS and facts.pending_decision is not None:
        return "pause", f"status:{status}"
    if status in FAILURE_TERMINAL_STATUSES:
        return "failure", f"status:{status}"
    return "failure", f"status_unknown:{status}"


def reduce_cross_parent_state(facts: CrossParentFacts) -> CrossParentState:
    """Reduce immutable facts without I/O, mutation, or policy-specific CFA logic."""
    violations: list[ConsistencyViolation] = []
    declared_aliases = tuple(dict.fromkeys(facts.declared_aliases))
    if len(declared_aliases) != len(facts.declared_aliases):
        violations.append(ConsistencyViolation("declared_alias_duplicate"))

    indexed: dict[str, ChildFacts] = {}
    for child in facts.children:
        if child.alias not in declared_aliases:
            violations.append(ConsistencyViolation("undeclared_child", child.alias))
        elif child.alias in indexed:
            violations.append(ConsistencyViolation("duplicate_child_facts", child.alias))
        else:
            indexed[child.alias] = child

    children: list[ChildState] = []
    for alias in declared_aliases:
        child, child_violations = _reduce_child(indexed.get(alias, ChildFacts(alias)))
        children.append(child)
        violations.extend(child_violations)

    operations = facts.active_operations + tuple(
        operation for child in children for operation in child.active_operations
    )
    pending = facts.pending_decision or next(
        (child.pending_decision for child in children if child.pending_decision), None
    )
    violations.extend(_pending_decision_violations(facts.pending_decision, children))
    violations.extend(_handoff_violations(pending, facts.checkpoint_handoff))
    if pending is not None and operations:
        violations.append(ConsistencyViolation("pending_decision_with_active_operation"))

    blockers = tuple(blocker for child in children for blocker in child.blockers)
    terminal = _terminal_disposition(facts.parent_status)
    # A failed or halted parent is compatible with a failed/halted child (and
    # a rejected child release).  Only terminal success promises that every
    # child is terminal and release-ready.  Active work or a pending operator
    # decision is contradictory for every terminal parent disposition.
    terminal_contradiction = bool(operations or pending is not None)
    if terminal is TerminalDisposition.SUCCESS:
        terminal_contradiction |= any(
            child.execution
            in {
                ChildExecution.PENDING,
                ChildExecution.RUNNING,
                ChildExecution.PAUSED,
                ChildExecution.INCONSISTENT,
            }
            or not child.release_ready
            for child in children
        )
    if terminal is not None and terminal_contradiction:
        violations.append(ConsistencyViolation("terminal_parent_contradiction"))

    if violations:
        parent_class = ParentClass.INCONSISTENT
        terminal = None
    elif operations:
        parent_class = ParentClass.RUNNING
    elif pending is not None:
        parent_class = ParentClass.AWAITING_OPERATOR
    elif terminal is TerminalDisposition.SUCCESS:
        parent_class = ParentClass.TERMINAL_SUCCESS
    elif terminal is TerminalDisposition.FAILURE:
        parent_class = ParentClass.TERMINAL_FAILURE
    elif terminal is TerminalDisposition.HALTED:
        parent_class = ParentClass.TERMINAL_HALTED
    elif blockers:
        parent_class = ParentClass.BLOCKED
    else:
        parent_class = ParentClass.READY
    return CrossParentState(
        tuple(children), operations, pending, blockers, tuple(violations), parent_class, terminal
    )


def _reduce_child(facts: ChildFacts) -> tuple[ChildState, tuple[ConsistencyViolation, ...]]:
    violations: list[ConsistencyViolation] = []
    if (
        facts.physical is Observation.PRESENT
        and facts.embedded is Observation.PRESENT
        and facts.physical_status != facts.embedded_status
    ):
        violations.append(ConsistencyViolation("embedded_physical_status_conflict", facts.alias))
    if facts.physical is Observation.MALFORMED:
        violations.append(ConsistencyViolation("physical_child_malformed", facts.alias))
    if facts.embedded is Observation.MALFORMED:
        violations.append(ConsistencyViolation("embedded_child_malformed", facts.alias))
    if facts.physical is Observation.MISSING and facts.embedded is Observation.PRESENT:
        # An in-memory child snapshot is useful live-call context, but cannot
        # stand in for a missing durable child outcome.
        violations.append(ConsistencyViolation("embedded_without_physical", facts.alias))

    kind, reason = classify_child_outcome(facts)
    if facts.pending_decision is not None and kind != "pause":
        violations.append(ConsistencyViolation("pending_decision_without_pause", facts.alias))
    blockers: list[ChildBlocker] = []
    if facts.physical is Observation.MISSING:
        execution = ChildExecution.PENDING
        blockers.append(ChildBlocker("child_missing", facts.alias))
    elif violations:
        execution = ChildExecution.INCONSISTENT
    elif facts.active_operations:
        execution = ChildExecution.RUNNING
    elif kind == "pause":
        execution = ChildExecution.PAUSED
    else:
        execution = ChildExecution.TERMINAL

    rejected = _is_rejected_release(facts)
    success = kind == "success"
    evaluable = success or rejected
    if rejected:
        disposition, release_ready = ReleaseDisposition.REJECTED, False
        blockers.append(ChildBlocker("release_rejected", facts.alias))
    elif success:
        disposition, release_ready = (
            (ReleaseDisposition.APPROVED, True)
            if facts.release_verdict == "APPROVED" and facts.release_ship_ready is True
            else (ReleaseDisposition.NOT_APPLICABLE, True)
        )
    else:
        disposition, release_ready = ReleaseDisposition.UNAVAILABLE, False
        if execution is not ChildExecution.RUNNING and not blockers:
            blockers.append(ChildBlocker(reason, facts.alias))

    if facts.checkpoint_sub_status == "done" and not success and not rejected:
        violations.append(ConsistencyViolation("checkpoint_sub_status_contradiction", facts.alias))
    return ChildState(
        facts.alias,
        execution,
        facts.physical_status,
        facts.halt_reason,
        facts.active_operations,
        evaluable,
        disposition,
        release_ready,
        facts.pending_decision,
        tuple(blockers),
    ), tuple(violations)


def _is_rejected_release(facts: ChildFacts) -> bool:
    return (
        facts.physical_status == "halted"
        and facts.halt_reason == "final_acceptance_rejected"
        and facts.release_verdict == "REJECTED"
        and facts.release_ship_ready is False
    )


def _pending_decision_violations(
    parent_pending: PendingDecision | None, children: list[ChildState]
) -> tuple[ConsistencyViolation, ...]:
    """Accept a project proxy and its paused child's payload as one decision.

    A cross parent persists a project proxy handoff while the child persists its
    own phase handoff.  The proxy's ``child_handoff_id`` and alias structurally
    identify that child payload, so these two durable observations corroborate
    each other rather than competing for ownership.
    """
    child_pending = tuple(child for child in children if child.pending_decision is not None)
    if parent_pending is None or not child_pending:
        return ()
    for child in child_pending:
        if _is_project_proxy_child_pair(parent_pending, child):
            child_pending = tuple(item for item in child_pending if item is not child)
            break
    return (
        (ConsistencyViolation("multiple_pending_decisions"),)
        if child_pending
        else ()
    )


def _is_project_proxy_child_pair(parent: PendingDecision, child: ChildState) -> bool:
    """Return whether ``child`` is the exact child decision proxied by parent."""
    decision = child.pending_decision
    return (
        parent.kind == "project"
        and parent.alias == child.alias
        and parent.child_handoff_id is not None
        and decision is not None
        and parent.child_handoff_id == decision.handoff_id
        and decision.alias in (None, child.alias)
    )


def _handoff_violations(
    pending: PendingDecision | None, checkpoint: CheckpointHandoff
) -> tuple[ConsistencyViolation, ...]:
    if pending is None:
        return (
            (ConsistencyViolation("checkpoint_pending_without_payload"),)
            if checkpoint.pending
            else ()
        )
    violations: list[ConsistencyViolation] = []
    if not checkpoint.pending:
        violations.append(ConsistencyViolation("payload_without_checkpoint_pending", pending.alias))
    for field in ("kind", "alias", "parent_handoff_id", "child_handoff_id"):
        expected = getattr(pending, field)
        observed = getattr(checkpoint, field)
        if expected is not None and observed is not None and expected != observed:
            violations.append(ConsistencyViolation(f"checkpoint_{field}_conflict", pending.alias))
    if (
        checkpoint.parent_handoff_id is not None
        and checkpoint.parent_handoff_id != pending.handoff_id
    ):
        violations.append(ConsistencyViolation("checkpoint_handoff_id_conflict", pending.alias))
    return tuple(violations)


def _terminal_disposition(status: str | None) -> TerminalDisposition | None:
    return {
        "done": TerminalDisposition.SUCCESS,
        "success": TerminalDisposition.SUCCESS,
        "completed": TerminalDisposition.SUCCESS,
        "failed": TerminalDisposition.FAILURE,
        "halted": TerminalDisposition.HALTED,
    }.get(status)


__all__ = [
    "ActiveOperation",
    "CheckpointHandoff",
    "ChildBlocker",
    "ChildExecution",
    "ChildFacts",
    "ChildState",
    "ConsistencyViolation",
    "CrossParentFacts",
    "CrossParentState",
    "Observation",
    "ParentClass",
    "PendingDecision",
    "PhaseIdentity",
    "ReleaseDisposition",
    "ScheduledGateIdentity",
    "TerminalDisposition",
    "classify_child_outcome",
    "reduce_cross_parent_state",
]
