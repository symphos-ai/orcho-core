"""
agents/stream_transport.py — cross-platform byte source for streamed agents.

The streaming core (:mod:`agents.stream`) spawns the agent subprocess and then
pumps whole lines out of a *transport* that owns the platform-specific way of
reading the child's live stdout. Keeping that seam separate lets the streamer
stay transport-agnostic and, crucially, lets the package import on hosts that
ship no pseudo-terminal.

Two transports ship:

* :class:`PtyTransport` (POSIX): the child's stdin/stdout are bound to a
  pseudo-terminal slave so the agent CLI sees a real TTY and line-buffers its
  output; the parent reads the master end via ``select`` + ``os.read``. The
  ``pty`` module is imported lazily inside the constructor — it is POSIX-only
  and CPython ships none of it on native Windows, so a module-level import
  would break ``import agents`` on that platform.
* :class:`PipeTransport` (native Windows, and any host without ``os.openpty``):
  the child's stdout is an ordinary pipe drained by a background reader thread
  into a queue, because ``select`` cannot wait on pipe handles on Windows. The
  child gets no controlling terminal — its stdout is a plain pipe, so a CLI
  that probes ``sys.stdout.isatty()`` sees a non-interactive stream, exactly as
  under any piped invocation. (stdin is ``DEVNULL``; ``sys.stdin.isatty()`` is
  not a reliable signal on Windows, where the ``NUL`` device behind ``DEVNULL``
  is itself a character device.)

:func:`select_transport` picks :class:`PtyTransport` when ``os.openpty`` is
available and :class:`PipeTransport` otherwise. :class:`PipeTransport` is pure
stdlib (``subprocess`` + ``threading`` + ``queue``) and works on POSIX too, so
tests can exercise the Windows path on every runner, not only ``windows-latest``.

Transport read contract (:meth:`StreamTransport.read`):

* returns a non-empty ``bytes`` chunk when output was available;
* returns ``None`` when no output arrived within the wait window (the child may
  still be running — the caller checks liveness and the idle watchdog);
* returns ``b""`` when the stream has closed (EOF — the caller stops reading).
"""
from __future__ import annotations

import contextlib
import errno
import os
import queue
import subprocess
import sys
import threading
from typing import Any

# Read granularity for both transports. Matches the historical PTY read size.
_READ_CHUNK = 4096

# Native Windows (and any exotic host) ships no ``os.openpty``. This single
# predicate drives transport selection so the rest of the module never branches
# on ``sys.platform`` directly.
_HAS_OPENPTY = hasattr(os, "openpty") and sys.platform != "win32"


class StreamTransport:
    """Strategy interface for the streamed-subprocess byte source.

    A transport owns two responsibilities: how the child's stdio is wired into
    :func:`subprocess.Popen` (:meth:`popen_stdio`), and how the parent reads the
    child's live output (:meth:`read`). Everything else — line buffering, log
    fan-out, masking, watchdogs — stays in :mod:`agents.stream`.
    """

    def popen_stdio(self) -> dict[str, Any]:
        """Return the ``Popen`` stdin/stdout kwargs for the child.

        ``stderr`` is owned by the spawn helper (always ``subprocess.PIPE``) and
        is intentionally *not* set here.
        """
        raise NotImplementedError

    def after_spawn(self, proc: subprocess.Popen) -> None:
        """Hook invoked once, immediately after ``Popen`` returns."""
        raise NotImplementedError

    def read(self, timeout: float) -> bytes | None:
        """Read up to one chunk, waiting at most ``timeout`` seconds.

        Returns non-empty ``bytes`` on output, ``None`` on a timeout with no
        output, and ``b""`` on EOF. See the module docstring for the contract.
        """
        raise NotImplementedError

    def close(self) -> None:
        """Release any parent-side resources (fds, reader thread)."""
        raise NotImplementedError


