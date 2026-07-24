# SPDX-License-Identifier: Apache-2.0
"""Typed control contracts for checkpoint-resume transitions.

This module deliberately owns only the narrow, expected failures at a resume
boundary.  Provider, process, and ordinary programming failures must continue
to reach the normal crash/``atexit`` path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

UNATTENDED_HALT_REASON = "phase_handoff_unattended_halt"


class ResumeControlError(RuntimeError):
    """Base class for expected, durable checkpoint-resume refusals."""


class ResumeControlRefusal(ResumeControlError):
    """A refusal that retains the halt reason which led to this resume."""

    def __init__(self, halt_reason: str, explanation: str) -> None:
        self.halt_reason = halt_reason
        self.explanation = explanation
        super().__init__(explanation)


@dataclass(frozen=True, slots=True)
class UnattendedHandoffRearm:
    """Validated canonical payload used to re-arm an unattended handoff."""

    handoff: dict[str, Any]
    halt_reason: str = UNATTENDED_HALT_REASON


@dataclass(frozen=True, slots=True)
class ResumeRefusalProvenance:
    """Pre-bootstrap terminal facts that a fresh session must not erase."""

    status: str | None
    halt_reason: str | None
    meta: dict[str, Any]


def read_resume_refusal_provenance(output_dir: Path | None) -> ResumeRefusalProvenance:
    """Read the pre-bootstrap resume facts without treating corrupt meta as fatal."""
    if output_dir is None:
        return ResumeRefusalProvenance(None, None, {})
    try:
        import json

        value = json.loads((output_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    meta = dict(value) if isinstance(value, Mapping) else {}
    status = meta.get("status")
    halt_reason = meta.get("halt_reason")
    return ResumeRefusalProvenance(
        status=status if isinstance(status, str) else None,
        halt_reason=halt_reason if isinstance(halt_reason, str) and halt_reason else None,
        meta=meta,
    )


def materialize_resume_control_refusal(
    *,
    session: dict[str, Any] | None,
    output_dir: Path | None,
    checkpoint: Any,
    state: Any,
    provenance: ResumeRefusalProvenance,
    error: ResumeControlError,
    task: str,
    project_dir: str,
) -> dict[str, Any]:
    """Persist a typed refusal as a settled halt before ``atexit`` can run."""
    # Mutate the live session when available: bootstrap's atexit closure holds
    # this exact dict, so replacing it would let the closure later overwrite the
    # durable refusal with ``interrupted``.
    durable = session if isinstance(session, dict) else dict(provenance.meta)
    for key, value in provenance.meta.items():
        durable.setdefault(key, value)
    durable.setdefault("task", task)
    durable.setdefault("project", project_dir)
    durable.setdefault("phases", {})
    reason = provenance.halt_reason or getattr(error, "halt_reason", None)
    if not isinstance(reason, str) or not reason:
        reason = "resume_control_refusal"
    durable["status"] = "halted"
    durable["halt_reason"] = reason
    durable["resume_refusal"] = {
        "error_type": type(error).__name__,
        "message": str(error),
        "original_status": provenance.status,
        "halt_reason": reason,
    }
    if state is not None:
        state.halt = True
        state.halt_reason = reason
    if checkpoint is not None:
        from pipeline.checkpoint import PipelineStatus

        checkpoint.set_status(PipelineStatus.HALTED)
    if output_dir is not None:
        from pipeline.engine import save_session

        save_session(output_dir, durable)
    return durable


_CANONICAL_HANDOFF_KEYS = frozenset({
    "id", "phase", "type", "trigger", "verdict", "approved",
    "round_extras_key", "round", "loop_max_rounds", "available_actions",
    "artifacts", "last_output",
})


def is_unattended_handoff_halt(meta: Mapping[str, Any]) -> bool:
    """Return whether *meta* is the resumable unattended-handoff halt."""
    return (
        meta.get("status") == "halted"
        and meta.get("halt_reason") == UNATTENDED_HALT_REASON
    )


def validate_unattended_handoff_payload(
    payload: Any,
    *,
    halt_reason: str = UNATTENDED_HALT_REASON,
) -> dict[str, Any]:
    """Validate and copy the audit-grade unattended handoff payload.

    A former compact block cannot be safely expanded: identity, artifacts, or
    operator actions might be guessed incorrectly.  Refuse it instead.
    """
    if not isinstance(payload, Mapping):
        raise ResumeControlRefusal(halt_reason, "unattended handoff payload is missing")
    missing = _CANONICAL_HANDOFF_KEYS.difference(payload)
    if missing:
        raise ResumeControlRefusal(
            halt_reason,
            "unattended handoff payload is legacy or incomplete; missing "
            + ", ".join(sorted(missing)),
        )
    if not isinstance(payload["id"], str) or not payload["id"]:
        raise ResumeControlRefusal(halt_reason, "unattended handoff payload has no id")
    if not isinstance(payload["phase"], str) or not payload["phase"]:
        raise ResumeControlRefusal(halt_reason, "unattended handoff payload has no phase")
    if not isinstance(payload["type"], str) or not payload["type"]:
        raise ResumeControlRefusal(halt_reason, "unattended handoff payload has no type")
    from pipeline.runtime.roles import PhaseHandoffType

    if payload["type"] not in {item.value for item in PhaseHandoffType}:
        raise ResumeControlRefusal(halt_reason, "unattended handoff payload has unknown type")
    if not isinstance(payload["trigger"], str):
        raise ResumeControlRefusal(halt_reason, "unattended handoff payload has invalid trigger")
    if not isinstance(payload["verdict"], str):
        raise ResumeControlRefusal(halt_reason, "unattended handoff payload has invalid verdict")
    if not isinstance(payload["approved"], bool):
        raise ResumeControlRefusal(halt_reason, "unattended handoff payload has invalid approved flag")
    if not isinstance(payload["round_extras_key"], str):
        raise ResumeControlRefusal(halt_reason, "unattended handoff payload has invalid round_extras_key")
    if not isinstance(payload["round"], int) or payload["round"] < 1:
        raise ResumeControlRefusal(halt_reason, "unattended handoff payload has invalid round")
    if not isinstance(payload["loop_max_rounds"], int) or payload["loop_max_rounds"] < 1:
        raise ResumeControlRefusal(halt_reason, "unattended handoff payload has invalid loop_max_rounds")
    actions = payload["available_actions"]
    if not isinstance(actions, list) or not actions or not all(
        isinstance(action, str) and action for action in actions
    ):
        raise ResumeControlRefusal(halt_reason, "unattended handoff payload has invalid available_actions")
    if not isinstance(payload["artifacts"], Mapping):
        raise ResumeControlRefusal(halt_reason, "unattended handoff payload has invalid artifacts")
    if not isinstance(payload["last_output"], str):
        raise ResumeControlRefusal(halt_reason, "unattended handoff payload has invalid last_output")
    return dict(payload)


def prepare_unattended_handoff_rearm(meta: Mapping[str, Any]) -> UnattendedHandoffRearm:
    """Return the persisted payload for a resumable unattended halt.

    ``phase_handoff_unattended["phase_handoff"]`` is the canonical payload
    location.  The previous compact representation is intentionally rejected
    rather than recomputed from a changed profile or gate schedule.
    """
    halt_reason = meta.get("halt_reason")
    reason = halt_reason if isinstance(halt_reason, str) and halt_reason else UNATTENDED_HALT_REASON
    if not is_unattended_handoff_halt(meta):
        raise ResumeControlRefusal(reason, "resume is not an unattended phase-handoff halt")
    block = meta.get("phase_handoff_unattended")
    if not isinstance(block, Mapping) or "phase_handoff" not in block:
        raise ResumeControlRefusal(
            reason, "unattended handoff payload is legacy or incomplete",
        )
    return UnattendedHandoffRearm(
        handoff=validate_unattended_handoff_payload(
            block["phase_handoff"], halt_reason=reason,
        ),
        halt_reason=reason,
    )


__all__ = [
    "ResumeControlError", "ResumeControlRefusal", "UNATTENDED_HALT_REASON",
    "ResumeRefusalProvenance", "UnattendedHandoffRearm",
    "is_unattended_handoff_halt", "materialize_resume_control_refusal",
    "prepare_unattended_handoff_rearm", "validate_unattended_handoff_payload",
    "read_resume_refusal_provenance",
]
