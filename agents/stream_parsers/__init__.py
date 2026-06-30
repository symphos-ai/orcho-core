"""agents/stream_parsers — JSONL → event-store mappers per provider.

Public functions return None or emit directly via core.observability.events.
Shared normalization for tool invocations lives in
:mod:`agents.stream_parsers.tool_invocations` so Claude, Codex, Gemini, and
future provider adapters emit a provider-neutral ``tool_category`` on every
tool call.
"""
from agents.stream_parsers.claude_jsonl import (
    format_claude_line_for_stdout,
    parse_claude_line,
)
from agents.stream_parsers.codex_jsonl import (
    format_codex_line_for_stdout,
    parse_codex_line,
)
from agents.stream_parsers.gemini_jsonl import (
    format_gemini_line_for_stdout,
    parse_gemini_line,
)
from agents.stream_parsers.tool_invocations import (
    NormalizedToolCall,
    ToolCategory,
    categorize_builtin_tool,
    emit_tool_call,
    format_tool_call,
    normalize_builtin_tool,
    normalize_mcp_tool_call,
    parse_claude_mcp_tool_name,
)

__all__ = [
    "NormalizedToolCall",
    "ToolCategory",
    "categorize_builtin_tool",
    "emit_tool_call",
    "format_claude_line_for_stdout",
    "format_codex_line_for_stdout",
    "format_gemini_line_for_stdout",
    "format_tool_call",
    "normalize_builtin_tool",
    "normalize_mcp_tool_call",
    "parse_claude_line",
    "parse_claude_mcp_tool_name",
    "parse_codex_line",
    "parse_gemini_line",
]
