"""Durable graceful-interruption handling for single-project runs.

A cancelled or terminated run must stop claiming it is still running. Two
shutdown paths reach this module and they are not interchangeable:

* ``atexit`` covers ordinary interpreter shutdown — an unhandled exception,
  ``KeyboardInterrupt``, a normal ``SystemExit``.
* A **signal handler** covers ``SIGTERM`` (and ``SIGBREAK`` on Windows), whose
  default disposition terminates the process without running ``atexit`` at all.
  This is the path ``cancel_run(mode="graceful")`` uses, so without a handler a
  cancelled run leaves ``meta.json`` at ``status: running`` forever.

The handler runs *inside* the interrupted frame, which constrains what it may
do — see :func:`persist_interruption`.
"""
from __future__ import annotations

import atexit
import contextlib
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.observability import events
from pipeline.engine import save_session
from pipeline.run_state.terminal import mark_run_interrupted

# One run per process, so a module-level slot is the whole state this needs.
_deferred_event: tuple[str, str] | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _flush_deferred_event() -> None:
    """Emit an interruption event that a signal handler could not emit itself."""
    global _deferred_event
    pending, _deferred_event = _deferred_event, None
    if pending is None:
        return
    reason, interrupted_at = pending
    with contextlib.suppress(Exception):
        events.emit("run.interrupted", reason=reason, interrupted_at=interrupted_at)


def persist_interruption(
    output_dir: Path,
    session: dict[str, Any],
    *,
    reason: str = "interrupted",
    defer_event: bool = False,
) -> bool:
    """Persist one interruption terminal boundary; return whether it was written.

    ``defer_event`` exists for the signal-handler caller. Appending an event
    takes an exclusive lock on ``events.jsonl``; a signal handler runs inside
    whatever frame it interrupted, so if that frame already holds the lock on
    another descriptor, waiting for it here would wait forever — the frame
    cannot release it until the handler returns, and the handler never does.
    The event is stashed instead and emitted from the exit path, by which time
    the interrupted frame has unwound and released the lock.
    """
    global _deferred_event
    if session.get("status") != "running":
        return False
    interrupted_at = _now()
    mark_run_interrupted(session, interrupted_at=interrupted_at, halt_reason=reason)
    save_session(output_dir, session)
    if defer_event:
        _deferred_event = (reason, interrupted_at)
    else:
        events.emit("run.interrupted", reason=reason, interrupted_at=interrupted_at)
    return True


def register_interruption_fallback(output_dir: Path, session: dict[str, Any]) -> None:
    """Register the atexit fallback for shutdown paths that reach Python exit."""
    def _on_exit() -> None:
        persist_interruption(output_dir, session)
        _flush_deferred_event()

    atexit.register(_on_exit)


def install_interrupt_handlers(output_dir: Path, session: dict[str, Any]) -> bool:
    """Install SIGTERM/SIGBREAK handlers; return whether they could be installed.

    Returns ``False`` — without raising — when the pipeline runs outside the
    main thread. Embedders do exactly that (in-process typed runs execute the
    pipeline in a worker thread), and ``signal.signal`` is main-thread only, so
    a hard failure here would break every embedded run to buy nothing: the
    atexit fallback still covers their shutdown, and such runs are stopped by
    the embedder rather than by a signal.
    """
    def _handle(signum: int, _frame: object) -> None:
        persist_interruption(
            output_dir, session, reason=f"signal:{signum}", defer_event=True,
        )
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _handle)
        # Installing the handler is what creates deferred events, so it is also
        # what must guarantee they are emitted: a caller may install handlers
        # without registering the atexit fallback.
        atexit.register(_flush_deferred_event)
        sigbreak = getattr(signal, "SIGBREAK", None)
        if sigbreak is not None:
            signal.signal(sigbreak, _handle)
    except ValueError:
        return False
    return True


__all__ = [
    "install_interrupt_handlers",
    "persist_interruption",
    "register_interruption_fallback",
]
