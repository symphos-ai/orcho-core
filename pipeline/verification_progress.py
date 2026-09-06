# SPDX-License-Identifier: Apache-2.0
"""Live, bounded progress for a running verification-gate command (ADR 0190).

Single canonical owner of the ``gate.progress`` wire contract. A scheduled
gate hook runs the declared checks (tests, linters) as blocking subprocesses
that can take minutes and print nothing; without a signal a watcher (CLI, MCP
``orcho_run_live_status``) cannot inspect output during execution. This module
turns the raw ``(stream_label, chunk)`` output of the streaming executor into a
coalesced, self-bounded ``gate.progress`` event stream plus an optional CLI
presenter callback.

Design invariants:

* **Observational only.** Progress never changes the command's real pass/fail
  outcome. The terminal receipt and the per-command ``.log`` remain
  authoritative; :data:`~core.observability.event_kinds.EventKind.GATE_END`
  stays authoritative for the settled result. Timestamps and byte counters are
  facts; there is no fabricated percentage or process-health verdict.
* **Bounded.** ``stdout_tail`` / ``stderr_tail`` are truncated at construction
  (mirroring :class:`agents.stall_protocol.StalledCommand`) so a runaway
  command can never balloon a durable record, and the full log is never
  re-appended into an event.
* **Best-effort publication.** A storage / emit / presenter failure must never
  propagate into the command's execution — every emission and presenter call is
  wrapped so the gate's outcome is independent of whether anyone was watching.
* **Coalesced.** Emissions are rate limited, never one-per-byte/line.

Layering: depends only on :mod:`core.observability` (event emission). The
context-construction helper lives here (not in the over-large
``pipeline/project/gate_repair.py``) so the wire contract stays single-owned.
"""

from __future__ import annotations

import codecs
import contextlib
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.observability import events as _events
from core.observability.event_kinds import EventKind

#: Hard cap on each persisted per-stream tail (characters). Matches the
#: stall-protocol output tail so both observational carriers agree.
TAIL_MAX = 2000

#: Minimum interval between output updates. Initial state, first output, and
#: final flush are bounded lifecycle exceptions; volume never bypasses the cap.
COALESCE_MIN_INTERVAL_S = 0.5

#: Stream labels the aggregator understands. ``bounded_proc`` feeds these.
STDOUT = "stdout"
STDERR = "stderr"

PresenterFn = Callable[["GateProgressRecord"], None]


def new_invocation_id() -> str:
    """Return a fresh unique id for one gate-command execution.

    Distinct per execution so reruns of the same command in a repair loop are
    separable in the durable event stream (the reader returns only the latest
    invocation).
    """
    return uuid.uuid4().hex


def _now_iso() -> str:
    """UTC ISO-8601 timestamp for observational progress records."""
    return datetime.now(UTC).isoformat()


def _bounded_tail(text: str, limit: int = TAIL_MAX) -> str:
    """Return the trailing ``limit`` characters of ``text`` (no marker noise)."""
    if len(text) <= limit:
        return text
    return text[-limit:]


@dataclass(frozen=True, slots=True)
class GateProgressRecord:
    """Bounded, provider-neutral snapshot of one running gate command.

    Frozen + bounded: ``stdout_tail`` / ``stderr_tail`` are truncated at
    construction (via ``__post_init__``), mirroring
    :class:`agents.stall_protocol.StalledCommand`, so the carrier is safe to
    persist or hand to a presenter without a second sanitisation step.

    ``last_output_at`` is ``None`` until the command produces its first byte;
    ``has_output`` distinguishes "no output yet" (``False``) from a stream that
    completed while empty (``True`` once any byte on either stream is seen).
    """

    name: str
    invocation_id: str
    hook: str
    phase: str
    started_at: str
    elapsed_s: float
    has_output: bool = False
    last_output_at: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    stdout_bytes: int = 0
    stderr_bytes: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "stdout_tail", _bounded_tail(self.stdout_tail))
        object.__setattr__(self, "stderr_tail", _bounded_tail(self.stderr_tail))

    def event_payload(self) -> dict[str, Any]:
        """Project to a :data:`EventKind.GATE_PROGRESS` payload.

        Required keys (``name`` / ``invocation_id`` / ``elapsed_s``) are always
        present; optional fields ride along. ``phase`` is intentionally NOT in
        the payload — it is carried on the top-level ``Event.phase`` field that
        :func:`core.observability.events.emit` stamps automatically.
        :func:`~core.observability.events._clean_payload` drops ``None`` values,
        so an absent ``last_output_at`` simply omits the key.
        """
        payload: dict[str, Any] = {
            "name": self.name,
            "invocation_id": self.invocation_id,
            "elapsed_s": self.elapsed_s,
            "has_output": self.has_output,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
        }
        if self.hook:
            payload["hook"] = self.hook
        if self.last_output_at is not None:
            payload["last_output_at"] = self.last_output_at
        if self.stdout_tail:
            payload["stdout_tail"] = self.stdout_tail
        if self.stderr_tail:
            payload["stderr_tail"] = self.stderr_tail
        return payload


