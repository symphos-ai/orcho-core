"""pipeline.control.resume_preflight — checkpoint-resume handoff preflight.

Detects the "resume into an undecided active handoff" shape *before* the
project pipeline re-enters ``run_pipeline`` and trips
``load_handoff_decision_validated`` — which fail-fasts with a RuntimeError
when ``meta.phase_handoff`` is active but no decision artifact exists under
``phase_handoff_decisions/``.

A run can be left in this shape two ways:

* the operator paused (``status=awaiting_phase_handoff``) and has not yet
  recorded a decision;
* the process was interrupted after the handoff payload landed but before
  the decision was written (``status=interrupted`` + active payload) — the
  torn-handoff shape the SDK decide API already recognises.

Detection is read-only: it never mutates the run. The audit-trail
invariant (ADR 0031) is preserved because any decision is still written
through :func:`sdk.phase_handoff.phase_handoff_decide`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:
    from pipeline.runtime.handoff import PhaseHandoffRequested

__all__ = [
    "ActiveHandoffPreflight",
    "build_signal_from_active_payload",
    "detect_active_handoff_without_decision",
    "render_noninteractive_hint",
    "resolve_active_handoff_interactively",
]

@dataclass(frozen=True, slots=True)
class ActiveHandoffPreflight:
    """An active handoff that still needs an operator decision on resume."""

    run_id: str
    handoff_id: str
    status: str
    available_actions: tuple[str, ...]
    payload: Mapping[str, Any]


def detect_active_handoff_without_decision(
    *,
    run_id: str,
    run_dir: Path,
    meta: Mapping[str, Any],
) -> ActiveHandoffPreflight | None:
    """Return a preflight when ``meta`` carries an undecided active handoff.

    Returns ``None`` (resume proceeds normally) unless **all** hold:

    * ``meta.phase_handoff`` is a dict with a non-empty ``id``;
    * ``meta.status`` is ``awaiting_phase_handoff`` or ``interrupted``;
    * no decision artifact exists for that ``id`` (checked via
      :func:`sdk.phase_handoff.load_phase_handoff_decision`).
    """
    if not isinstance(meta, Mapping):
        return None
    status = meta.get("status")
    active = meta.get("phase_handoff")
    # Decidability is owned by the canonical predicate in sdk.phase_handoff
    # (the SDK is the public owner of this classification for CLI/MCP). The
    # lazy import mirrors the ``_decision_exists`` / interactive-resolve
    # pattern below and avoids a module-level control→sdk import cycle.
    from sdk.phase_handoff import _is_decidable_handoff_status

    if not _is_decidable_handoff_status(status, active):
        return None
    if not isinstance(active, dict):
        return None
    handoff_id = active.get("id")
    if not isinstance(handoff_id, str) or not handoff_id:
        return None
    if _decision_exists(run_id, handoff_id, run_dir):
        return None
    actions = tuple(
        a for a in (active.get("available_actions") or ()) if isinstance(a, str)
    )
    return ActiveHandoffPreflight(
        run_id=run_id,
        handoff_id=handoff_id,
        status=str(status),
        available_actions=actions,
        payload=active,
    )


def _decision_exists(run_id: str, handoff_id: str, run_dir: Path) -> bool:
    """True when a decision artifact already exists for ``handoff_id``.

    A corrupt artifact (or a lookup failure) is a *different* problem; we
    treat it as "exists" so the preflight does not also prompt and the
    normal resume path surfaces the real validation error.
    """
    from sdk.errors import InvalidPhaseHandoffState
    from sdk.phase_handoff import load_phase_handoff_decision

    try:
        decision = load_phase_handoff_decision(
            run_id, handoff_id, runs_dir=run_dir.parent, cwd=None,
        )
    except (InvalidPhaseHandoffState, OSError, ValueError):
        return True
    return decision is not None


def build_signal_from_active_payload(
    payload: Mapping[str, Any],
) -> PhaseHandoffRequested | None:
    """Rehydrate a :class:`PhaseHandoffRequested` from the active payload.

    Returns ``None`` on a malformed payload (missing required field /
    unknown handoff type) so the caller falls back to the non-interactive
    hint rather than guessing audit-critical defaults.
    """
    from pipeline.runtime.handoff import PhaseHandoffRequested
    from pipeline.runtime.roles import PhaseHandoffType

    try:
        return PhaseHandoffRequested(
            handoff_id=payload["id"],
            phase=payload["phase"],
            type=PhaseHandoffType(payload["type"]),
            trigger=payload.get("trigger", ""),
            verdict=payload.get("verdict", ""),
            approved=bool(payload.get("approved", False)),
            round_extras_key=payload.get("round_extras_key", ""),
            round=int(payload["round"]),
            loop_max_rounds=int(payload["loop_max_rounds"]),
            available_actions=tuple(payload.get("available_actions") or ()),
            artifacts=dict(payload.get("artifacts") or {}),
            last_output=str(payload.get("last_output") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def render_noninteractive_hint(preflight: ActiveHandoffPreflight) -> str:
    """Build the copy-pasteable hint for the non-interactive resume path.

    Lists run_id / handoff_id / status / available_actions plus a ready
    ``phase_handoff_decide(...)`` SDK call and the resume command, so a
    CI / MCP / piped caller knows exactly how to unblock the run.
    """
    actions = ", ".join(preflight.available_actions) or "(none published)"
    example_action = (
        preflight.available_actions[0]
        if preflight.available_actions
        else "continue"
    )
    feedback_kw = (
        ', feedback="..."'
        if example_action in ("retry_feedback", "continue_with_waiver")
        else ""
    )
    return "\n".join([
        f"Run {preflight.run_id} is paused on an undecided phase handoff "
        f"(status: {preflight.status}); cannot resume until it is decided.",
        f"  run_id            : {preflight.run_id}",
        f"  handoff_id        : {preflight.handoff_id}",
        f"  status            : {preflight.status}",
        f"  available_actions : {actions}",
        "",
        "Record a decision (audit-trail invariant — decisions go through "
        "the SDK), then re-run the same resume. For example:",
        "  python -c \"from sdk.phase_handoff import phase_handoff_decide; "
        f"phase_handoff_decide({preflight.run_id!r}, "
        f"{preflight.handoff_id!r}, {example_action!r}{feedback_kw})\"",
        f"  orcho run --resume {preflight.run_id}",
    ])


def resolve_active_handoff_interactively(
    preflight: ActiveHandoffPreflight,
    *,
    runs_dir: Path,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> bool:
    """Prompt the operator and record the decision through the SDK.

    Shows the same menu a fresh handoff uses
    (:func:`pipeline.control.handoff_prompt.prompt_phase_handoff_action`),
    then writes the decision via
    :func:`sdk.phase_handoff.phase_handoff_decide`. Returns ``True`` when
    a decision was recorded (resume should continue in the same command —
    ``run_pipeline`` will now find the artifact) and ``False`` when the
    operator aborted or the decision could not be recorded (leave the run
    paused). Never mutates the run except through the sanctioned SDK call.
    """
    from pipeline.control.handoff_prompt import (
        HANDOFF_PROMPT_ABORTED,
        prompt_phase_handoff_action,
    )

    signal = build_signal_from_active_payload(preflight.payload)
    if signal is None:
        return False

    decision = prompt_phase_handoff_action(signal, stdin=stdin, stdout=stdout)
    if decision is HANDOFF_PROMPT_ABORTED:
        return False

    from sdk.errors import InvalidPhaseHandoffState
    from sdk.phase_handoff import (
        load_phase_handoff_decision,
        phase_handoff_decide,
    )

    try:
        phase_handoff_decide(
            preflight.run_id,
            preflight.handoff_id,
            decision.action,
            feedback=decision.feedback,
            note=decision.note,
            runs_dir=runs_dir,
            cwd=None,
        )
    except ValueError:
        return False
    except InvalidPhaseHandoffState:
        try:
            existing = load_phase_handoff_decision(
                preflight.run_id,
                preflight.handoff_id,
                runs_dir=runs_dir,
                cwd=None,
            )
        except (InvalidPhaseHandoffState, OSError, ValueError):
            return False
        return existing is not None and existing.action == decision.action
    return True
