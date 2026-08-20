"""Unit coverage for asynchronous stdin prompt delivery."""
from __future__ import annotations

import io

from agents.stream_prompt import PromptStdinWriter


def test_writer_encodes_full_utf8_payload_and_closes_for_eof() -> None:
    class RecordingPipe(io.BytesIO):
        closed_by_writer = False

        def close(self) -> None:
            self.closed_by_writer = True

    stream = RecordingPipe()
    prompt = "ž" * 40000
    writer = PromptStdinWriter(stream, prompt)
    writer.start()
    writer.finish()
    assert stream.getvalue() == prompt.encode("utf-8")
    assert stream.closed_by_writer


def test_writer_suppresses_broken_pipe() -> None:
    class BrokenPipe:
        def write(self, _payload) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            raise AssertionError("flush is not reached")

        def close(self) -> None:
            pass

    writer = PromptStdinWriter(BrokenPipe(), "prompt")
    writer.start()
    writer.finish()
