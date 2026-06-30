"""Codex ``codex exec --json`` JSONL parser.

Codex JSONL is a transport protocol. This module maps useful events into the
Orcho event store and formats the same lines into a compact transcript for
live stdout / output.log.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from agents.stream_parsers.skill_registry import current_registered_skill_names
from agents.stream_parsers.tool_invocations import (
    emit_tool_call,
    format_tool_call,
    normalize_builtin_tool,
    normalize_mcp_tool_call,
)
from core.io.transcript import detect_registered_skill_use, format_thinking_text
from core.observability import events as _events


def parse_codex_line(
    line: str,
    *,
    agent_label: str | None = None,
    skill_names: Iterable[str] | None = None,
) -> None:
    """Parse one Codex JSONL line and emit Orcho progress events."""
    event = _load_event(line)
    if event is None:
        return

    event_type = event.get("type")
    if event_type == "item.completed":
        item = event.get("item")
        if not isinstance(item, dict):
            return
        item_type = item.get("type")
        if item_type == "command_execution":
            command = _command_from_item(item)
            if command:
                emit_tool_call(
                    normalize_builtin_tool("Bash", {"command": command}),
                    agent_label=agent_label,
                )
            return
        if item_type == "mcp_tool_call":
            call = _normalize_codex_mcp_item(item)
            if call is not None:
                emit_tool_call(call, agent_label=agent_label)
            return
        if item_type == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                clean = text.strip()
                from core.io.stdout_render import (
                    classify_assistant_json_contract_chunk,
                    split_embedded_json_contract,
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
                # Recovery shape: prose summary trailed by a contract
                # object. Emit the prose as agent.text and mark the
                # contract ready so the feed mirrors the live transcript.
                prose, embedded = split_embedded_json_contract(clean)
                visible = prose.strip() if embedded else clean
                if visible:
                    _events.emit("agent.text", text=visible, agent=agent_label)
                    active_skills = (
                        skill_names
                        if skill_names is not None
                        else current_registered_skill_names()
                    )
                    skill_name = detect_registered_skill_use(visible, active_skills)
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

    if event_type == "turn.completed":
        usage = event.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        _events.emit(
            "agent.summary",
            agent=agent_label,
            usage=usage,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cached_input_tokens=usage.get("cached_input_tokens"),
            reasoning_output_tokens=usage.get("reasoning_output_tokens"),
        )


def format_codex_line_for_stdout(
    line: str,
    *,
    skill_names: Iterable[str] | None = None,
) -> str | None:
    """Return compact human-readable output for one Codex JSONL line."""
    event = _load_event(line)
    if event is None:
        return line

    event_type = event.get("type")
    if event_type in {
        "thread.started",
        "turn.started",
        "turn.completed",
        "item.started",
    }:
        return None

    if event_type == "item.completed":
        item = event.get("item")
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        if item_type == "command_execution":
            command = _command_from_item(item)
            if not command:
                return None
            return format_tool_call(
                normalize_builtin_tool("Bash", {"command": command}),
            )
        if item_type == "mcp_tool_call":
            call = _normalize_codex_mcp_item(item)
            if call is None:
                # Half-formed MCP item — parser intentionally drops it
                # (no half-emitted event), but the formatter falls through
                # to the diagnostic passthrough so a smoke run can still
                # see the raw line in transcript / output.log. Catches the
                # "Codex CLI uses a different MCP shape than the spec"
                # risk early.
                return line
            return format_tool_call(call)
        if item_type == "agent_message":
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                return None
            from core.io.stdout_render import (
                is_assistant_json_suppressed,
                render_assistant_text_to_stdout,
            )
            from core.observability.logging import get_verbose

            drop_json = is_assistant_json_suppressed() or not get_verbose()
            # Suppress a leading JSON contract or, in the recovery shape,
            # the trailing contract guts after a prose summary — keeping
            # the prose and a one-line marker. Debug (``drop_json`` off)
            # shows the raw response verbatim.
            visible, notice = render_assistant_text_to_stdout(
                text, drop_json=drop_json
            )
            parts: list[str] = []
            if visible is not None:
                active_skills = (
                    skill_names
                    if skill_names is not None
                    else current_registered_skill_names()
                )
                parts.append(
                    format_thinking_text(visible, skill_names=active_skills)
                )
            if notice:
                parts.append(format_thinking_text(notice))
            return "".join(parts) or None

    return line


def _load_event(line: str) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        event = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    return event if isinstance(event, dict) else None


def _normalize_codex_mcp_item(item: dict[str, Any]):
    """Translate a Codex ``mcp_tool_call`` item into the shared
    :class:`NormalizedToolCall` shape.

    Returns ``None`` when the item is missing both server and tool name
    (so the call is unrecoverable — silently drop rather than emit a
    half-formed event).
    """
    server = str(item.get("server") or "")
    tool = str(item.get("tool") or item.get("tool_name") or "")
    if not server or not tool:
        return None
    arguments = item.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    error = item.get("error")
    if not isinstance(error, dict):
        error = None
    status = str(item.get("status") or ("failed" if error else "completed"))
    return normalize_mcp_tool_call(
        server=server,
        tool_name=tool,
        arguments=arguments,
        status=status,
        error=error,
    )


def _command_from_item(item: dict[str, Any]) -> str:
    command = item.get("command")
    if isinstance(command, str):
        return command
    for key in ("aggregated_output", "text"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return ""
