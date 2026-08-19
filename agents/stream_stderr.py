"""Bounded concurrent stderr capture for streamed child processes.

stderr is deliberately kept separate from the stdout transport: runtime
adapters use stdout as a structured streaming protocol.  This reader drains
the stderr pipe as soon as a child is spawned, while retaining only a bounded
raw-byte tail for diagnostics.
"""
from __future__ import annotations

import contextlib
import threading
from collections import deque
from typing import BinaryIO

DEFAULT_STDERR_LIMIT = 4 * 1024 * 1024
_READ_SIZE = 64 * 1024


class BoundedStderrReader:
    """Drain a stderr pipe in a thread and retain its last ``limit`` bytes.

    ``total_bytes_read`` and ``dropped_bytes`` describe the raw stream, rather
    than the decoded presentation payload.  A lock makes snapshots safe while
    the child is still running.
    """

    def __init__(self, stream: BinaryIO | None, *, limit: int = DEFAULT_STDERR_LIMIT) -> None:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        self._stream = stream
        self._limit = limit
        self._chunks: deque[bytes] = deque()
        self._retained_bytes = 0
        self._lock = threading.Lock()
        self._total_bytes_read = 0
        self._dropped_bytes = 0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start draining, if a stderr pipe is available."""
        if self._stream is None or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._drain, name="orcho-stderr-reader", daemon=True,
        )
        self._thread.start()

    def _drain(self) -> None:
        assert self._stream is not None
        reader = getattr(self._stream, "read1", None) or self._stream.read
        try:
            while chunk := reader(_READ_SIZE):
                self._append(chunk)
        except (OSError, ValueError):
            # Settlement may close the pipe to unblock a reader inherited by a
            # descendant.  The bytes already observed remain authoritative.
            pass

    def _append(self, chunk: bytes) -> None:
        with self._lock:
            self._total_bytes_read += len(chunk)
            self._chunks.append(chunk)
            self._retained_bytes += len(chunk)
            overflow = self._retained_bytes - self._limit
            if overflow <= 0:
                return
            self._dropped_bytes += overflow
            while overflow:
                head = self._chunks.popleft()
                if len(head) <= overflow:
                    overflow -= len(head)
                    self._retained_bytes -= len(head)
                else:
                    self._chunks.appendleft(head[overflow:])
                    self._retained_bytes -= overflow
                    overflow = 0

    @property
    def total_bytes_read(self) -> int:
        with self._lock:
            return self._total_bytes_read

    @property
    def dropped_bytes(self) -> int:
        with self._lock:
            return self._dropped_bytes

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return self._retained_bytes

    def finish(self) -> str:
        """Join after child settlement and return the decoded bounded payload.

        A normal exited child closes stderr and lets the reader drain to EOF.
        If a descendant retains the descriptor, closing our read end prevents
        an exceptional cleanup path from leaking a reader thread indefinitely.
        """
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
            if thread.is_alive():
                self.close()
                thread.join(timeout=1.0)
        return self.text()

    def close(self) -> None:
        if self._stream is not None:
            with contextlib.suppress(OSError, ValueError):
                self._stream.close()

    def text(self) -> str:
        """Return a truncation note followed by the decoded retained raw tail."""
        with self._lock:
            payload = b"".join(self._chunks)
            dropped = self._dropped_bytes
        text = payload.decode("utf-8", errors="replace")
        if dropped:
            note = f"[stderr truncated: {dropped} bytes discarded]"
            return f"{note}\n{text}" if text else note
        return text


__all__ = ["BoundedStderrReader", "DEFAULT_STDERR_LIMIT"]
