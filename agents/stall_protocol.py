# SPDX-License-Identifier: Apache-2.0
"""Provider-neutral carrier types and sink for stalled-command diagnostics.

A *stalled command* is a shell command an agent is running that has stopped
making progress: it may be terminally hung (idle past the runtime's
idle-timeout) or merely suspicious (a non-terminal risk signal such as an
agent polling foreign processes by free-text name). This module owns the
neutral protocol surface so that no consumer needs to import a runtime, the
stream monitor, or the pipeline to speak about stalls:

* :class:`StallReason` — the bounded reason vocabulary.
* :class:`StalledCommand` — the frozen, *bounded* carrier (preview and tail
  are truncated at construction so a runaway command can never balloon a
  durable record).
* :class:`AgentCommandStalledError` — the terminal escalation carrier the
  agents layer raises on idle-timeout; the pipeline catches it next to
  ``AgentAccessError`` and writes a terminal ``session['failure']``.
* :class:`StallDiagnosticSink` — the narrow provider-neutral sink the stream
  monitor calls on a *non-terminal* detection. The default implementation
  emits an ``agent.command_stalled`` event through core observability; it
  never touches the run session and never raises ``AgentCommandStalledError``.

Layering: this module depends only on :mod:`core.observability` (event
emission). It must never import :mod:`pipeline` — the dependency direction is
``pipeline -> agents``, never the reverse. The carrier types live here, in the
neutral agents layer, precisely so ``pipeline/run_state/*`` and ``sdk/*`` can
import them without pulling in the stream monitor.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

#: Hard cap on the persisted command preview (characters). A command line is a
#: handful of tokens in the normal case; the cap bounds a pathological argv.
COMMAND_PREVIEW_MAX = 200

#: Hard cap on the persisted output tail / inactivity window (characters).
OUTPUT_TAIL_MAX = 2000

#: The bounded recovery verb vocabulary, in projection order. ``interrupt``
#: addresses the run's own child process group; ``resume_from_checkpoint``
#: picks the run back up; ``halt`` is a meta-only durable option (never an
#: executable SDK action). This lives in the neutral protocol module — with no
#: ``agents -> pipeline`` back edge — so every consumer (the event payload, the
#: live projection, the terminal failure record, and the evidence slice) agrees
#: on one source of truth. ``pipeline.run_state.stalled_command`` re-exports it.
STALL_RECOVERY_VERBS: tuple[str, ...] = (
    "interrupt",
    "resume_from_checkpoint",
    "halt",
)


def build_stall_recovery_actions() -> list[dict[str, str]]:
    """Durable recovery options for a stalled command.

    One ``{"action": <verb>}`` entry per :data:`STALL_RECOVERY_VERBS`, in
    order. Mirrors the dict shape of the provider-access recovery list so the
    SDK projection, the event payload, and the evidence slice can parse all of
    them uniformly.
    """
    return [{"action": verb} for verb in STALL_RECOVERY_VERBS]


class StallReason(StrEnum):
    """Why a command was classified as stalled.

    * ``unsafe_process_polling`` — the command polls foreign processes by
      free-text name (``pgrep -f <text>`` / ``kill -0 $(pgrep -f ...)``).
      This is a *non-terminal* risk flag: it never escalates to a kill on its
      own and never makes the run terminal-failed.
    * ``silent_child_command`` — a child command produced no output and never
      exited within the runtime's idle-timeout (a terminal hang).
    * ``output_inactivity`` — the command emitted output but then went quiet
      past the inactivity window.
    """

    UNSAFE_PROCESS_POLLING = "unsafe_process_polling"
    SILENT_CHILD_COMMAND = "silent_child_command"
    OUTPUT_INACTIVITY = "output_inactivity"


def _truncate(text: str, limit: int) -> str:
    """Return ``text`` bounded to ``limit`` characters (no marker noise)."""
    if len(text) <= limit:
        return text
    return text[:limit]


@dataclass(frozen=True, slots=True)
class StalledCommand:
    """Bounded, provider-neutral record of one stalled command.

    Frozen + bounded: ``command_preview`` and ``output_tail`` are truncated at
    construction (via ``__post_init__``) so the carrier is safe to persist
    directly into durable evidence without a second sanitisation step. The
    fields are deliberately minimal — phase, elapsed seconds, a command
    preview, an output tail / inactivity window, the reason category, and the
    owning process group id (``None`` when not known / not a real child).
    """

    phase: str
    elapsed_s: float
    command_preview: str
    output_tail: str
    reason: StallReason
    process_group: int | None = None

    def __post_init__(self) -> None:
        # frozen dataclass: bound the free-text fields through the back door so
        # every instance — however constructed — carries truncated text.
        object.__setattr__(
            self, "command_preview", _truncate(self.command_preview, COMMAND_PREVIEW_MAX),
        )
        object.__setattr__(
            self, "output_tail", _truncate(self.output_tail, OUTPUT_TAIL_MAX),
        )

    def event_payload(self, *, terminal: bool) -> dict[str, Any]:
        """Project to an ``agent.command_stalled`` event payload.

        The required keys (``phase`` / ``reason`` / ``elapsed_s`` /
        ``terminal`` / ``recovery_actions``) are always present; the optional
        preview / tail / process_group ride along when non-empty. ``reason`` is
        the StrEnum's string value so the payload is plain-JSON.
        ``recovery_actions`` is the shared bounded verb set so a generic event
        consumer (MCP/event-tail) sees the recovery contract at the moment of
        write-through, without re-deriving it. ``_clean_payload`` (the event
        store) drops ``None`` values, so an unknown process group simply omits
        the key.
        """
        payload: dict[str, Any] = {
            "phase": self.phase,
            "reason": str(self.reason),
            "elapsed_s": self.elapsed_s,
            "terminal": terminal,
            "recovery_actions": build_stall_recovery_actions(),
        }
        if self.command_preview:
            payload["command_preview"] = self.command_preview
        if self.output_tail:
            payload["output_tail"] = self.output_tail
        if self.process_group is not None:
            payload["process_group"] = self.process_group
        return payload


class AgentCommandStalledError(Exception):
    """Terminal escalation raised by the agents layer on idle-timeout.

    Carries the bounded :class:`StalledCommand` so the pipeline's failure
    handler can write a terminal ``session['failure']`` record without
    re-deriving the diagnostic. This is the *terminal* path only — the
    non-terminal risk path goes through :class:`StallDiagnosticSink` instead
    and never raises.
    """

    def __init__(self, stalled: StalledCommand, *, message: str | None = None) -> None:
        self.stalled = stalled
        super().__init__(
            message
            or (
                f"agent command stalled in phase {stalled.phase!r} after "
                f"{stalled.elapsed_s:.0f}s ({stalled.reason})"
            ),
        )


@runtime_checkable
class StallDiagnosticSink(Protocol):
    """Narrow provider-neutral sink for *non-terminal* stall diagnostics.

    The stream monitor calls :meth:`record` the moment it detects a
    non-terminal stall (during a stream event, before the phase finishes). The
    sink owns *how* the diagnostic becomes observable — the default emits an
    event — so the monitor never writes the run session directly, never raises
    :class:`AgentCommandStalledError`, and never kills the subprocess.
    """

    def record(self, stalled: StalledCommand) -> None:
        """Record one non-terminal stall diagnostic. Must not raise."""
        ...


class EventStallDiagnosticSink:
    """Default sink: emit a non-terminal ``agent.command_stalled`` event.

    Provider-neutral and session-free. It writes one event into the active run
    event-store through :func:`core.observability.events.emit` with
    ``terminal=False``; a focused projector (``sdk.evidence_slices``) reads
    those events back so the diagnostic is observable while the phase is still
    running. Emission is best-effort — a store that was never initialised makes
    ``emit`` a no-op, and any unexpected error is swallowed so a diagnostic can
    never break the run it is describing.
    """

    def record(self, stalled: StalledCommand) -> None:
        import contextlib

        from core.observability import events as _events
        from core.observability.event_kinds import EventKind

        with contextlib.suppress(Exception):
            _events.emit(
                EventKind.AGENT_COMMAND_STALLED,
                **stalled.event_payload(terminal=False),
            )


__all__ = [
    "COMMAND_PREVIEW_MAX",
    "OUTPUT_TAIL_MAX",
    "STALL_RECOVERY_VERBS",
    "AgentCommandStalledError",
    "EventStallDiagnosticSink",
    "StallDiagnosticSink",
    "StallReason",
    "StalledCommand",
    "build_stall_recovery_actions",
]
