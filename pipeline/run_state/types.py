"""Client-neutral run-state value types (Stage 0 brain layer).

Pure data shapes describing a run's lifecycle as folded from its event
stream. They never touch the filesystem or spawn a process — that lives in
:mod:`pipeline.run_state.projector` (event read) and
:mod:`pipeline.run_state.consistency` (diagnosis).

Discipline:

- No dependency on ``sdk.run_control``; no back-edges into runtime / resume
  / finalization paths.
- :class:`RunStatus` mirrors :class:`pipeline.checkpoint.PipelineStatus` and
  adds the torn ``interrupted`` status and the synthetic initial ``unknown``.
- :class:`RunEventType` values match the on-disk ``kind`` from
  :class:`core.observability.event_kinds.EventKind` where one exists; the
  remaining members are client-neutral types the reducer can interpret but
  which no current writer emits (flagged below).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class RunStatus(StrEnum):
    """Overall run status as folded by the reducer.

    Values mirror :class:`pipeline.checkpoint.PipelineStatus` plus the torn
    ``interrupted`` status (a handoff persisted but the decision write was
    interrupted) and the synthetic ``unknown`` seed for a fresh snapshot.
    """

    RUNNING = "running"
    AWAITING_PHASE_HANDOFF = "awaiting_phase_handoff"
    AWAITING_GATE_DECISION = "awaiting_gate_decision"
    AWAITING_HUMAN_REVIEW = "awaiting_human_review"
    DONE = "done"
    HALTED = "halted"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class RunEventType(StrEnum):
    """Client-neutral event types the reducer can interpret.

    Members whose value matches an on-disk ``kind`` from
    :class:`core.observability.event_kinds.EventKind`:
    :attr:`RUN_START`, :attr:`RUN_END`, :attr:`PHASE_START`,
    :attr:`PHASE_END`, :attr:`PHASE_HANDOFF_REQUESTED`.

    The rest are client-neutral / deferred types required for reducer
    completeness but **not emitted by any current writer** — halt is in
    practice observed from on-disk decision artifacts, not the event
    stream: :attr:`RUN_STARTED`, :attr:`PHASE_HANDOFF_DECIDED`,
    :attr:`RUN_INTERRUPTED`, :attr:`RUN_HALTED`.
    """

    # ── Match on-disk EventKind values ─────────────────────────────
    RUN_START = "run.start"
    RUN_END = "run.end"
    PHASE_START = "phase.start"
    PHASE_END = "phase.end"
    PHASE_HANDOFF_REQUESTED = "phase.handoff_requested"

    # ── Client-neutral / deferred (no current writer emits these) ──
    RUN_STARTED = "run.started"
    PHASE_HANDOFF_DECIDED = "phase_handoff.decided"
    RUN_INTERRUPTED = "run.interrupted"
    RUN_HALTED = "run.halted"

    @classmethod
    def from_kind(cls, kind: str) -> RunEventType | None:
        """Map an on-disk event ``kind`` to a member, or ``None`` if unknown."""
        try:
            return cls(kind)
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class RunStateSnapshot:
    """Immutable projection of a run's lifecycle folded from its events.

    ``seq`` is the last accepted event seq. Collections are tuples (never
    lists) so the snapshot is fully immutable. ``seen_handoff_ids`` is the
    read-only history of every id seen in a ``phase.handoff_requested``
    event; it is distinct from ``active_handoff_id`` and is **never cleared
    on halt** (so consistency checks can tell "no event ever happened" from
    "event happened, active later cleared").
    """

    status: RunStatus = RunStatus.UNKNOWN
    seq: int = 0
    seen_phases: tuple[str, ...] = ()
    completed_phases: tuple[str, ...] = ()
    failed_phases: tuple[str, ...] = ()
    active_phase: str | None = None
    active_handoff_id: str | None = None
    active_handoff_phase: str | None = None
    seen_handoff_ids: tuple[str, ...] = ()
    terminal: bool = False

    @classmethod
    def initial(cls) -> RunStateSnapshot:
        """Return the seed snapshot (status ``unknown``, seq 0, empty)."""
        return cls()


class RunTransitionError(Exception):
    """Raised when an event cannot be applied to a snapshot."""


@dataclass(frozen=True, slots=True)
class RunStateIssue:
    """One diagnosed run-state inconsistency.

    ``severity`` is a plain ``str`` (``'error'`` / ``'warning'`` / ``'info'``).
    ``repair_hint`` is advisory text only — diagnosis never repairs.
    """

    code: str
    severity: str
    message: str
    repair_hint: str | None = None


@dataclass(frozen=True, slots=True)
class RunStateValidationReport:
    """Result of validating a run dir's projection against durable state.

    ``ok`` is True when no issue has severity ``'error'``.
    """

    run_dir: Path
    projected: RunStateSnapshot
    meta_status: str | None
    ok: bool
    issues: tuple[RunStateIssue, ...] = field(default_factory=tuple)


# ── Active phase-handoff transition records ────────────────────────────
#
# Typed shapes for the *active* (non-terminal) phase-handoff transitions —
# the ``status='running'`` decisions an operator can resolve a pause to.
# Halt is deliberately absent: it is a terminal transition owned by
# :mod:`pipeline.run_state.terminal`, never this layer. The builders that
# produce these records live in :mod:`pipeline.run_state.handoff`.


class HandoffAction(StrEnum):
    """The non-terminal action a ``phase_handoff_override`` marker records.

    Values mirror :class:`pipeline.runtime.roles.PhaseHandoffAction` minus
    ``halt`` (terminal), without depending on the runtime layer.
    """

    CONTINUE = "continue"
    CONTINUE_WITH_WAIVER = "continue_with_waiver"
    RETRY_FEEDBACK = "retry_feedback"


class HandoffRetryMode(StrEnum):
    """Which loop a ``retry_feedback`` decision re-runs.

    ``PLAN`` re-runs the ``plan -> validate_plan`` loop; ``REPAIR`` re-runs
    the ``review_changes -> repair_changes`` loop. Carried explicitly so a
    caller distinguishes the two **without** parsing the paused phase string.
    """

    PLAN = "plan"
    REPAIR = "repair"


@dataclass(frozen=True, slots=True)
class HandoffTransition:
    """Derived records of one active phase-handoff transition.

    A transition mutates the flat state mapping in place for the parts that
    live *there* (``status`` and the active ``phase_handoff`` payload) and
    returns this object for the parts that live on a *separate* object
    (``state.extras`` / the session), which the caller places itself.

    Fields:

    - ``override`` — the ``phase_handoff_override`` marker (always present).
    - ``waiver`` — the durable ``phase_handoff_waiver`` record; present only
      for ``continue_with_waiver``, ``None`` otherwise.
    - ``human_feedback`` — the ``human_feedback`` extras marker; present only
      for ``retry_feedback``, ``None`` otherwise.
    - ``retry_mode`` — the typed plan/repair distinction; present only for
      ``retry_feedback``, ``None`` otherwise.
    """

    override: dict[str, object]
    waiver: dict[str, object] | None = None
    human_feedback: dict[str, object] | None = None
    retry_mode: HandoffRetryMode | None = None


__all__ = [
    "HandoffAction",
    "HandoffRetryMode",
    "HandoffTransition",
    "RunEventType",
    "RunStateIssue",
    "RunStateSnapshot",
    "RunStateValidationReport",
    "RunStatus",
    "RunTransitionError",
]
