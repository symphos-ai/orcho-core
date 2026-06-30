"""
agents/stream_parsers/claude_jsonl.py — Claude `--output-format stream-json`
parser. Each line of the CLI's stdout is a JSON object; we map relevant
content blocks to event-store events.

Schema we care about (Claude Code CLI as of 2026-05):
    {
      "type": "assistant",
      "message": {
        "content": [
          {"type": "tool_use", "name": "Bash", "input": {...}},
          {"type": "text", "text": "..."},
        ],
        "stop_reason": null | "end_turn",
        ...
      }
    }
    {
      "type": "result",
      "subtype": "success",
      "stop_reason": "end_turn",
      "session_id": "...",
      "total_cost_usd": 4.84,
      "usage": {...}
    }

Lines that don't match (system messages, partial deltas, anything not
"assistant" or "result") are silently ignored — we only need progress signal,
not faithful replay of the protocol.

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
    normalize_mcp_tool_call,
    parse_claude_mcp_tool_name,
)
from core.io.transcript import detect_registered_skill_use, format_thinking_text
from core.observability import events as _events


def parse_claude_line(
    line: str,
    *,
    agent_label: str | None = None,
    skill_names: Iterable[str] | None = None,
) -> None:
    """Parse one line of Claude stream-json and emit events.

    Args:
        line:        Raw line from Claude's stdout (one JSON object).
        agent_label: Optional label to attach to events (e.g. "plan", "implement")
                     so the event-store carries phase context even outside
                     phase.start/end boundaries.
    """
    line = line.strip()
    if not line or not line.startswith("{"):
        return
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return

    msg_type = d.get("type")

    if msg_type == "assistant":
        msg = d.get("message")
        if not isinstance(msg, dict):
            return
        for c in msg.get("content", []) or []:
            if not isinstance(c, dict):
                continue
            ctype = c.get("type")
            if ctype == "tool_use":
                _emit_tool_use(c, agent_label=agent_label)
            elif ctype == "text":
                text = c.get("text") or ""
                if text.strip():
                    clean = text.strip()
                    from core.io.stdout_render import (
                        classify_assistant_json_contract_chunk,
                        split_embedded_json_contract,
                    )
                    is_contract, is_first = (
                        classify_assistant_json_contract_chunk(clean)
                    )
                    if is_contract:
                        if is_first:
                            _events.emit(
                                "agent.contract_ready",
                                agent=agent_label,
                                format="json",
                            )
                        continue
                    # Recovery shape: prose summary trailed by a contract
                    # object. Surface the prose as agent.text and mark the
                    # contract ready, mirroring the live transcript so the
                    # dashboard/MCP feed never carries the raw JSON guts.
                    prose, embedded = split_embedded_json_contract(clean)
                    visible = prose.strip() if embedded else clean
                    if visible:
                        _events.emit("agent.text",
                                     text=visible,
                                     agent=agent_label)
                        active_skills = (
                            skill_names
                            if skill_names is not None
                            else current_registered_skill_names()
                        )
                        skill_name = detect_registered_skill_use(
                            visible, active_skills
                        )
                        if skill_name:
                            _events.emit(
                                "agent.skill_use",
                                skill_name=skill_name,
                                text=visible,
                                agent=agent_label,
                            )
                    if embedded:
                        _events.emit(
                            "agent.contract_ready",
                            agent=agent_label,
                            format="json",
                        )
        return

    if msg_type == "result":
        # Final summary line — capture cost / tokens for analytics, but
        # don't double-emit agent.end (the agent caller does that with
        # actual return code from _stream_run).
        usage = d.get("usage") or {}
        fresh = int(usage.get("input_tokens") or 0)
        cache_create = int(usage.get("cache_creation_input_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        total_input = fresh + cache_create + cache_read
        _events.emit("agent.summary",
                     agent=agent_label,
                     session_id=d.get("session_id"),
                     cost_usd=d.get("total_cost_usd"),
                     usage=usage,
                     input_tokens=total_input,
                     fresh_input_tokens=fresh,
                     cache_creation_input_tokens=cache_create,
                     cache_read_input_tokens=cache_read,
                     output_tokens=usage.get("output_tokens"),
                     stop_reason=d.get("stop_reason"))
        return

    # Other types (system, user, tool_result echoes, etc.) — ignored.


def format_claude_line_for_stdout(
    line: str,
    *,
    skill_names: Iterable[str] | None = None,
) -> str | None:
    """Return compact human-readable output for Claude stream-json lines.

    Used as both ``stdout_filter`` and ``log_filter`` on ``_stream_run``:
    known Claude stream-json envelopes (``assistant``, ``result``,
    ``system``) are either formatted into a compact line or suppressed
    on both surfaces; thinking signatures, tool-result echoes, and usage
    blobs stay out of transcript and ``output.log``. Unknown / non-JSON
    lines pass through verbatim so diagnostic CLI output remains visible.
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

    if d.get("type") != "assistant":
        return None

    msg = d.get("message")
    if not isinstance(msg, dict):
        return None

    from core.io.stdout_render import (
        is_assistant_json_suppressed,
        render_assistant_text_to_stdout,
    )
    from core.observability.logging import get_verbose
    drop_json = is_assistant_json_suppressed() or not get_verbose()

    out: list[str] = []
    for c in msg.get("content", []) or []:
        if not isinstance(c, dict):
            continue
        ctype = c.get("type")
        if ctype == "text":
            text = str(c.get("text") or "")
            if not text.strip():
                continue
            # JSON-shaped assistant text is suppressed from live stdout
            # unless ``--output debug`` is active. This covers two shapes:
            # a leading JSON contract (whole block) and the recovery shape
            # — prose followed by a trailing contract object, where the
            # prose stays visible and only the JSON guts are replaced by
            # the one-line marker. ``defer_assistant_json()`` remains a
            # per-phase override that forces the drop even in debug mode.
            # ``output.log`` always retains the raw text via the upstream
            # ``_stream_run`` writer, so nothing is lost forensically.
            visible, notice = render_assistant_text_to_stdout(
                text, drop_json=drop_json
            )
            if visible is not None:
                active_skills = (
                    skill_names
                    if skill_names is not None
                    else current_registered_skill_names()
                )
                out.append(
                    format_thinking_text(visible, skill_names=active_skills)
                )
            if notice:
                out.append(format_thinking_text(notice))
        elif ctype == "tool_use":
            out.append(format_tool_call(_normalize_tool_use_block(c)))
        # Thinking/signature blocks are intentionally hidden.

    return "".join(out) or None


def _normalize_tool_use_block(block: dict) -> NormalizedToolCall:
    """Normalize a Claude ``tool_use`` content block into the shared
    :class:`agents.stream_parsers.tool_invocations.NormalizedToolCall`.

    MCP tool calls arrive as ``tool_use`` blocks whose ``name`` follows
    the ``mcp__<server>__<tool>`` convention; everything else is a
    built-in tool whose category comes from the shared map.
    """
    name = str(block.get("name") or "tool")
    inp = block.get("input")
    if not isinstance(inp, dict):
        inp = {}
    mcp = parse_claude_mcp_tool_name(name)
    if mcp:
        server, tool = mcp
        return normalize_mcp_tool_call(
            server=server,
            tool_name=tool,
            arguments=inp,
            status="completed",
        )
    return normalize_builtin_tool(name, inp)


def _emit_tool_use(block: dict, *, agent_label: str | None) -> None:
    """Emit exactly one event for a Claude ``tool_use`` block.

    Built-in tools route to ``agent.tool_use``; MCP tool calls route to
    ``agent.mcp_tool_call`` via the shared normalization layer so the UI
    sees a stable ``tool_category`` regardless of provider.
    """
    emit_tool_call(_normalize_tool_use_block(block), agent_label=agent_label)
