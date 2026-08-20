"""Asynchronous prompt delivery for streamed runtime invocations.

The writer owns a single child stdin pipe.  It is deliberately independent of
the stream read loop: writing a large prompt synchronously can fill the pipe
before the child starts draining stdout/stderr, deadlocking both processes.
"""
from __future__ import annotations

import contextlib
import os
import threading
from typing import BinaryIO, Literal

PromptDeliveryMode = Literal["argv", "stdin"]


class PromptStdinWriter:
    """Write one UTF-8 prompt to stdin in a daemon thread, then send EOF."""

    def __init__(self, stream: BinaryIO | None, prompt: str) -> None:
        self._stream = stream
        self._payload = prompt.encode("utf-8")
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._stream is None or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._write_and_close, name="orcho-prompt-writer", daemon=True,
        )
        self._thread.start()

    def _write_and_close(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            view = memoryview(self._payload)
            while view:
                written = stream.write(view)
                # Buffered binary streams normally return an int; accepting
                # ``None`` also supports simple test and adapter file seams.
                if written is None:
                    break
                if written <= 0:
                    break
                view = view[written:]
            stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            # A provider may exit before consuming all input. Its return code
            # and drained stderr remain the authoritative failure surfaces.
            pass
        finally:
            self.close()

    def finish(self) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
        self.close()

    def close(self) -> None:
        if self._stream is not None:
            with contextlib.suppress(BrokenPipeError, OSError, ValueError):
                self._stream.close()


def roundtrip_prompt_via_stdin(prompt: str) -> str:
    """Return *prompt* after the same writer/EOF contract used by a child.

    Mock runtimes use this hermetic pipe-backed consumer to exercise prompt
    delivery without spawning a provider binary.  Reading starts immediately
    after the daemon writer starts, so prompts larger than a pipe buffer drain
    concurrently rather than blocking the caller.
    """
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb")
    writer_stream = os.fdopen(write_fd, "wb")
    writer = PromptStdinWriter(writer_stream, prompt)
    writer.start()
    try:
        payload = reader.read()
    finally:
        writer.finish()
        reader.close()
    return payload.decode("utf-8")


__all__ = ["PromptDeliveryMode", "PromptStdinWriter", "roundtrip_prompt_via_stdin"]