class PtyTransport(StreamTransport):
    """POSIX transport: child bound to a PTY slave, parent reads the master.

    Constructing this transport allocates a pseudo-terminal; when the host PTY
    pool is exhausted ``pty.openpty`` raises ``OSError`` here, which the streamer
    classifies via :mod:`agents.pty_diagnostics` and reports as a startup
    failure rather than a traceback.
    """

    def __init__(self) -> None:
        # POSIX-only import, kept lazy so ``import agents`` never touches ``pty``
        # on native Windows (CPython ships no ``pty`` module there).
        import pty

        self._master_fd, self._slave_fd = pty.openpty()

    def popen_stdio(self) -> dict[str, Any]:
        # Both stdin and stdout ride the slave end so the child sees a real TTY
        # on both and line-buffers its output.
        return {"stdin": self._slave_fd, "stdout": self._slave_fd}

    def after_spawn(self, proc: subprocess.Popen) -> None:  # noqa: ARG002 — parity
        # The parent only reads the master end; close the slave so the child is
        # the sole holder and EOF propagates when it exits.
        os.close(self._slave_fd)

    def read(self, timeout: float) -> bytes | None:
        import select

        try:
            rlist, _, _ = select.select([self._master_fd], [], [], timeout)
        except (ValueError, OSError):
            return b""  # master closed under us → treat as EOF
        if not rlist:
            return None  # no data within the wait window
        try:
            return os.read(self._master_fd, _READ_CHUNK)
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EBADF):
                return b""  # child closed the pty → EOF
            raise

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self._master_fd)


class PipeTransport(StreamTransport):
    """Windows-capable transport: child stdout is a pipe drained by a thread.

    ``select`` cannot wait on pipe handles on Windows, so a daemon reader thread
    performs blocking reads on the child's stdout and hands chunks to the parent
    through a queue. The parent's :meth:`read` waits on the queue with a timeout,
    preserving the same ``bytes`` / ``None`` / ``b""`` contract as the PTY path.

    The child receives no controlling terminal: stdin is ``DEVNULL`` and stdout
    is a plain pipe. Pure stdlib, so it also runs on POSIX for test coverage.
    """

    _EOF = object()  # sentinel enqueued once the reader thread hits end-of-stream

    def __init__(self) -> None:
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._eof_seen = False

    def popen_stdio(self) -> dict[str, Any]:
        # No TTY available: stdin is DEVNULL, stdout is a pipe we drain. The
        # child's stdout is therefore a plain pipe, so a CLI probing
        # ``sys.stdout.isatty()`` sees a non-interactive stream, as with any
        # piped invocation.
        return {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE}

    def after_spawn(self, proc: subprocess.Popen) -> None:
        stdout = proc.stdout
        if stdout is None:  # defensive: popen_stdio always requests a pipe
            self._queue.put(self._EOF)
            return
        self._thread = threading.Thread(
            target=self._drain,
            args=(stdout,),
            name="orcho-stream-reader",
            daemon=True,
        )
        self._thread.start()

    def _drain(self, stdout: Any) -> None:
        """Blocking-read the child's stdout until EOF, feeding the queue.

        ``read1`` returns as soon as one underlying read completes rather than
        waiting to fill a full buffer, so output streams line-by-line instead of
        arriving in one lump at exit. Raw/unbuffered pipes expose ``read``
        instead; fall back to it.
        """
        reader = getattr(stdout, "read1", None) or stdout.read
        try:
            while True:
                chunk = reader(_READ_CHUNK)
                if not chunk:
                    break  # EOF: every write end of the pipe has closed
                self._queue.put(chunk)
        except (OSError, ValueError):
            # Pipe torn down under us (child killed). Fall through to the
            # sentinel so the parent stops cleanly.
            pass
        finally:
            self._queue.put(self._EOF)

    def read(self, timeout: float) -> bytes | None:
        if self._eof_seen:
            return b""
        try:
            item = self._queue.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None  # no data within the wait window
        if item is self._EOF:
            self._eof_seen = True
            return b""
        return item

    def close(self) -> None:
        # The reader thread is a daemon draining a pipe owned by the Popen
        # object; it exits on EOF once the child's stdout closes. The caller's
        # ``proc.wait()`` reaps the child and closes the fd — nothing to force
        # here. Join briefly so a well-behaved thread is gone before we return.
        if self._thread is not None:
            self._thread.join(timeout=0.1)


def select_transport() -> StreamTransport:
    """Return the transport that matches the host.

    :class:`PtyTransport` when ``os.openpty`` is available (POSIX),
    :class:`PipeTransport` otherwise (native Windows). Constructing the PTY
    transport may raise ``OSError`` on PTY-pool exhaustion; the streamer catches
    it and renders the exhaustion diagnostic.
    """
    if _HAS_OPENPTY:
        return PtyTransport()
    return PipeTransport()


__all__ = [
    "PipeTransport",
    "PtyTransport",
    "StreamTransport",
    "select_transport",
]
