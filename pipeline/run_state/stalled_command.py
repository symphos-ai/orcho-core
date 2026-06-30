# SPDX-License-Identifier: Apache-2.0
"""Durable recovery metadata for stalled-command failures (ADR 0101 family).

When an agent command stalls, the run carries a bounded, provider-neutral
recovery record so operators and clients can act on it. There are two
sources of a stall record and this module serves both:

* **Terminal** — an idle-timeout escalated to
  :class:`agents.stall_protocol.AgentCommandStalledError`. The pipeline's
  failure handler merges :func:`build_stalled_command_failure` into
  ``session['failure']`` (next to the provider-access recovery record).
* **Non-terminal** — a live risk flag emitted as an ``agent.command_stalled``
  event while the phase is still running. That path never touches the session;
  observability comes from the event store. The *same* recovery verbs apply,
  built here so terminal failure, live projection, the event payload, and the
  evidence slice all agree.

The carrier types live in the neutral :mod:`agents.stall_protocol`; this module
imports them rather than redefining, so there is no ``agents -> pipeline`` back
edge. Like the rest of ``run_state``, this module does no file IO, emits no
events, and touches no checkpoint — it only builds plain dicts and mutates the
flat state mapping in place.
"""

from __future__ import annotations

from typing import Any

from agents.stall_protocol import (
    STALL_RECOVERY_VERBS,
    StalledCommand,
    build_stall_recovery_actions,
)

#: Stable ``failure_kind`` discriminator for a stalled-command terminal
#: failure (parallel to ``provider_access``). Read by the SDK projections.
STALLED_COMMAND_FAILURE_KIND = "stalled_command"

#: Recommended-action tag for the durable failure record.
RECOMMENDED_ACTION = "interrupt_resume_or_halt"

# ``STALL_RECOVERY_VERBS`` / ``build_stall_recovery_actions`` are owned by the
# neutral :mod:`agents.stall_protocol` (so the carrier's ``event_payload`` can
# embed the same verb set without an ``agents -> pipeline`` back edge). They are
# re-exported here for the run-state / SDK / evidence consumers that historically
# imported them from this module.


def build_stalled_command_failure(stalled: StalledCommand) -> dict[str, Any]:
    """Provider-neutral durable failure fields for a terminal stall.

    Pure: takes the bounded carrier and returns a plain dict to merge into
    ``session['failure']`` and the terminal ``run.end`` / ``agent.command_stalled``
    event payloads. The preview / tail are already bounded by the carrier, so
    no further truncation happens here.
    """
    failure: dict[str, Any] = {
        "failure_kind": STALLED_COMMAND_FAILURE_KIND,
        "recoverable": True,
        "recommended_action": RECOMMENDED_ACTION,
        "failed_phase": stalled.phase,
        "reason": str(stalled.reason),
        "elapsed_s": stalled.elapsed_s,
        "recovery_actions": build_stall_recovery_actions(),
    }
    if stalled.command_preview:
        failure["command_preview"] = stalled.command_preview
    if stalled.output_tail:
        failure["output_tail"] = stalled.output_tail
    if stalled.process_group is not None:
        failure["process_group"] = stalled.process_group
    return failure


def write_finalization_snapshot(
    state: dict[str, Any], stalled: StalledCommand,
) -> None:
    """Stamp an *optional* bounded finalization snapshot of a stall.

    Writes ``state['stall_diagnostics']`` — a bounded after-the-fact mirror of
    the stall, explicitly flagged ``"source": "finalization_snapshot"`` so no
    consumer mistakes it for the live-observability source. The authoritative
    live source is the emitted non-terminal ``agent.command_stalled`` event;
    this snapshot only duplicates it for a settled run and is never the source
    of non-terminal observability.

    Pure in-place mutation: no IO, no events. Callers that want the snapshot
    persisted own ``save_session``.
    """
    snapshot = {
        "source": "finalization_snapshot",
        "phase": stalled.phase,
        "reason": str(stalled.reason),
        "elapsed_s": stalled.elapsed_s,
        "recovery_actions": list(STALL_RECOVERY_VERBS),
    }
    if stalled.command_preview:
        snapshot["command_preview"] = stalled.command_preview
    if stalled.output_tail:
        snapshot["output_tail"] = stalled.output_tail
    if stalled.process_group is not None:
        snapshot["process_group"] = stalled.process_group
    state["stall_diagnostics"] = snapshot


__all__ = [
    "RECOMMENDED_ACTION",
    "STALLED_COMMAND_FAILURE_KIND",
    "STALL_RECOVERY_VERBS",
    "build_stall_recovery_actions",
    "build_stalled_command_failure",
    "write_finalization_snapshot",
]
