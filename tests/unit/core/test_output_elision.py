"""Output elision guards for oversized tool results."""

from __future__ import annotations

import json

from core.io.output_elision import (
    elide_middle_by_bytes,
    elide_tool_result_for_transcript,
    elide_tool_result_line_for_model,
    utf8_len,
)


def test_byte_cap_elides_single_line_blob_with_marker() -> None:
    blob = "A" * (2 * 1024 * 1024)

    out = elide_middle_by_bytes(blob, max_bytes=64 * 1024)

    assert utf8_len(out) <= 64 * 1024
    assert "вырезано" in out
    assert out.startswith("A" * 100)
    assert out.endswith("A" * 100)


def test_byte_cap_leaves_small_input_unchanged() -> None:
    text = "small grep result\n"

    assert elide_middle_by_bytes(text, max_bytes=64 * 1024) == text


def test_tool_result_line_caps_json_output_field() -> None:
    line = json.dumps({
        "type": "tool_result",
        "tool_id": "run-shell",
        "status": "success",
        "output": "B" * (2 * 1024 * 1024),
    }) + "\n"

    out = elide_tool_result_line_for_model(line, max_bytes=64 * 1024)
    decoded = json.loads(out)

    assert utf8_len(out) <= 64 * 1024
    assert decoded["tool_id"] == "run-shell"
    assert "вырезано" in decoded["output"]
    assert "B" * 100 in decoded["output"]


def test_transcript_elision_uses_head_tail_lines_and_marker() -> None:
    text = "".join(f"line-{i}\n" for i in range(70))

    out = elide_tool_result_for_transcript(
        text,
        max_bytes=10 * 1024,
        max_lines=40,
        head_lines=3,
        tail_lines=3,
    )

    assert "line-0\nline-1\nline-2\n" in out
    assert "line-67\nline-68\nline-69\n" in out
    assert "line-30" not in out
    assert "64 строк" in out
    assert "вырезано" in out


def test_transcript_elision_leaves_small_text_unchanged() -> None:
    text = "tool output\nok\n"

    assert elide_tool_result_for_transcript(text, max_bytes=100, max_lines=10) == text
