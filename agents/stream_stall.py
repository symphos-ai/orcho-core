# SPDX-License-Identifier: Apache-2.0
"""Focused stream-level stall monitor — monitoring only.

Watches a streamed subprocess for two stall shapes and turns each into the
neutral carrier from :mod:`agents.stall_protocol`:

* **non-terminal** unsafe free-text process polling — detected from a stream
  line via the text-only command guard and written through to a
  provider-neutral :class:`StallDiagnosticSink` AT THE MOMENT OF DETECTION
  (during the stream event, before the phase ends). Never kills, never raises,
  never touches the run session.
* **terminal** idle-timeout — the monitor only *builds* the bounded
  :class:`StalledCommand`. The single kill path and the
  :class:`AgentCommandStalledError` escalation stay in
  :func:`agents.stream._stream_run`; the existing idle-timeout is the ONLY
  auto-kill trigger.

This module is monitoring only. It imports the carrier/sink from
``agents.stall_protocol`` and the text-only detector from
``agents.command_guard``; it never spawns, signals, scans, or matches host
processes by free-text name. The verdict for unsafe polling comes purely from
the command text the agent itself emitted.
"""
from __future__ import annotations

from agents.command_guard import (
    agent_commands_from_stream_line,
    blocked_unsafe_process_polling,
)
from agents.stall_protocol import (
    OUTPUT_TAIL_MAX,
    StallDiagnosticSink,
    StalledCommand,
    StallReason,
)


class StreamStallMonitor:
    """Monitoring-only helper wired into ``_stream_run`` at the chunk seam.

    Owns no time source and no subprocess handle: ``_stream_run`` drives it,
    passing the elapsed seconds and (for the terminal carrier) the process
    group of its own child. The monitor accumulates a bounded output tail and a
    "saw any output" flag so an idle-timeout can be classified, and it
    de-duplicates repeated polling commands so a tight poll loop emits one
    diagnostic per distinct command rather than one per line.
    """

    def __init__(
        self,
        *,
        phase: str,
        sink: StallDiagnosticSink,
        command_preview: str = "",
    ) -> None:
        self._phase = phase
        self._sink = sink
        self._command_preview = command_preview
        self._saw_output = False
        self._tail = ""
        self._polled_commands: set[str] = set()

    # ── output tracking (drives idle reason + tail) ─────────────────────────
    def note_output(self, text: str) -> None:
        """Record that the child emitted output; keep a bounded tail window."""
        if not text:
            return
        self._saw_output = True
        # Keep only the trailing window — the carrier truncates again, but
        # bounding here keeps the monitor's own buffer from growing unbounded
        # across a long-but-active run.
        self._tail = (self._tail + text)[-OUTPUT_TAIL_MAX:]

    # ── non-terminal write-through (at the moment of detection) ─────────────
    def inspect_line(self, line: str, *, elapsed_s: float) -> bool:
        """Write through a non-terminal diagnostic if ``line`` polls processes.

        Runs the text-only unsafe-process-polling guard over the commands in
        ``line``. On the first sighting of a given polling command it builds a
        bounded ``StalledCommand(reason=unsafe_process_polling)`` and calls
        ``sink.record(...)`` immediately — during the stream event, never after
        the phase. It never kills, never raises, and never marks any foreign
        process: the verdict is text-only. Returns ``True`` when at least one
        diagnostic was recorded (used by tests)."""
        recorded = False
        for command in agent_commands_from_stream_line(line):
            if blocked_unsafe_process_polling(command) is None:
                continue
            if command in self._polled_commands:
                continue
            self._polled_commands.add(command)
            self._sink.record(
                StalledCommand(
                    phase=self._phase,
                    elapsed_s=elapsed_s,
                    command_preview=command,
                    output_tail="",
                    reason=StallReason.UNSAFE_PROCESS_POLLING,
                    process_group=None,
                )
            )
            recorded = True
        return recorded

    # ── terminal idle-timeout carrier (no kill / no raise here) ─────────────
    def idle_stall(
        self, *, elapsed_s: float, process_group: int | None,
    ) -> StalledCommand:
        """Build the bounded terminal carrier for an idle-timeout.

        ``silent_child_command`` when the child never emitted any output;
        ``output_inactivity`` when it produced output and then went quiet past
        the inactivity window. ``process_group`` is the run's OWN child group
        (passed by ``_stream_run``); the kill and the
        ``AgentCommandStalledError`` raise stay in ``_stream_run``."""
        reason = (
            StallReason.OUTPUT_INACTIVITY
            if self._saw_output
            else StallReason.SILENT_CHILD_COMMAND
        )
        return StalledCommand(
            phase=self._phase,
            elapsed_s=elapsed_s,
            command_preview=self._command_preview,
            output_tail=self._tail,
            reason=reason,
            process_group=process_group,
        )


__all__ = ["StreamStallMonitor"]
