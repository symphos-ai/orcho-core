from __future__ import annotations

import io

from agents.stream_stderr import BoundedStderrReader


def test_reader_retains_raw_tail_and_reports_exact_dropped_bytes() -> None:
    reader = BoundedStderrReader(io.BytesIO(b"0123456789"), limit=4)
    reader.start()
    text = reader.finish()

    assert reader.total_bytes_read == 10
    assert reader.dropped_bytes == 6
    assert reader.retained_bytes == 4
    assert text.startswith("[stderr truncated: 6 bytes discarded]\n")
    assert text.removeprefix("[stderr truncated: 6 bytes discarded]\n") == "6789"


def test_reader_decodes_only_after_bounding_raw_bytes_at_utf8_boundary() -> None:
    # The retained tail starts inside a UTF-8 sequence.  It is bounded as raw
    # bytes, so accounting stays exact and decoding safely replaces the split.
    raw = b"x" + "€tail".encode()
    reader = BoundedStderrReader(io.BytesIO(raw), limit=len(raw) - 2)
    reader.start()

    text = reader.finish()

    assert reader.total_bytes_read == len(raw)
    assert reader.dropped_bytes == 2
    assert text.startswith("[stderr truncated: 2 bytes discarded]\n")
    assert text.removeprefix("[stderr truncated: 2 bytes discarded]\n") == "��tail"
