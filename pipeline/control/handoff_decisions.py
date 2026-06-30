"""Shared phase-handoff decision lifecycle (ADR 0040, Phase B).

Both the single-project orchestrator and the cross-project planning loop
need the same three-step lifecycle when resuming a paused run:

1. **Load** the persisted decision artifact for the active handoff id
   via :func:`sdk.phase_handoff.load_phase_handoff_decision`.
2. **Validate** the strict-reader outcome. A missing artifact + an
   active pause is fail-fast (the operator must decide before resume).
   A corrupt artifact (``InvalidPhaseHandoffState``) is translated to
   a ``RuntimeError`` so dispatch surfaces a structured failure rather
   than wedging the state machine.
3. **Classify** the action into the
   :data:`HandoffDecisionAction` literal so callers can branch
   cleanly between halt / continue / retry_feedback /
   continue_with_waiver.

This module deliberately knows nothing about session dicts, ``meta.json``,
cross checkpoints, ``cross_plan.md``, ``phase0_done``, child aliases,
profile loops, ``PhaseHandoffHaltedError``, or any prompt / retry
execution. Those are domain-specific shapes that ADR 0040's "forbidden
abstraction shapes" rules require to stay local to each caller. The
primitive's contract is: turn a ``(run_id, handoff_id, runs_dir)``
triple into a typed decision result; everything that happens after that
is the caller's problem.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pipeline.run_state.types import HandoffAction

#: The four operator decision actions, normalised to the SDK's literal
#: values. Mirrors :class:`pipeline.runtime.roles.PhaseHandoffAction`
#: without depending on the runtime layer.
HandoffDecisionAction = Literal[
    "halt", "continue", "retry_feedback", "continue_with_waiver",
]

#: The valid decision actions on the wire. The three active (non-terminal)
#: transitions are sourced from the shared :class:`HandoffAction` transition
#: enum; ``halt`` is the terminal action that enum deliberately omits (it is
#: owned by :mod:`pipeline.run_state.terminal`). Keeping this set derived from
#: one source means the project resume path and the SDK decide path classify
#: the same action contract rather than two hand-maintained tuples.
_VALID_DECISION_ACTIONS: frozenset[str] = frozenset(
    {a.value for a in HandoffAction} | {"halt"},
)


@dataclass(frozen=True, slots=True)
class HandoffDecisionContext:
    """Input for :func:`load_handoff_decision`.

    ``run_id`` and ``handoff_id`` identify the active pause; ``runs_dir``
    locates the parent runs directory on disk; ``cwd`` is forwarded to
    the SDK reader so workspace-walk-up logic stays consistent with
    operator-side CLI invocations (the SDK's default is to walk up from
    the current working directory; passing ``None`` disables the walk
    and uses ``runs_dir`` directly).

    ``missing_message`` and ``invalid_message_prefix`` let the caller
    tighten the RuntimeError text with domain-specific guidance (single
    vs cross — pointer at the right MCP / CLI tool, etc.). Both default
    to a generic message that reads cleanly without context.
    """
    run_id: str
    handoff_id: str
    runs_dir: Path
    cwd: Path | str | None = None
    missing_message: str | None = None
    invalid_message_prefix: str | None = None


@dataclass(frozen=True, slots=True)
class HandoffDecisionResult:
    """Outcome of :func:`load_handoff_decision`.

    The caller branches on ``action`` and applies its own domain-specific
    halt / continue / retry_feedback / continue_with_waiver semantics. ``feedback`` is
    normalised to the empty string when the SDK returned ``None`` so
    branches that prepend it to a prompt do not have to ``or ""`` at
    every call site. ``note`` and ``decided_at`` are passed through
    unchanged for the audit-marker writes that every caller does.
    """
    action: HandoffDecisionAction
    feedback: str
    note: str | None
    decided_at: str
    handoff_id: str


def load_handoff_decision(
    ctx: HandoffDecisionContext,
) -> HandoffDecisionResult:
    """Load + validate + classify the active handoff decision.

    Behaviour matrix:

    * **Absent decision** (SDK returns ``None``) → ``RuntimeError``.
      An active pause without a matching decision means the operator
      has not yet decided; resume must not invent one.
    * **Corrupt decision** (``InvalidPhaseHandoffState``) →
      ``RuntimeError`` wrapping the SDK exception.
    * **Valid decision** → :class:`HandoffDecisionResult` with
      ``action`` narrowed to the four-value literal.
    """
    from sdk.errors import InvalidPhaseHandoffState
    from sdk.phase_handoff import load_phase_handoff_decision

    invalid_prefix = (
        ctx.invalid_message_prefix
        or f"Cannot resume run {ctx.run_id!r}: decision artifact for "
           f"handoff {ctx.handoff_id!r} failed strict validation"
    )
    missing_message = (
        ctx.missing_message
        or f"Cannot resume run {ctx.run_id!r}: handoff "
           f"{ctx.handoff_id!r} is flagged as pending but no decision "
           "artifact was found. Call phase_handoff_decide before resume."
    )

    try:
        decision = load_phase_handoff_decision(
            ctx.run_id,
            ctx.handoff_id,
            runs_dir=ctx.runs_dir,
            cwd=ctx.cwd,
        )
    except InvalidPhaseHandoffState as exc:
        raise RuntimeError(
            f"{invalid_prefix}: {exc}. Manual repair required."
        ) from exc

    if decision is None:
        raise RuntimeError(missing_message)

    return HandoffDecisionResult(
        action=_narrow_action(decision.action),
        feedback=decision.feedback or "",
        note=decision.note,
        decided_at=decision.decided_at,
        handoff_id=decision.handoff_id,
    )


def _narrow_action(raw: str) -> HandoffDecisionAction:
    """Constrain the SDK's wider string type to the four-value literal.

    The SDK's :class:`PhaseHandoffDecision` carries ``action`` as
    :data:`PhaseHandoffActionValue` (a wider literal alias). The strict
    reader already validates the value lands inside that set; here we
    re-narrow to the four resume-relevant outcomes so callers' branch
    matrix is exhaustive and `mypy --strict` can see it.

    Any other string slipping through the strict reader (e.g. a future
    SDK action we have not taught the engine about) raises
    ``RuntimeError`` rather than silently mis-routing.
    """
    if raw in _VALID_DECISION_ACTIONS:
        return raw  # type: ignore[return-value]
    raise RuntimeError(
        f"Unknown handoff decision action {raw!r}; expected one of "
        "'halt' / 'continue' / 'retry_feedback' / 'continue_with_waiver'."
    )