@dataclass(frozen=True, slots=True)
class GateProgressContext:
    """Identity + presenter for one gate-command execution's progress stream.

    Built by :func:`build_gate_progress_context` and threaded into
    ``run_command``; the executor turns it into a :class:`GateProgressAggregator`
    wired to the streaming subprocess. ``presenter`` is the optional CLI hook
    (``None`` under SILENT / MCP-stdio presentations so nothing is written to
    stdout — the durable event is still emitted).
    """

    invocation_id: str
    name: str
    hook: str = ""
    phase: str = ""
    presenter: PresenterFn | None = None


def build_gate_progress_context(
    *,
    invocation_id: str,
    name: str,
    hook: str = "",
    phase: str = "",
    presenter: PresenterFn | None = None,
) -> GateProgressContext:
    """Construct the progress context for a gate execution (kept out of
    ``gate_repair.py`` so the wire contract stays single-owned here)."""
    return GateProgressContext(
        invocation_id=invocation_id,
        name=name,
        hook=hook,
        phase=phase,
        presenter=presenter,
    )


@dataclass
class _StreamState:
    """Rolling decode + bounded tail for one output stream."""

    decoder: codecs.IncrementalDecoder
    tail: str = ""
    bytes_read: int = 0

    def feed(self, chunk: bytes) -> None:
        # errors='replace' + an incremental decoder means a multibyte sequence
        # split across chunk boundaries never raises and never leaks a b'..'
        # repr; the trailing partial bytes are buffered until the next chunk.
        self.bytes_read += len(chunk)
        text = self.decoder.decode(chunk)
        if text:
            self.tail = _bounded_tail(self.tail + text)


