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
    assert "omitted" in out
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
    assert "omitted" in decoded["output"]
    assert "B" * 100 in decoded["output"]


def test_oversized_assistant_line_is_returned_unchanged() -> None:
    plan = "# Plan\n" + ("- step with detail\n" * 8000)
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": plan}]},
    }) + "\n"
    assert utf8_len(line) > 96 * 1024

    out = elide_tool_result_line_for_model(line)

    assert out == line
    assert json.loads(out)["message"]["content"][0]["text"] == plan


def test_oversized_result_line_is_returned_unchanged() -> None:
    plan = "# Plan\n" + ("- step with detail\n" * 8000)
    line = json.dumps({
        "type": "result",
        "subtype": "success",
        "result": plan,
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }) + "\n"
    assert utf8_len(line) > 96 * 1024

    out = elide_tool_result_line_for_model(line)

    assert out == line
    assert json.loads(out)["result"] == plan


def test_oversized_tool_result_in_user_message_is_capped_but_valid_json() -> None:
    line = json.dumps({
        "type": "user",
        "message": {"content": [{
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": "C" * (2 * 1024 * 1024),
        }]},
    }) + "\n"

    out = elide_tool_result_line_for_model(line, max_bytes=64 * 1024)
    decoded = json.loads(out)

    assert utf8_len(out) <= 64 * 1024
    block = decoded["message"]["content"][0]
    assert block["tool_use_id"] == "toolu_1"
    assert "omitted" in block["content"]


def test_oversized_non_json_line_still_gets_byte_cap() -> None:
    line = "N" * (2 * 1024 * 1024) + "\n"

    out = elide_tool_result_line_for_model(line, max_bytes=64 * 1024)

    assert utf8_len(out) <= 64 * 1024 + 1
    assert "omitted" in out
    assert out.endswith("\n")


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
    assert "64 lines" in out
    assert "omitted" in out


def test_transcript_elision_leaves_small_text_unchanged() -> None:
    text = "tool output\nok\n"

    assert elide_tool_result_for_transcript(text, max_bytes=100, max_lines=10) == text
