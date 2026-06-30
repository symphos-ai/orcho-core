"""Byte-based output elision for agent tool-output surfaces."""

from __future__ import annotations

import json
from typing import Any

MODEL_TOOL_OUTPUT_MAX_BYTES = 96 * 1024
TRANSCRIPT_TOOL_RESULT_MAX_BYTES = 96 * 1024
TRANSCRIPT_TOOL_RESULT_MAX_LINES = 2_000
TRANSCRIPT_TOOL_RESULT_HEAD_LINES = 20
TRANSCRIPT_TOOL_RESULT_TAIL_LINES = 20

_OUTPUT_FIELD_NAMES = frozenset({
    "aggregated_output",
    "content",
    "error",
    "output",
    "result",
    "stderr",
    "stdout",
    "text",
})
_TOOL_ITEM_TYPES = frozenset({
    "command_execution",
    "function_call_output",
    "mcp_tool_call",
    "tool_result",
})
_TOOL_EVENT_TYPES = frozenset({
    "command_execution",
    "function_call_output",
    "mcp_tool_call",
    "tool_result",
})


def utf8_len(text: str) -> int:
    """Return the UTF-8 byte length for ``text``."""
    return len(text.encode("utf-8"))


def elide_middle_by_bytes(text: str, *, max_bytes: int) -> str:
    """Middle-elide ``text`` when its UTF-8 byte length exceeds ``max_bytes``."""
    if max_bytes <= 0 or utf8_len(text) <= max_bytes:
        return text

    raw = text.encode("utf-8")
    omitted_bytes = max(0, len(raw) - max_bytes)
    omitted_lines = text.count("\n")
    marker = (
        f"\n[… {format_bytes(omitted_bytes)} / "
        f"{format_count(omitted_lines)} строк вырезано …]\n"
    )
    marker_bytes = marker.encode("utf-8")
    remaining = max(0, max_bytes - len(marker_bytes))
    head_size = remaining // 2
    tail_size = remaining - head_size

    head = raw[:head_size].decode("utf-8", errors="ignore")
    tail = raw[len(raw) - tail_size:].decode("utf-8", errors="ignore")
    return f"{head}{marker}{tail}"


def elide_tool_result_line_for_model(
    line: str,
    *,
    max_bytes: int = MODEL_TOOL_OUTPUT_MAX_BYTES,
) -> str:
    """Cap one provider stdout line before retaining it for runtime consumers."""
    if utf8_len(line) <= max_bytes:
        return line

    body, newline = _split_trailing_newline(line)
    decoded = _load_json(body)
    if decoded is None:
        return elide_middle_by_bytes(body, max_bytes=max_bytes) + newline

    if _looks_like_tool_result(decoded):
        capped = _cap_tool_output_fields(decoded, max_bytes=max(1024, max_bytes // 2))
        rendered = json.dumps(capped, ensure_ascii=False, separators=(",", ":"))
        if utf8_len(rendered) <= max_bytes:
            return rendered + newline
        return elide_middle_by_bytes(rendered, max_bytes=max_bytes) + newline

    return elide_middle_by_bytes(body, max_bytes=max_bytes) + newline


def elide_tool_result_stream_for_model(
    text: str,
    *,
    max_bytes: int = MODEL_TOOL_OUTPUT_MAX_BYTES,
) -> str:
    """Cap provider stdout while preserving ordinary small lines exactly."""
    return "".join(
        elide_tool_result_line_for_model(line, max_bytes=max_bytes)
        for line in text.splitlines(keepends=True)
    )


def elide_text_for_model(
    text: str,
    *,
    max_bytes: int = MODEL_TOOL_OUTPUT_MAX_BYTES,
) -> str:
    """Cap unstructured stdout/stderr text before it can enter agent context."""
    return elide_middle_by_bytes(text, max_bytes=max_bytes)


def elide_tool_result_for_transcript(
    text: str,
    *,
    max_bytes: int = TRANSCRIPT_TOOL_RESULT_MAX_BYTES,
    max_lines: int = TRANSCRIPT_TOOL_RESULT_MAX_LINES,
    head_lines: int = TRANSCRIPT_TOOL_RESULT_HEAD_LINES,
    tail_lines: int = TRANSCRIPT_TOOL_RESULT_TAIL_LINES,
) -> str:
    """Presentation-only elision for oversized rendered tool results."""
    line_count = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    if utf8_len(text) <= max_bytes and line_count <= max_lines:
        return text
    lines = text.splitlines(keepends=True)
    if len(lines) > head_lines + tail_lines:
        head = "".join(lines[:head_lines])
        tail = "".join(lines[-tail_lines:])
        middle = "".join(lines[head_lines:-tail_lines])
        marker = (
            f"[… {format_count(len(lines) - head_lines - tail_lines)} строк / "
            f"{format_bytes(utf8_len(middle))} вырезано …]\n"
        )
        return f"{head}{marker}{tail}"
    return elide_middle_by_bytes(text, max_bytes=max_bytes)


def format_bytes(size: int) -> str:
    """Format bytes as a compact human-readable quantity."""
    if size < 1024:
        return f"{size} B"
    kb = size / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    return f"{kb / 1024:.1f} MB"


def format_count(value: int) -> str:
    """Format a count with a decimal suffix when it improves scanability."""
    if value < 1000:
        return str(value)
    return f"{value / 1000:.1f}k"


def _split_trailing_newline(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1], line[-1]
    return line, ""


def _load_json(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _looks_like_tool_result(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    event_type = value.get("type")
    if isinstance(event_type, str) and event_type in _TOOL_EVENT_TYPES:
        return True
    item = value.get("item")
    if isinstance(item, dict):
        item_type = item.get("type")
        if isinstance(item_type, str) and item_type in _TOOL_ITEM_TYPES:
            return True
    message = value.get("message")
    if isinstance(message, dict):
        for block in message.get("content", []) or ():
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return True
    return False


def _cap_tool_output_fields(value: Any, *, max_bytes: int) -> Any:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return [_cap_tool_output_fields(v, max_bytes=max_bytes) for v in value]
    if not isinstance(value, dict):
        return value

    capped: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, str) and key in _OUTPUT_FIELD_NAMES:
            capped[key] = elide_middle_by_bytes(item, max_bytes=max_bytes)
        else:
            capped[key] = _cap_tool_output_fields(item, max_bytes=max_bytes)
    return capped
