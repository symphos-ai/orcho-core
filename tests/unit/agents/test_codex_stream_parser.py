"""Codex JSONL stream parser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.stream_parsers.codex_jsonl import (
    format_codex_line_for_stdout,
    parse_codex_line,
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


def test_parse_blank_and_garbage_ignored(_store) -> None:
    parse_codex_line("")
    parse_codex_line("not-json")
    parse_codex_line("[")
    assert _events_kinds(_store) == []


def test_parse_command_execution_emits_tool_use(_store) -> None:
    line = json.dumps({
        "type": "item.completed",
        "item": {"type": "command_execution", "command": "pytest -q"},
    })
    parse_codex_line(line, agent_label="REVIEW")
    event = _last_event(_store)
    assert event.kind == "agent.tool_use"
    assert event.payload["tool_name"] == "Bash"
    assert event.payload["tool_category"] == "shell"
    assert event.payload["display_name"] == "Bash"
    assert event.payload["summary"] == "pytest -q"
    assert event.payload["input"] == {"command": "pytest -q"}
    assert event.payload["agent"] == "REVIEW"


def test_parse_mcp_tool_call_completed(_store) -> None:
    line = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "server": "orcho-demo-mcp",
            "tool": "orcho_plan_validate",
            "arguments": {"task": "x"},
            "status": "completed",
        },
    })
    parse_codex_line(line, agent_label="REVIEW")
    event = _last_event(_store)
    assert event.kind == "agent.mcp_tool_call"
    assert event.payload["server"] == "orcho-demo-mcp"
    assert event.payload["tool_name"] == "orcho_plan_validate"
    assert event.payload["tool_category"] == "mcp"
    assert event.payload["arguments"] == {"task": "x"}
    assert event.payload["status"] == "completed"


def test_parse_mcp_tool_call_failed_carries_error(_store) -> None:
    line = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "server": "orcho-demo-mcp",
            "tool": "orcho_plan_validate",
            "arguments": {},
            "status": "failed",
            "error": {"message": "user cancelled MCP tool call"},
        },
    })
    parse_codex_line(line)
    event = _last_event(_store)
    assert event.kind == "agent.mcp_tool_call"
    assert event.payload["status"] == "failed"
    assert event.payload["error"] == {"message": "user cancelled MCP tool call"}


def test_parse_mcp_tool_call_missing_server_or_tool_silently_skipped(_store) -> None:
    parse_codex_line(json.dumps({
        "type": "item.completed",
        "item": {"type": "mcp_tool_call", "server": "", "tool": "t"},
    }))
    parse_codex_line(json.dumps({
        "type": "item.completed",
        "item": {"type": "mcp_tool_call", "server": "s", "tool": ""},
    }))
    assert _events_kinds(_store) == []


def test_parse_mcp_item_started_does_not_emit(_store) -> None:
    parse_codex_line(json.dumps({
        "type": "item.started",
        "item": {
            "type": "mcp_tool_call",
            "server": "orcho-demo-mcp",
            "tool": "orcho_plan_validate",
        },
    }))
    assert _events_kinds(_store) == []


def test_parse_agent_message_emits_text(_store) -> None:
    line = json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "  Looks good.  \n"},
    })
    parse_codex_line(line)
    event = _last_event(_store)
    assert event.kind == "agent.text"
    assert event.payload["text"] == "Looks good."


def test_parse_json_contract_emits_ready_event_not_text(_store) -> None:
    line = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "agent_message",
            "text": '{"verdict":"APPROVED","findings":[]}',
        },
    })
    parse_codex_line(line, agent_label="REVIEW")
    events = evstore.read_all(_store)
    assert [event.kind for event in events] == ["agent.contract_ready"]
    assert events[0].payload["agent"] == "REVIEW"
    assert events[0].payload["format"] == "json"


def test_parse_recovery_shape_emits_prose_then_contract_ready(_store) -> None:
    text = (
        "## Summary\n\nDid the work.\n\n"
        '{"type":"subtask_attestation","subtask_id":"T1"}'
    )
    parse_codex_line(json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": text},
    }), agent_label="IMPLEMENT")
    events = evstore.read_all(_store)
    assert [e.kind for e in events] == ["agent.text", "agent.contract_ready"]
    assert "Did the work" in events[0].payload["text"]
    assert "subtask_attestation" not in events[0].payload["text"]
    assert events[1].payload["format"] == "json"


def test_stdout_recovery_shape_keeps_prose_drops_contract() -> None:
    text = (
        "## Summary\n\nDid the work.\n\n"
        '{"type":"subtask_attestation","subtask_id":"T1"}'
    )
    out = format_codex_line_for_stdout(json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": text},
    }))
    assert out is not None
    assert "Did the work" in out
    assert "Contracted answer prepared" in out
    assert "subtask_attestation" not in out


def test_parse_pretty_json_contract_emits_ready_once(_store) -> None:
    for chunk in [
        "{\n",
        '  "verdict": "APPROVED",\n',
        '  "findings": []\n',
        "}\n",
    ]:
        parse_codex_line(json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": chunk},
        }), agent_label="REVIEW")

    assert _events_kinds(_store) == ["agent.contract_ready"]


def test_parse_agent_message_skill_use_emits_skill_event(_store) -> None:
    line = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "agent_message",
            "text": "Using `frontend-qa` because this changes the UI.",
        },
    })
    parse_codex_line(line, agent_label="QA", skill_names={"frontend-qa"})
    events = evstore.read_all(_store)
    assert [event.kind for event in events] == ["agent.text", "agent.skill_use"]
    assert events[-1].payload["skill_name"] == "frontend-qa"
    assert events[-1].payload["agent"] == "QA"


def test_parse_turn_completed_emits_summary(_store) -> None:
    usage = {
        "input_tokens": 10,
        "cached_input_tokens": 3,
        "output_tokens": 2,
        "reasoning_output_tokens": 1,
    }
    parse_codex_line(json.dumps({"type": "turn.completed", "usage": usage}))
    event = _last_event(_store)
    assert event.kind == "agent.summary"
    assert event.payload["usage"] == usage
    assert event.payload["input_tokens"] == 10
    assert event.payload["cached_input_tokens"] == 3
    assert event.payload["output_tokens"] == 2
    assert event.payload["reasoning_output_tokens"] == 1


def test_parse_lifecycle_and_unknown_events_are_silent(_store) -> None:
    parse_codex_line(json.dumps({"type": "thread.started", "thread_id": "abc"}))
    parse_codex_line(json.dumps({"type": "turn.started"}))
    parse_codex_line(json.dumps({"type": "something.new"}))
    assert _events_kinds(_store) == []


def test_item_started_command_execution_does_not_emit_tool_use(_store) -> None:
    parse_codex_line(json.dumps({
        "type": "item.started",
        "item": {"id": "item_0", "type": "command_execution", "command": "ls"},
    }))
    assert _events_kinds(_store) == []


def test_item_completed_command_execution_emits_single_tool_use(_store) -> None:
    started = json.dumps({
        "type": "item.started",
        "item": {"id": "item_0", "type": "command_execution", "command": "ls"},
    })
    completed = json.dumps({
        "type": "item.completed",
        "item": {"id": "item_0", "type": "command_execution", "command": "ls"},
    })
    parse_codex_line(started)
    parse_codex_line(completed)
    kinds = _events_kinds(_store)
    assert kinds == ["agent.tool_use"]


def test_item_started_agent_message_does_not_emit_text(_store) -> None:
    parse_codex_line(json.dumps({
        "type": "item.started",
        "item": {"id": "item_1", "type": "agent_message", "text": "Hi."},
    }))
    assert _events_kinds(_store) == []


def test_item_completed_agent_message_emits_single_text(_store) -> None:
    started = json.dumps({
        "type": "item.started",
        "item": {"id": "item_1", "type": "agent_message", "text": "Hi."},
    })
    completed = json.dumps({
        "type": "item.completed",
        "item": {"id": "item_1", "type": "agent_message", "text": "Hi."},
    })
    parse_codex_line(started)
    parse_codex_line(completed)
    assert _events_kinds(_store) == ["agent.text"]


def test_formatter_hides_lifecycle_and_summary_lines() -> None:
    assert format_codex_line_for_stdout(json.dumps({"type": "thread.started"})) is None
    assert format_codex_line_for_stdout(json.dumps({"type": "turn.started"})) is None
    assert format_codex_line_for_stdout(json.dumps({"type": "turn.completed"})) is None


def test_formatter_renders_command_execution_on_completed() -> None:
    line = json.dumps({
        "type": "item.completed",
        "item": {"type": "command_execution", "command": "python -m pytest"},
    })
    assert format_codex_line_for_stdout(line) == "  ⚡ Bash: python -m pytest\n"


def test_formatter_silences_item_started() -> None:
    cmd_started = json.dumps({
        "type": "item.started",
        "item": {"type": "command_execution", "command": "python -m pytest"},
    })
    msg_started = json.dumps({
        "type": "item.started",
        "item": {"type": "agent_message", "text": "Hi."},
    })
    assert format_codex_line_for_stdout(cmd_started) is None
    assert format_codex_line_for_stdout(msg_started) is None


def test_formatter_no_duplicate_transcript_for_started_then_completed() -> None:
    started = json.dumps({
        "type": "item.started",
        "item": {"id": "item_0", "type": "command_execution", "command": "ls"},
    })
    completed = json.dumps({
        "type": "item.completed",
        "item": {"id": "item_0", "type": "command_execution", "command": "ls"},
    })
    rendered = [
        format_codex_line_for_stdout(started),
        format_codex_line_for_stdout(completed),
    ]
    assert rendered == [None, "  ⚡ Bash: ls\n"]


def test_formatter_renders_agent_message() -> None:
    line = json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "Hi."},
    })
    assert format_codex_line_for_stdout(line) == "  💬 Assistant: Hi.\n"


def test_formatter_renders_registered_skill_use() -> None:
    line = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "agent_message",
            "text": "Using `frontend-qa` because this changes the UI.",
        },
    })
    assert format_codex_line_for_stdout(
        line,
        skill_names={"frontend-qa"},
    ) == (
        "  🧠 Skill: frontend-qa — Using `frontend-qa` because this changes "
        "the UI.\n"
    )


def test_formatter_suppresses_json_agent_message_under_defer() -> None:
    from core.io.stdout_render import defer_assistant_json
    from core.observability.logging import set_verbose

    line = json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": '{"verdict":"APPROVED"}'},
    })
    # In ``--output debug`` JSON-shaped agent messages are visible…
    set_verbose(True)
    try:
        assert format_codex_line_for_stdout(line) is not None
        # …but ``defer_assistant_json()`` overrides that to force a drop.
        with defer_assistant_json():
            out = format_codex_line_for_stdout(line)
            assert out is not None
            assert "Contracted answer prepared" in out
            assert "verdict" not in out
    finally:
        set_verbose(False)
    # Outside debug mode JSON-shaped agent messages get the same marker.
    out = format_codex_line_for_stdout(line)
    assert out is not None
    assert "Contracted answer prepared" in out
    assert "verdict" not in out


def test_formatter_marks_json_agent_message_when_not_verbose() -> None:
    line = json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": '[{"finding":"x"}]'},
    })
    out = format_codex_line_for_stdout(line)
    assert out is not None
    assert "Contracted answer prepared" in out
    assert "finding" not in out


def test_formatter_marks_pretty_json_chunks_once_when_not_verbose() -> None:
    chunks = [
        "{\n",
        '  "verdict": "APPROVED",\n',
        '  "findings": []\n',
        "}\n",
    ]
    rendered = [
        format_codex_line_for_stdout(json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": chunk},
        }))
        for chunk in chunks
    ]
    assert rendered[0] is not None
    assert "Contracted answer prepared" in rendered[0]
    assert rendered[1:] == [None, None, None]


def test_formatter_keeps_prose_agent_message_when_not_verbose() -> None:
    line = json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "Plain prose output."},
    })
    assert (
        format_codex_line_for_stdout(line)
        == "  💬 Assistant: Plain prose output.\n"
    )


def test_formatter_passes_through_non_json_and_unknown_json() -> None:
    assert format_codex_line_for_stdout("plain error\n") == "plain error\n"
    unknown = json.dumps({"type": "unexpected", "body": "keep"}) + "\n"
    assert format_codex_line_for_stdout(unknown) == unknown


def test_formatter_renders_mcp_tool_call_completed() -> None:
    line = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "server": "orcho-demo-mcp",
            "tool": "orcho_plan_validate",
            "status": "completed",
        },
    })
    assert format_codex_line_for_stdout(line) == (
        "  🔌 MCP: orcho-demo-mcp.orcho_plan_validate\n"
    )


def test_formatter_renders_mcp_tool_call_failed_with_message() -> None:
    line = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "server": "orcho-demo-mcp",
            "tool": "orcho_plan_validate",
            "status": "failed",
            "error": {"message": "user cancelled MCP tool call"},
        },
    })
    assert format_codex_line_for_stdout(line) == (
        "  🔌 MCP: orcho-demo-mcp.orcho_plan_validate "
        "failed: user cancelled MCP tool call\n"
    )


def test_formatter_does_not_passthrough_known_mcp_started() -> None:
    line = json.dumps({
        "type": "item.started",
        "item": {
            "type": "mcp_tool_call",
            "server": "orcho-demo-mcp",
            "tool": "orcho_plan_validate",
        },
    })
    assert format_codex_line_for_stdout(line) is None


def test_formatter_passes_through_malformed_mcp_for_diagnostics() -> None:
    """Half-formed MCP items must remain visible in transcript/log so a
    smoke run can catch a Codex CLI shape that diverges from the spec —
    even though the parser silently drops them to keep the event store
    free of half-formed agent.mcp_tool_call entries.
    """
    missing_server = json.dumps({
        "type": "item.completed",
        "item": {"type": "mcp_tool_call", "server": "", "tool": "t"},
    }) + "\n"
    assert format_codex_line_for_stdout(missing_server) == missing_server

    missing_tool = json.dumps({
        "type": "item.completed",
        "item": {"type": "mcp_tool_call", "server": "s", "tool": ""},
    }) + "\n"
    assert format_codex_line_for_stdout(missing_tool) == missing_tool


def test_formatter_does_not_passthrough_known_mcp_completed_raw() -> None:
    """Known mcp_tool_call payloads must never leak raw JSON to transcript/log."""
    line = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "server": "s",
            "tool": "t",
            "status": "completed",
        },
    })
    formatted = format_codex_line_for_stdout(line)
    assert formatted is not None
    assert '"mcp_tool_call"' not in formatted
    assert '"item.completed"' not in formatted
