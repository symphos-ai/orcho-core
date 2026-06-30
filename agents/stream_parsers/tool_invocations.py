"""Provider-neutral tool-invocation normalization.

Provider JSONL/stream parsers should normalize provider-specific tool-call
events into :class:`NormalizedToolCall` before emitting events or rendering
transcripts. Claude, Codex, and future Gemini adapters share this module so
the UI category/icon mapping stays provider-neutral — the on-disk event
payload carries a stable ``tool_category`` enum and consumers map that to
their own iconography, independent of the originating CLI's naming
conventions.

Two canonical event kinds flow through here:

* ``agent.tool_use``       — built-in tool calls (Bash, Read, Grep, …).
* ``agent.mcp_tool_call``  — MCP tool calls (any ``mcp::<server>::<tool>``
  invocation, whether routed via Claude's ``mcp__server__tool`` block names
  or Codex's structured ``mcp_tool_call`` items).

Exactly one event kind fires per tool call. No dual emit.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from core.io.ansi import C, paint
from core.observability import events as _events

ToolCategory = Literal[
    "shell",
    "file_read",
    "file_write",
    "search",
    "web",
    "task",
    "todo",
    "mcp",
    "unknown",
]


@dataclass(frozen=True)
class NormalizedToolCall:
    """Provider-neutral view of a single tool invocation.

    ``event_kind`` discriminates the canonical event the caller should
    emit. ``input`` carries the raw tool-input dict for built-ins;
    ``arguments`` carries the MCP-side arguments. The two are kept
    separate so MCP calls keep their canonical wire-format wording and
    built-ins keep theirs.
    """

    event_kind: str
    tool_category: ToolCategory
    tool_name: str
    display_name: str
    summary: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    server: str | None = None
    status: str | None = None
    arguments: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    #: Original absolute path of the primary file argument before run-relative
    #: normalisation. Populated by :func:`normalize_builtin_tool` for
    #: single-path tools (Read/Edit/Write/MultiEdit/NotebookEdit/Grep) so the
    #: live transcript can show ``api/teams.py (/tmp/.../checkout/api/teams.py)``
    #: — short path for cognitive scanning, full path in grey for context.
    #: ``None`` when no rewrite happened or the tool carries no single path.
    #: Renderer-only: never serialised into ``agent.tool_use`` events.
    abs_hint: str | None = None


#: Per-category transcript glyph. Rendered on the live stream and in
#: ``output.log`` so an operator can glance at a tool call and identify
#: its kind without reading the tool name. The UI layer maps the on-disk
#: ``tool_category`` to richer iconography independently.
_CATEGORY_ICON: dict[ToolCategory, str] = {
    "shell":      "⚡",
    "file_read":  "📖",
    "file_write": "📝",
    "search":     "🔍",
    "web":        "🌐",
    "task":       "🧩",
    "todo":       "📋",
    "mcp":        "🔌",
    "unknown":    "🛠",
}


_BUILTIN_CATEGORY: dict[str, ToolCategory] = {
    "Bash": "shell",
    "Read": "file_read",
    "Edit": "file_write",
    "MultiEdit": "file_write",
    "Write": "file_write",
    "NotebookEdit": "file_write",
    "Grep": "search",
    "Glob": "search",
    "WebFetch": "web",
    "WebSearch": "web",
    "Task": "task",
    "TodoWrite": "todo",
}


def categorize_builtin_tool(tool_name: str) -> ToolCategory:
    """Map a built-in tool name to its provider-neutral category."""
    return _BUILTIN_CATEGORY.get(tool_name, "unknown")


def parse_claude_mcp_tool_name(name: str) -> tuple[str, str] | None:
    """Split a Claude MCP tool name into ``(server, tool)``.

    Claude encodes MCP tool calls as built-in ``tool_use`` blocks with
    a ``name`` of the form ``mcp__<server>__<tool>``. Returns ``None``
    for non-MCP names so callers fall through to built-in normalization.
    """
    if not name.startswith("mcp__"):
        return None
    parts = name.split("__", 2)
    if len(parts) == 3 and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None


#: Built-in tools whose primary path argument carries cognitive value when
#: shown alongside the short relative form. Bash is intentionally absent —
#: its ``command`` is a free-form shell line that may carry multiple paths,
#: so a single "abs_hint" can't represent it usefully.
_SINGLE_PATH_FIELD: dict[str, str] = {
    "Read":         "file_path",
    "Edit":         "file_path",
    "MultiEdit":    "file_path",
    "Write":        "file_path",
    "NotebookEdit": "file_path",
    "Grep":         "path",
}


def normalize_builtin_tool(
    tool_name: str, inp: dict[str, Any],
) -> NormalizedToolCall:
    """Wrap a built-in tool call in the canonical normalized form.

    Path-like arguments under the active worktree are rewritten to
    relative form here so that every downstream consumer (events,
    stdout transcript, ``output.log``) sees the short cognitive path.

    For tools that carry a single primary file/path argument the original
    absolute path is captured in ``abs_hint`` so the live transcript can
    render the short form alongside the full path in grey — short for
    scanning, full for orientation.
    """
    orig_inp = inp
    inp = _maybe_rewrite_paths(inp)
    abs_hint: str | None = None
    field_name = _SINGLE_PATH_FIELD.get(tool_name)
    if field_name is not None:
        orig_val = orig_inp.get(field_name)
        new_val = inp.get(field_name)
        if (
            isinstance(orig_val, str)
            and isinstance(new_val, str)
            and orig_val != new_val
        ):
            abs_hint = orig_val
    return NormalizedToolCall(
        event_kind="agent.tool_use",
        tool_category=categorize_builtin_tool(tool_name),
        tool_name=tool_name,
        display_name=tool_name,
        summary=_summarize_builtin_input(tool_name, inp),
        input=inp,
        abs_hint=abs_hint,
    )


def normalize_mcp_tool_call(
    *,
    server: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    status: str | None = None,
    error: dict[str, Any] | None = None,
) -> NormalizedToolCall:
    """Wrap an MCP tool call in the canonical normalized form.

    See :func:`normalize_builtin_tool` for the path-normalization rule
    applied to ``arguments``.
    """
    arguments = (
        _maybe_rewrite_paths(arguments) if arguments is not None else {}
    )
    return NormalizedToolCall(
        event_kind="agent.mcp_tool_call",
        tool_category="mcp",
        tool_name=tool_name,
        display_name=f"{server}.{tool_name}",
        summary=f"{server}.{tool_name}",
        input={},
        server=server,
        status=status,
        arguments=arguments,
        error=error,
    )


def _active_run_roots() -> tuple[str, str] | None:
    """Return ``(checkout, run_dir)`` for the active worktree, or None.

    Lazy import keeps ``tool_invocations`` free of a hard ``pipeline``
    dependency at module load. ``checkout`` is the worktree checkout
    path the orchestrator set via ContextVar; ``run_dir`` is its
    parent (``runs/<id>/``). Trailing separators are stripped so the
    boundary regex below matches cleanly.
    """
    try:
        from pipeline.engine.worktree import get_active_worktree_checkout
    except Exception:
        return None
    checkout = get_active_worktree_checkout()
    if not checkout:
        return None
    checkout = checkout.rstrip("/")
    run_dir = os.path.dirname(checkout)
    return checkout, run_dir


def _rewrite_path_string(
    value: str, *, checkout: str, run_dir: str,
) -> str:
    """Strip the active run's path prefixes from ``value``.

    Two rewrites, longest-match-first:

    * ``<checkout>/foo`` → ``foo``; bare ``<checkout>`` → ``.``.
    * ``<run_dir>/foo``  → ``@run/foo``; bare ``<run_dir>`` → ``@run``.

    A lookahead boundary (``/``, end-of-string, whitespace, or quote)
    prevents partial matches like ``<checkout>_other`` from being
    rewritten. Substitution is unanchored so embedded paths inside
    Bash command strings (``cd <checkout>/api && ls <checkout>``)
    collapse correctly.
    """
    if checkout:
        value = re.sub(re.escape(checkout) + "/", "", value)
        value = re.sub(
            re.escape(checkout) + r"(?=\s|$|[\"'])", ".", value,
        )
    if run_dir:
        value = re.sub(re.escape(run_dir) + "/", "@run/", value)
        value = re.sub(
            re.escape(run_dir) + r"(?=\s|$|[\"'])", "@run", value,
        )
    return value


def _rewrite_value(value: Any, *, checkout: str, run_dir: str) -> Any:
    """Recursively rewrite path-like strings within ``value``."""
    if isinstance(value, str):
        return _rewrite_path_string(
            value, checkout=checkout, run_dir=run_dir,
        )
    if isinstance(value, dict):
        return {
            k: _rewrite_value(v, checkout=checkout, run_dir=run_dir)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _rewrite_value(v, checkout=checkout, run_dir=run_dir)
            for v in value
        ]
    return value


def _maybe_rewrite_paths(value: Any) -> Any:
    """Rewrite path-like strings in ``value`` for the active worktree.

    No-op when no worktree is active for the current execution context
    (``get_active_worktree_checkout()`` returns ``None``) — direct
    development tool calls outside an Orcho run keep absolute paths.

    Called at NormalizedToolCall construction time so every downstream
    consumer (event store, stdout transcript, ``output.log``) sees the
    short cognitive form: ``<checkout>/api/teams.py`` → ``api/teams.py``,
    ``<run_dir>/evidence/foo.json`` → ``@run/evidence/foo.json``.
    """
    roots = _active_run_roots()
    if roots is None:
        return value
    checkout, run_dir = roots
    return _rewrite_value(value, checkout=checkout, run_dir=run_dir)


def emit_tool_call(
    call: NormalizedToolCall, *, agent_label: str | None = None,
) -> None:
    """Emit exactly one canonical event for ``call``.

    Built-in calls produce ``agent.tool_use`` with the new
    ``tool_category`` / ``display_name`` fields layered on as optional
    additions — the event-kind required-key contract is unchanged so
    existing consumers keep working. MCP calls produce
    ``agent.mcp_tool_call`` with the MCP-specific payload.
    """
    if call.event_kind == "agent.tool_use":
        _events.emit(
            "agent.tool_use",
            tool_name=call.tool_name,
            tool_category=call.tool_category,
            display_name=call.display_name,
            summary=call.summary,
            input=call.input,
            agent=agent_label,
        )
        return
    if call.event_kind == "agent.mcp_tool_call":
        _events.emit(
            "agent.mcp_tool_call",
            server=call.server or "",
            tool_name=call.tool_name,
            tool_category="mcp",
            display_name=call.display_name,
            summary=call.summary,
            arguments=call.arguments if call.arguments is not None else {},
            status=call.status or "completed",
            error=call.error,
            agent=agent_label,
        )
        return


def format_tool_call(call: NormalizedToolCall) -> str:
    """Render ``call`` as a compact transcript line.

    Built-in: ``"  <icon> <display_name>: <summary>\\n"`` (or just
    ``"  <icon> <display_name>\\n"`` when no summary), where ``<icon>``
    comes from :data:`_CATEGORY_ICON` keyed by ``tool_category``. When
    ``call.abs_hint`` is set (single-path tool whose path was rewritten),
    the full absolute path is appended in grey parentheses so the
    operator can orient at a glance while the short form stays scannable.
    MCP: ``"  🔌 MCP: <server>.<tool>"`` with a ``" failed: <message>"``
    suffix when the call carried an error payload with a ``message``
    field. The literal ``MCP:`` marker stays so the line is
    self-describing even on terminals that drop the glyph.
    """
    if call.event_kind == "agent.tool_use":
        icon = _CATEGORY_ICON.get(call.tool_category, _CATEGORY_ICON["unknown"])
        if call.summary:
            base = f"  {icon} {call.display_name}: {call.summary}"
        else:
            base = f"  {icon} {call.display_name}"
        if call.abs_hint:
            base += f" {paint(f'({call.abs_hint})', C.GREY)}"
        return base + "\n"
    if call.event_kind == "agent.mcp_tool_call":
        icon = _CATEGORY_ICON["mcp"]
        base = f"  {icon} MCP: {call.display_name}"
        if call.error and isinstance(call.error, dict):
            message = call.error.get("message")
            if isinstance(message, str) and message:
                return f"{base} failed: {message}\n"
        return f"{base}\n"
    return ""


# ---------------------------------------------------------------------------
# Built-in tool summary rules — preserved from the legacy Claude parser.
# ---------------------------------------------------------------------------


def _trunc(value: str, limit: int = 200) -> str:
    normalized = value.replace("\n", " ").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "…"


def _summarize_builtin_input(tool_name: str, inp: dict[str, Any]) -> str:
    if tool_name == "Bash":
        return _trunc(str(inp.get("command", "")))
    if tool_name == "Read":
        return str(inp.get("file_path", ""))
    if tool_name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return str(inp.get("file_path", ""))
    if tool_name == "Grep":
        pat = str(inp.get("pattern", ""))
        path = str(inp.get("path", "."))
        return f"{pat!r} in {path}"
    if tool_name == "Glob":
        return str(inp.get("pattern", ""))
    if tool_name == "TodoWrite":
        todos = inp.get("todos") or []
        n_done = sum(1 for t in todos if (t or {}).get("status") == "completed")
        n_run = sum(1 for t in todos if (t or {}).get("status") == "in_progress")
        n_todo = sum(1 for t in todos if (t or {}).get("status") == "pending")
        return f"{n_done} done · {n_run} running · {n_todo} pending"
    if tool_name == "Task":
        return _trunc(str(inp.get("description", inp.get("prompt", ""))))
    if tool_name == "WebFetch":
        return str(inp.get("url", ""))
    if tool_name == "WebSearch":
        return _trunc(str(inp.get("query", "")))

    flat = ", ".join(
        f"{k}={_trunc(str(v), 60)}" for k, v in list(inp.items())[:3]
    )
    return _trunc(flat)
