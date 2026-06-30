"""Gemini stream-json parser contracts.

Tests pin the event-mapping rules in
:mod:`agents.stream_parsers.gemini_jsonl`:

* ``tool_use`` envelopes emit a normalized ``agent.tool_use`` event.
* Assistant messages emit ``agent.text``; user echoes are silenced.
* The terminal ``result`` event emits ``agent.summary`` with the same
  field shape Claude/Codex consumers already understand.
* Malformed JSON is silently dropped — the parser never raises.
"""

from __future__ import annotations

import json

import pytest

from agents.stream_parsers.gemini_jsonl import (
    format_gemini_line_for_stdout,
    parse_gemini_line,
)


def _capture_events(monkeypatch) -> list[tuple[str, dict]]:
    seen: list[tuple[str, dict]] = []

    def fake_emit(event_type: str, **payload):
        seen.append((event_type, payload))

    from core.observability import events as _events
    monkeypatch.setattr(_events, "emit", fake_emit)
    return seen


class TestParseGeminiLine:
    def test_init_event_is_silent(self, monkeypatch) -> None:
        seen = _capture_events(monkeypatch)
        parse_gemini_line(json.dumps({
            "type": "init", "session_id": "sid-A", "model": "gemini-2.5-flash",
        }))
        assert seen == []

    def test_user_message_is_silent(self, monkeypatch) -> None:
        seen = _capture_events(monkeypatch)
        parse_gemini_line(json.dumps({
            "type": "message", "role": "user", "content": "echoed prompt",
        }))
        assert seen == []

    def test_assistant_message_emits_agent_text(self, monkeypatch) -> None:
        seen = _capture_events(monkeypatch)
        parse_gemini_line(
            json.dumps({
                "type": "message", "role": "assistant",
                "content": "hello world", "delta": True,
            }),
            agent_label="plan",
        )
        kinds = [e[0] for e in seen]
        assert "agent.text" in kinds
        payload = next(p for k, p in seen if k == "agent.text")
        assert payload["text"] == "hello world"
        assert payload["agent"] == "plan"

    def test_tool_use_emits_agent_tool_use(self, monkeypatch) -> None:
        seen = _capture_events(monkeypatch)
        parse_gemini_line(json.dumps({
            "type": "tool_use",
            "tool_name": "read_file",
            "tool_id": "t1",
            "parameters": {"file_path": "src/app.py"},
        }))
        kinds = [e[0] for e in seen]
        assert "agent.tool_use" in kinds

    def test_result_emits_agent_summary_with_split_token_buckets(
        self, monkeypatch,
    ) -> None:
        seen = _capture_events(monkeypatch)
        parse_gemini_line(json.dumps({
            "type": "result",
            "status": "success",
            "stats": {
                "total_tokens": 1100,
                "input_tokens": 1000,
                "output_tokens": 100,
                "cached": 200,
                "duration_ms": 1234,
                "tool_calls": 0,
            },
        }))
        kinds = [e[0] for e in seen]
        assert "agent.summary" in kinds
        payload = next(p for k, p in seen if k == "agent.summary")
        assert payload["input_tokens"] == 1000
        assert payload["fresh_input_tokens"] == 800
        assert payload["cache_read_input_tokens"] == 200
        assert payload["cache_creation_input_tokens"] == 0
        assert payload["output_tokens"] == 100
        assert payload["cost_usd"] is None

    def test_result_clamps_fresh_when_cached_exceeds_input(
        self, monkeypatch,
    ) -> None:
        seen = _capture_events(monkeypatch)
        parse_gemini_line(json.dumps({
            "type": "result",
            "status": "success",
            "stats": {"input_tokens": 100, "output_tokens": 5, "cached": 250},
        }))
        payload = next(p for k, p in seen if k == "agent.summary")
        assert payload["input_tokens"] == 100
        assert payload["cache_read_input_tokens"] == 250
        assert payload["fresh_input_tokens"] == 0

    @pytest.mark.parametrize("bad", [
        "",
        "  ",
        "not json",
        "{not valid",
        "{}",
        json.dumps({"type": "unknown_event"}),
    ])
    def test_malformed_or_unknown_lines_silent(
        self, monkeypatch, bad: str,
    ) -> None:
        seen = _capture_events(monkeypatch)
        parse_gemini_line(bad)
        assert seen == []


class TestFormatGeminiLineForStdout:
    def test_tool_use_renders_one_line(self) -> None:
        out = format_gemini_line_for_stdout(json.dumps({
            "type": "tool_use",
            "tool_name": "read_file",
            "tool_id": "t1",
            "parameters": {"file_path": "src/app.py"},
        }))
        assert out is not None
        assert "read_file" in out

    def test_assistant_message_renders_when_not_suppressed(self) -> None:
        out = format_gemini_line_for_stdout(json.dumps({
            "type": "message", "role": "assistant",
            "content": "human-readable reply",
        }))
        # format_thinking_text wraps the content; the substring is enough
        # to confirm the assistant text reached stdout formatting.
        assert out is not None
        assert "human-readable reply" in out

    def test_init_result_user_lines_suppressed(self) -> None:
        for event in (
            {"type": "init", "session_id": "x"},
            {"type": "message", "role": "user", "content": "echo"},
            {"type": "result", "status": "success", "stats": {}},
            {"type": "tool_result", "tool_id": "x", "status": "success"},
        ):
            assert format_gemini_line_for_stdout(json.dumps(event)) is None

    def test_non_json_passes_through(self) -> None:
        assert (
            format_gemini_line_for_stdout("Warning: terminal needs 256 colors")
            == "Warning: terminal needs 256 colors"
        )

    def test_empty_line_dropped(self) -> None:
        assert format_gemini_line_for_stdout("   \n") is None
