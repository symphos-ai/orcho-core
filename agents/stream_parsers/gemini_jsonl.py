"""
agents/stream_parsers/gemini_jsonl.py — Google Gemini CLI
``-o stream-json`` parser. Each line of the CLI's stdout is a JSON object;
we map relevant events to the same event-store surface that the Claude
and Codex parsers emit.

Schema we care about (``gemini`` CLI 0.40 as of 2026-05):

    {"type":"init",        "session_id":"<uuid>", "model":"..."}
    {"type":"message",     "role":"user|assistant", "content":"...", "delta":true?}
    {"type":"tool_use",    "tool_name":"...", "tool_id":"...", "parameters":{...}}
    {"type":"tool_result", "tool_id":"...", "status":"...", "output":"..."}
    {"type":"result",      "status":"success", "stats":{
        "total_tokens": int, "input_tokens": int, "output_tokens": int,
        "cached": int, "input": int, "duration_ms": int, "tool_calls": int,
        "models": {"<id>": {...}},
    }}

Lines that don't match (init / tool_result / user echo / partial deltas of
something else) are silently ignored — we only need progress signal, not
faithful replay of the protocol.

The parser does NOT raise on malformed JSON; it returns silently. Callers
should pass each raw line and let the parser decide what to emit.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from agents.stream_parsers.skill_registry import current_registered_skill_names
from agents.stream_parsers.tool_invocations import (
    NormalizedToolCall,
    emit_tool_call,
    format_tool_call,
    normalize_builtin_tool,
)
from core.io.transcript import detect_registered_skill_use, format_thinking_text
from core.observability import events as _events


def parse_gemini_line(
    line: str,
    *,
    agent_label: str | None = None,
    skill_names: Iterable[str] | None = None,
) -> None:
    """Parse one line of Gemini stream-json and emit events.

    Args:
        line:        Raw line from Gemini's stdout (one JSON object).
        agent_label: Optional label to attach to events (e.g. "plan",
                     "implement") so the event-store carries phase context.
    """
    line = line.strip()
    if not line or not line.startswith("{"):
        return
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return

    msg_type = d.get("type")

    if msg_type == "tool_use":
        _emit_tool_use(d, agent_label=agent_label)
        return

    if msg_type == "message":
        if d.get("role") != "assistant":
            return
        content = d.get("content")
        if not isinstance(content, str) or not content.strip():
            return
        clean = content.strip()
        from core.io.stdout_render import (
            classify_assistant_json_contract_chunk,
        )
        is_contract, is_first = classify_assistant_json_contract_chunk(clean)
        if is_contract:
            if is_first:
                _events.emit(
                    "agent.contract_ready",
                    agent=agent_label,
                    format="json",
                )
            return
        _events.emit("agent.text", text=clean, agent=agent_label)
        active_skills = (
            skill_names
            if skill_names is not None
            else current_registered_skill_names()
        )
        skill_name = detect_registered_skill_use(clean, active_skills)
        if skill_name:
            _events.emit(
                "agent.skill_use",
                skill_name=skill_name,
                text=clean,
                agent=agent_label,
            )
        return

    if msg_type == "result":
        stats = d.get("stats") or {}
        if not isinstance(stats, dict):
            stats = {}
        # Gemini CLI's ``stats.input_tokens`` already includes the cached
        # input bucket (``cached`` is a subset breakdown, not additive).
        # Treat ``cached`` as the cache-read scope and derive fresh from
        # the difference so the event payload matches the Claude shape
        # consumers already know. ``max(0, ...)`` clamps the degenerate
        # ``cached > input_tokens`` case to zero fresh input.
        input_tokens = _safe_int(stats.get("input_tokens"))
        cached = _safe_int(stats.get("cached"))
        fresh = max(0, input_tokens - cached)
        _events.emit(
            "agent.summary",
            agent=agent_label,
            session_id=d.get("session_id"),
            # Gemini CLI does not report cost; left None so downstream
            # accounting falls back to the configured rate card.
            cost_usd=None,
            usage=stats,
            input_tokens=input_tokens,
            fresh_input_tokens=fresh,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=cached,
            output_tokens=stats.get("output_tokens"),
            stop_reason=d.get("status"),
        )
        return

    # init / message(user) / tool_result / unknown — ignored.


def format_gemini_line_for_stdout(
    line: str,
    *,
    skill_names: Iterable[str] | None = None,
) -> str | None:
    """Return compact human-readable output for Gemini stream-json lines.

    Used as both ``stdout_filter`` and ``log_filter`` on ``_stream_run``:
    known envelopes (``tool_use``, ``message`` with ``role=assistant``)
    are either formatted into a compact line or suppressed on both
    surfaces. Other events (``init`` / ``tool_result`` / user echoes /
    ``result``) are dropped from the live transcript and ``output.log``.
    Unknown / non-JSON lines pass through verbatim so diagnostic CLI
    output remains visible (e.g. the Node.js terminal-warning lines the
    CLI prints before the JSON stream begins).
    """
    raw = line
    stripped = line.strip()
    if not stripped:
        return None
    if not stripped.startswith("{"):
        return raw
    try:
        d = json.loads(stripped)
    except json.JSONDecodeError:
        return raw

    msg_type = d.get("type")

    if msg_type == "tool_use":
        return format_tool_call(_normalize_tool_use_event(d))

    if msg_type == "message":
        if d.get("role") != "assistant":
            return None
        content = d.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        from core.io.stdout_render import (
            consume_assistant_json_notice,
            is_assistant_json_suppressed,
            should_suppress_assistant_text,
        )
        from core.observability.logging import get_verbose
        drop_json = is_assistant_json_suppressed() or not get_verbose()
        if should_suppress_assistant_text(content, drop_json=drop_json):
            notice = consume_assistant_json_notice()
            if notice:
                return format_thinking_text(notice)
            return None
        active_skills = (
            skill_names
            if skill_names is not None
            else current_registered_skill_names()
        )
        return format_thinking_text(content, skill_names=active_skills)

    # init / tool_result / result — suppressed on both surfaces.
    return None


def _normalize_tool_use_event(event: dict) -> NormalizedToolCall:
    """Normalize a Gemini ``tool_use`` event into the shared
    :class:`agents.stream_parsers.tool_invocations.NormalizedToolCall`.

    Gemini CLI 0.40 emits built-in tools only (``read_file``, ``write_file``,
    ``run_shell_command``, ``glob``, etc.). MCP tool routing through the
    Gemini CLI lands on the same surface — when MCP integration is added
    this helper grows an ``mcp_<server>_<tool>`` parser, mirroring the
    Claude path.
    """
    name = str(event.get("tool_name") or "tool")
    params = event.get("parameters")
    if not isinstance(params, dict):
        params = {}
    return normalize_builtin_tool(name, params)


def _emit_tool_use(event: dict, *, agent_label: str | None) -> None:
    """Emit exactly one event for a Gemini ``tool_use`` envelope."""
    emit_tool_call(_normalize_tool_use_event(event), agent_label=agent_label)


def _safe_int(value: object) -> int:
    """Best-effort integer coercion mirroring the Claude parser helper."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