class GateProgressAggregator:
    """Consume incremental output chunks and publish coalesced progress.

    ``feed(stream_label, chunk)`` is the executor callback. It decodes each
    stream with its own incremental UTF-8 decoder (``errors='replace'``),
    maintains a rolling bounded per-stream tail, tracks byte counters and the
    first/last output timestamps, and COALESCES emissions by time via
    :func:`core.observability.events.emit`. Every emission and presenter call is
    best-effort: a failure there never propagates into the command's execution.

    :meth:`emit_initial` publishes one record up front (``has_output=False``) so
    a *silent* long-running command is still observable as an active gate before
    it produces any output — the reader would otherwise see nothing until the
    first byte (ADR 0190). Coalescing still governs subsequent output chunks.

    The stdout and stderr reader threads call :meth:`feed` concurrently, so all
    mutable state is guarded by a lock.
    """

    def __init__(
        self,
        context: GateProgressContext,
        *,
        clock: Callable[[], float] = time.monotonic,
        min_interval_s: float = COALESCE_MIN_INTERVAL_S,
    ) -> None:
        self._ctx = context
        self._clock = clock
        self._min_interval_s = min_interval_s
        self._start = clock()
        self._started_at = _now_iso()
        self._streams = {
            STDOUT: _StreamState(codecs.getincrementaldecoder("utf-8")("replace")),
            STDERR: _StreamState(codecs.getincrementaldecoder("utf-8")("replace")),
        }
        self._has_output = False
        self._last_output_at: str | None = None
        self._last_emit_at: float | None = None
        self._bytes_since_emit = 0
        # feed() runs on the two reader threads; guard all mutable state.
        self._lock = threading.Lock()
        self._closed = False
        self._stop = threading.Event()
        self._publisher: threading.Thread | None = None

    # -- input --------------------------------------------------------------
    def emit_initial(self) -> None:
        """Publish the up-front ``has_output=False`` record for a silent gate."""
        with self._lock:
            self._emit_locked()

    def feed(self, stream_label: str, chunk: bytes) -> None:
        """Executor callback: fold one output chunk in and maybe emit."""
        with self._lock:
            state = self._streams.get(stream_label)
            if self._closed or state is None or not chunk:
                return
            first_output = not self._has_output
            state.feed(chunk)
            self._has_output = True
            self._last_output_at = _now_iso()
            self._bytes_since_emit += len(chunk)
            # The no-output → output transition is a real state change worth
            # surfacing at once; later chunks coalesce by time.
            if first_output or self._should_emit():
                self._emit_locked()

    def _should_emit(self) -> bool:
        if self._last_emit_at is None:
            return True  # first record always signals liveness promptly.
        return (self._clock() - self._last_emit_at) >= self._min_interval_s

    def start(self) -> None:
        """Publish initial state and start the bounded pending-output publisher."""
        self.emit_initial()
        self._publisher = threading.Thread(target=self._publish_pending, daemon=True)
        self._publisher.start()

    def _publish_pending(self) -> None:
        while not self._stop.wait(self._min_interval_s):
            with self._lock:
                if self._bytes_since_emit and self._should_emit():
                    self._emit_locked()

    def close(self) -> None:
        """Stop publication and flush pending bytes before the settled boundary."""
        self._stop.set()
        if self._publisher is not None:
            self._publisher.join()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for state in self._streams.values():
                state.tail = _bounded_tail(state.tail + state.decoder.decode(b"", final=True))
            if self._bytes_since_emit:
                self._emit_locked()

    # -- output -------------------------------------------------------------
    def _snapshot_locked(self) -> GateProgressRecord:
        return GateProgressRecord(
            name=self._ctx.name,
            invocation_id=self._ctx.invocation_id,
            hook=self._ctx.hook,
            phase=self._ctx.phase,
            started_at=self._started_at,
            elapsed_s=round(self._clock() - self._start, 3),
            has_output=self._has_output,
            last_output_at=self._last_output_at,
            stdout_tail=self._streams[STDOUT].tail,
            stderr_tail=self._streams[STDERR].tail,
            stdout_bytes=self._streams[STDOUT].bytes_read,
            stderr_bytes=self._streams[STDERR].bytes_read,
        )

    def snapshot(self) -> GateProgressRecord:
        """Build the current bounded progress record (no side effects)."""
        with self._lock:
            return self._snapshot_locked()

    def emit(self) -> None:
        """Publish one coalesced progress record — best-effort, never raises."""
        with self._lock:
            self._emit_locked()

    def _emit_locked(self) -> None:
        """Publish the current snapshot. Caller holds ``self._lock``."""
        record = self._snapshot_locked()
        self._last_emit_at = self._clock()
        self._bytes_since_emit = 0
        # Durable event first: a store that was never initialised makes emit a
        # no-op; any unexpected error is swallowed so a diagnostic can never
        # break the command it describes. ``event_phase`` stamps the gate's own
        # phase on the top-level Event.phase even after phase.end cleared the
        # global phase context (ADR 0190).
        with contextlib.suppress(Exception):
            _events.emit(
                EventKind.GATE_PROGRESS,
                event_phase=self._ctx.phase or None,
                **record.event_payload(),
            )
        presenter = self._ctx.presenter
        if presenter is not None:
            with contextlib.suppress(Exception):
                presenter(record)


__all__ = [
    "COALESCE_MIN_INTERVAL_S",
    "STDERR",
    "STDOUT",
    "TAIL_MAX",
    "GateProgressAggregator",
    "GateProgressContext",
    "GateProgressRecord",
    "PresenterFn",
    "build_gate_progress_context",
    "new_invocation_id",
]


@contextlib.contextmanager
def observe_gate(
    context: GateProgressContext | None,
) -> Iterator[Callable[[str, bytes], None] | None]:
    """Scope publication to command execution, including cancellation cleanup."""
    if context is None:
        yield None
        return
    aggregator = GateProgressAggregator(context)
    aggregator.start()
    try:
        yield aggregator.feed
    finally:
        aggregator.close()
