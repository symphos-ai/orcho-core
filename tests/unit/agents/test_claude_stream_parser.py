"""
JSONL → event-store mapping.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.stream_parsers.claude_jsonl import (
    format_claude_line_for_stdout,
    parse_claude_line,
)
from core.observability import events as evstore


@pytest.fixture(autouse=True)
def _store(tmp_path: Path):
    evstore.init_event_store(tmp_path)
    yield tmp_path
    evstore.init_event_store(None)


def _events_kinds(tmp_path: Path) -> list[str]:
    return [e.kind for e in evstore.read_all(tmp_path)]


def _last_event(tmp_path: Path):
    evs = evstore.read_all(tmp_path)
    return evs[-1] if evs else None


def test_blank_and_garbage_ignored(_store):
    parse_claude_line("")
    parse_claude_line("not-json")
    parse_claude_line("[")
    assert _events_kinds(_store) == []


def test_assistant_tool_use_bash(_store):
    line = json.dumps({
        "type": "assistant",
        "message": {
            "content": [{
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "ls -la /tmp", "description": "list tmp"},
            }],
        },
    })
    parse_claude_line(line, agent_label="PLAN")
    e = _last_event(_store)
    assert e.kind == "agent.tool_use"
    assert e.payload["tool_name"] == "Bash"
    assert e.payload["tool_category"] == "shell"
    assert e.payload["display_name"] == "Bash"
    assert e.payload["summary"] == "ls -la /tmp"
    assert e.payload["input"]["command"] == "ls -la /tmp"
    assert e.payload["agent"] == "PLAN"


def test_assistant_tool_use_grep(_store):
    line = json.dumps({
        "type": "assistant",
        "message": {
            "content": [{
                "type": "tool_use",
                "name": "Grep",
                "input": {"pattern": "TODO", "path": "/src"},
            }],
        },
    })
    parse_claude_line(line)
    e = _last_event(_store)
    assert e.payload["tool_category"] == "search"
    assert "TODO" in e.payload["summary"]
    assert "/src" in e.payload["summary"]


def test_assistant_tool_use_read(_store):
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use",
            "name": "Read",
            "input": {"file_path": "/x/y.py", "limit": 50},
        }]},
    })
    parse_claude_line(line)
    e = _last_event(_store)
    assert e.payload["tool_category"] == "file_read"
    assert e.payload["display_name"] == "Read"
    assert e.payload["summary"] == "/x/y.py"
    assert e.payload["input"]["limit"] == 50


def test_assistant_tool_use_mcp_routes_to_mcp_event(_store):
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use",
            "name": "mcp__orcho_demo_mcp__orcho_plan_validate",
            "input": {"task": "x"},
        }]},
    })
    parse_claude_line(line, agent_label="PLAN")
    e = _last_event(_store)
    assert e.kind == "agent.mcp_tool_call"
    assert e.payload["server"] == "orcho_demo_mcp"
    assert e.payload["tool_name"] == "orcho_plan_validate"
    assert e.payload["tool_category"] == "mcp"
    assert e.payload["display_name"] == "orcho_demo_mcp.orcho_plan_validate"
    assert e.payload["arguments"] == {"task": "x"}
    assert e.payload["status"] == "completed"
    assert e.payload["agent"] == "PLAN"


def test_assistant_mcp_tool_use_does_not_emit_builtin_tool_use(_store):
    """A single MCP tool_use must produce exactly one event, of the MCP kind."""
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use",
            "name": "mcp__orcho_demo_mcp__orcho_plan_validate",
            "input": {},
        }]},
    })
    parse_claude_line(line)
    kinds = _events_kinds(_store)
    assert kinds == ["agent.mcp_tool_call"]


def test_assistant_tool_use_todowrite(_store):
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use",
            "name": "TodoWrite",
            "input": {"todos": [
                {"status": "completed", "content": "a"},
                {"status": "in_progress", "content": "b"},
                {"status": "pending", "content": "c"},
                {"status": "pending", "content": "d"},
            ]},
        }]},
    })
    parse_claude_line(line)
    e = _last_event(_store)
    assert e.payload["summary"] == "1 done · 1 running · 2 pending"


def test_assistant_text_block(_store):
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "  Hello.  \n"},
        ]},
    })
    parse_claude_line(line)
    e = _last_event(_store)
    assert e.kind == "agent.text"
    assert e.payload["text"] == "Hello."


def test_assistant_text_skill_use_emits_skill_event(_store):
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {
                "type": "text",
                "text": "Использую `frontend-qa`, потому что задача про UI.",
            },
        ]},
    })
    parse_claude_line(line, agent_label="QA", skill_names={"frontend-qa"})
    events = evstore.read_all(_store)
    assert [event.kind for event in events] == ["agent.text", "agent.skill_use"]
    assert events[-1].payload["skill_name"] == "frontend-qa"
    assert events[-1].payload["agent"] == "QA"


def test_assistant_text_empty_skipped(_store):
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "   "},
        ]},
    })
    parse_claude_line(line)
    assert _events_kinds(_store) == []


def test_assistant_json_contract_emits_ready_event_not_text(_store):
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": '{"verdict":"APPROVED","findings":[]}'},
        ]},
    })
    parse_claude_line(line, agent_label="VALIDATE_PLAN")
    events = evstore.read_all(_store)
    assert [event.kind for event in events] == ["agent.contract_ready"]
    assert events[0].payload["agent"] == "VALIDATE_PLAN"
    assert events[0].payload["format"] == "json"


def test_assistant_pretty_json_contract_emits_ready_once(_store):
    for chunk in [
        "{\n",
        '  "verdict": "APPROVED",\n',
        '  "findings": []\n',
        "}\n",
    ]:
        parse_claude_line(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": chunk}]},
        }), agent_label="REVIEW")

    assert _events_kinds(_store) == ["agent.contract_ready"]


def test_multiple_blocks_in_one_message(_store):
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "Step 1"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}},
            {"type": "text", "text": "Done"},
        ]},
    })
    parse_claude_line(line)
    kinds = _events_kinds(_store)
    assert kinds == ["agent.text", "agent.tool_use", "agent.text"]


def test_result_emits_summary(_store):
    line = json.dumps({
        "type": "result",
        "subtype": "success",
        "stop_reason": "end_turn",
        "session_id": "abc-123",
        "total_cost_usd": 1.23,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    })
    parse_claude_line(line, agent_label="BUILD")
    e = _last_event(_store)
    assert e.kind == "agent.summary"
    assert e.payload["session_id"] == "abc-123"
    assert e.payload["cost_usd"] == 1.23
    assert e.payload["input_tokens"] == 100
    assert e.payload["output_tokens"] == 50
    assert e.payload["agent"] == "BUILD"


def test_result_summary_input_includes_cache_buckets(_store):
    line = json.dumps({
        "type": "result",
        "subtype": "success",
        "stop_reason": "end_turn",
        "session_id": "abc-123",
        "usage": {
            "input_tokens": 9,
            "cache_creation_input_tokens": 120,
            "cache_read_input_tokens": 15000,
            "output_tokens": 50,
        },
    })
    parse_claude_line(line)
    e = _last_event(_store)
    assert e.payload["input_tokens"] == 15129
    assert e.payload["fresh_input_tokens"] == 9
    assert e.payload["cache_creation_input_tokens"] == 120
    assert e.payload["cache_read_input_tokens"] == 15000


def test_unknown_message_type_ignored(_store):
    line = json.dumps({"type": "system", "subtype": "init"})
    parse_claude_line(line)
    assert _events_kinds(_store) == []


def test_unknown_tool_falls_back_to_flat(_store):
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use",
            "name": "FrobnicateXyz",
            "input": {"foo": "bar", "baz": 42},
        }]},
    })
    parse_claude_line(line)
    e = _last_event(_store)
    assert "foo=bar" in e.payload["summary"]
    assert "baz=42" in e.payload["summary"]


def test_stdout_formatter_hides_system_and_result_lines() -> None:
    assert format_claude_line_for_stdout(json.dumps({"type": "system"})) is None
    assert format_claude_line_for_stdout(json.dumps({"type": "result"})) is None


def test_stdout_formatter_renders_text_and_tool_use() -> None:
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "thinking", "thinking": "secret", "signature": "sig"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "calc.py"}},
            {"type": "text", "text": "Done."},
        ]},
    })

    out = format_claude_line_for_stdout(line)

    assert out is not None
    assert "📖 Read: calc.py" in out
    assert "💬 Assistant: Done." in out
    assert "secret" not in out
    assert "signature" not in out


def test_stdout_formatter_renders_registered_skill_use() -> None:
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [{
            "type": "text",
            "text": "Использую `frontend-qa`, потому что задача про UI.",
        }]},
    })
    out = format_claude_line_for_stdout(line, skill_names={"frontend-qa"})
    assert out == (
        "  🧠 Skill: frontend-qa — Использую `frontend-qa`, "
        "потому что задача про UI.\n"
    )


def test_stdout_formatter_renders_mcp_tool_use() -> None:
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use",
            "name": "mcp__orcho_demo_mcp__orcho_plan_validate",
            "input": {"task": "x"},
        }]},
    })
    out = format_claude_line_for_stdout(line)
    assert out is not None
    assert "🔌 MCP: orcho_demo_mcp.orcho_plan_validate" in out


def test_stdout_formatter_passes_through_non_json() -> None:
    assert format_claude_line_for_stdout("plain text\n") == "plain text\n"


def test_stdout_formatter_marks_json_text_when_not_verbose() -> None:
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": '{"verdict":"APPROVED","findings":[]}'},
        ]},
    })
    out = format_claude_line_for_stdout(line)
    assert out is not None
    assert "Contracted answer prepared" in out
    assert "verdict" not in out


def test_stdout_formatter_marks_pretty_json_chunks_once_when_not_verbose() -> None:
    chunks = [
        "{\n",
        '  "verdict": "APPROVED",\n',
        '  "findings": []\n',
        "}\n",
    ]
    rendered = [
        format_claude_line_for_stdout(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": chunk}]},
        }))
        for chunk in chunks
    ]
    assert rendered[0] is not None
    assert "Contracted answer prepared" in rendered[0]
    assert rendered[1:] == [None, None, None]


def test_stdout_formatter_keeps_json_text_in_debug() -> None:
    from core.observability.logging import set_verbose

    line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": '{"verdict":"APPROVED","findings":[]}'},
        ]},
    })
    set_verbose(True)
    try:
        out = format_claude_line_for_stdout(line)
    finally:
        set_verbose(False)
    assert out is not None
    assert '💬 Assistant: {"verdict":"APPROVED","findings":[]}' in out


def test_stdout_formatter_debug_still_honors_defer_assistant_json() -> None:
    from core.io.stdout_render import defer_assistant_json
    from core.observability.logging import set_verbose

    line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": '{"plan":"…"}'},
        ]},
    })
    set_verbose(True)
    try:
        with defer_assistant_json():
            out = format_claude_line_for_stdout(line)
            assert out is not None
            assert "Contracted answer prepared" in out
            assert "plan" not in out
    finally:
        set_verbose(False)


def test_stdout_formatter_keeps_prose_text_when_not_verbose() -> None:
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "Root cause / framing: the repo has no HTTP API."},
        ]},
    })
    out = format_claude_line_for_stdout(line)
    assert out is not None
    assert "💬 Assistant: Root cause / framing" in out


def test_stdout_formatter_tool_use_visible_in_all_modes() -> None:
    from core.observability.logging import set_verbose

    line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}},
        ]},
    })
    out_default = format_claude_line_for_stdout(line)
    set_verbose(True)
    try:
        out_debug = format_claude_line_for_stdout(line)
    finally:
        set_verbose(False)
    assert out_default is not None and "📖 Read: x.py" in out_default
    assert out_debug is not None and "📖 Read: x.py" in out_debug


# ── recovery shape: prose summary trailed by a JSON contract ──────────


_RECOVERY_TEXT = (
    "## Summary\n\n"
    "Edit-user flow implemented in src/main.ts.\n\n"
    '{"type":"subtask_attestation","subtask_id":"T1","summary":"done"}'
)


def test_stdout_formatter_keeps_prose_drops_trailing_contract() -> None:
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": _RECOVERY_TEXT}]},
    })
    out = format_claude_line_for_stdout(line)
    assert out is not None
    # human summary stays visible
    assert "## Summary" in out
    assert "Edit-user flow implemented" in out
    # the contract guts are replaced by the one-line marker
    assert "Contracted answer prepared" in out
    assert "subtask_attestation" not in out
    assert "subtask_id" not in out


def test_stdout_formatter_keeps_full_recovery_text_in_debug() -> None:
    from core.observability.logging import set_verbose

    line = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": _RECOVERY_TEXT}]},
    })
    set_verbose(True)
    try:
        out = format_claude_line_for_stdout(line)
    finally:
        set_verbose(False)
    assert out is not None
    # debug shows the raw response verbatim so recovery is observable
    assert "subtask_attestation" in out
    assert "Contracted answer prepared" not in out


def test_stdout_formatter_inline_json_example_not_suppressed() -> None:
    # A valid JSON object mid-prose with trailing text is NOT terminal, so
    # the block must render verbatim rather than being mistaken for a
    # contract.
    text = 'Send {"a": 1} to the endpoint, then verify the response.'
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    })
    out = format_claude_line_for_stdout(line)
    assert out is not None
    assert '{"a": 1}' in out
    assert "Contracted answer prepared" not in out


def test_parse_recovery_emits_prose_text_then_contract_ready(_store):
    parse_claude_line(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": _RECOVERY_TEXT}]},
    }), agent_label="IMPLEMENT")
    events = evstore.read_all(_store)
    assert [e.kind for e in events] == ["agent.text", "agent.contract_ready"]
    assert "Edit-user flow implemented" in events[0].payload["text"]
    assert "subtask_attestation" not in events[0].payload["text"]
    assert events[1].payload["format"] == "json"
    assert events[1].payload["agent"] == "IMPLEMENT"
