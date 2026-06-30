"""ClaudeAgent runtime adapter contracts.

After Phase 7 the runtime exposes a single :meth:`ClaudeAgent.invoke`.
These tests defend the wrapper behavior around ``_stream_run``:

* Every call uses ``--output-format stream-json --verbose`` so a
  ``session_id`` lands in stdout and the runtime is bridge-capable
  regardless of ``mutates_artifacts``.
* ``mutates_artifacts=True`` adds both ``--permission-mode acceptEdits``
  and ``--dangerously-skip-permissions``; the default does not.
* ``continue_session=True`` with a previously captured ``session_id``
  threads ``--resume <id>`` so the bridge survives across invocations.
* Module-level helpers (``_extract_assistant_text``,
  ``_extract_last_result``, ``_extract_session_id``) tolerate non-JSON
  noise and unexpected shapes — they run on every invocation, so a
  parser miss must not crash the agent.

No real subprocess: ``agents._stream_run`` is monkeypatched per-test
via the local ``mock_stream_run`` fixture.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import agents as agents_module
from agents.runtimes.auth import raise_authentication_error
from agents.runtimes.claude import (
    ClaudeAgent,
    _extract_assistant_text,
    _extract_last_result,
    _extract_session_id,
)
from core.io.retry import AgentAuthenticationError

# ── helpers / fixtures ─────────────────────────────────────────────────────


def _stream_result(
    stdout: str = "done",
    returncode: int = 0,
    stderr: str = "",
    duration: float = 0.1,
):
    """``_stream_run`` returns a (stdout, returncode, stderr, duration) tuple."""
    return (stdout, returncode, stderr, duration)


def _assistant_event(
    text: str,
    *,
    session_id: str | None = None,
    usage: dict | None = None,
) -> str:
    """One JSONL line shaped like Claude Code stream-json output."""
    event: dict = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }
    if usage is not None:
        event["message"]["usage"] = usage
    if session_id is not None:
        event["session_id"] = session_id
    return json.dumps(event)


def _result_event(
    *,
    cost: float | None = 0.01,
    in_tok: int | None = 100,
    out_tok: int | None = 200,
    cache_create: int | None = None,
    cache_read: int | None = None,
    model_usage: dict | None = None,
) -> str:
    """One JSONL line shaped like Claude Code's terminal result event.

    ``model_usage`` mirrors the CLI's per-model rollup (probed
    2026-05-18): keys are full model ids like
    ``"claude-opus-4-7[1m]"``, values carry ``contextWindow`` +
    ``maxOutputTokens`` + per-call token counters. When provided,
    activates M14.4.1 runtime-reported context fullness capture.
    """
    usage: dict = {"input_tokens": in_tok, "output_tokens": out_tok}
    if cache_create is not None:
        usage["cache_creation_input_tokens"] = cache_create
    if cache_read is not None:
        usage["cache_read_input_tokens"] = cache_read
    event: dict = {
        "type": "result",
        "total_cost_usd": cost,
        "usage": usage,
    }
    if model_usage is not None:
        event["modelUsage"] = model_usage
    return json.dumps(event)


@pytest.fixture
def claude(mock_claude_bin: None) -> ClaudeAgent:
    return ClaudeAgent(model="claude-sonnet-test")


def test_missing_claude_binary_error_names_runtime(monkeypatch) -> None:
    from core.infra import config

    def missing() -> str:
        raise RuntimeError("Cannot find 'claude' binary")

    monkeypatch.setattr(config, "get_claude_bin", missing)
    agent = ClaudeAgent(model="claude-sonnet-test")

    with pytest.raises(RuntimeError, match="claude runtime cannot start"):
        _ = agent.bin


@pytest.fixture
def mock_stream_run(monkeypatch) -> MagicMock:
    """Patch ``agents._stream_run`` so no real subprocess fires."""
    mock = MagicMock(return_value=_stream_result("done"))
    monkeypatch.setattr(agents_module, "_stream_run", mock)
    return mock


# ── _extract_assistant_text ────────────────────────────────────────────────


class TestExtractAssistantText:
    def test_empty_input_returns_empty_string(self) -> None:
        assert _extract_assistant_text("") == ""

    def test_returns_last_assistant_event_text(self) -> None:
        out = "\n".join([
            _assistant_event("first chunk"),
            _assistant_event("second chunk"),
        ])
        assert _extract_assistant_text(out) == "second chunk"

    def test_joins_text_blocks_from_final_assistant_event(self) -> None:
        out = json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "part 1"},
                    {"type": "text", "text": "part 2"},
                ],
            },
        })
        assert _extract_assistant_text(out) == "part 1\npart 2"

    def test_skips_lines_that_are_not_json(self) -> None:
        out = "banner line not json\n" + _assistant_event("ok")
        assert _extract_assistant_text(out) == "ok"

    def test_skips_malformed_json_lines(self) -> None:
        out = "{not valid json}\n" + _assistant_event("after garbage")
        assert _extract_assistant_text(out) == "after garbage"

    def test_skips_non_assistant_events(self) -> None:
        system_event = json.dumps(
            {"type": "system", "subtype": "init", "message": {}}
        )
        out = system_event + "\n" + _assistant_event("only assistant counts")
        assert _extract_assistant_text(out) == "only assistant counts"

    def test_skips_assistant_event_without_dict_message(self) -> None:
        odd = json.dumps({"type": "assistant", "message": "not a dict"})
        out = odd + "\n" + _assistant_event("good one")
        assert _extract_assistant_text(out) == "good one"


# ── _extract_last_result ───────────────────────────────────────────────────


class TestExtractLastResult:
    def test_returns_final_result_line(self) -> None:
        out = "\n".join([
            _assistant_event("hi"),
            _result_event(cost=0.05, in_tok=10, out_tok=20),
        ])
        result = _extract_last_result(out)
        assert result is not None
        assert result["total_cost_usd"] == 0.05

    def test_empty_input_returns_none(self) -> None:
        assert _extract_last_result("") is None

    def test_no_result_line_returns_none(self) -> None:
        assert _extract_last_result(_assistant_event("only assistant")) is None

    def test_walks_in_reverse_and_skips_non_json_tail(self) -> None:
        out = _result_event(cost=0.1) + "\n{not valid json}\nbanner\n"
        result = _extract_last_result(out)
        assert result is not None
        assert result["total_cost_usd"] == 0.1


# ── _extract_session_id ────────────────────────────────────────────────────


class TestExtractSessionId:
    def test_returns_session_id_from_stream_json_event(self) -> None:
        out = _assistant_event("hi", session_id="sess-abc")
        assert _extract_session_id(out) == "sess-abc"

    def test_empty_input_returns_none(self) -> None:
        assert _extract_session_id("") is None

    def test_no_session_id_anywhere_returns_none(self) -> None:
        out = _assistant_event("no id here")
        assert _extract_session_id(out) is None

    def test_skips_malformed_json_then_falls_back_to_regex(self) -> None:
        out = '\x1b[36m{"session_id": "sess-from-regex", "noise": }\x1b[0m'
        assert _extract_session_id(out) == "sess-from-regex"

    def test_skips_non_dict_json_node(self) -> None:
        out = "[1, 2, 3]"
        assert _extract_session_id(out) is None

    def test_skips_brace_prefixed_line_with_malformed_json(self) -> None:
        out = '{this is not json}\nsome banner {"session_id": "found-it"}'
        assert _extract_session_id(out) == "found-it"


# ── invoke(): CLI shape ────────────────────────────────────────────────────


class TestInvokeCliShape:
    """Every invoke goes through stream-json so the bridge is capable."""

    def test_stream_json_and_verbose_always_present(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        claude.invoke("any prompt", "/project")
        cmd = mock_stream_run.call_args[0][0]
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--verbose" in cmd

    def test_read_only_drops_write_flags(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        claude.invoke("read-only call", "/project")
        cmd = mock_stream_run.call_args[0][0]
        assert "--dangerously-skip-permissions" not in cmd
        assert "acceptEdits" not in cmd

    def test_mutates_artifacts_emits_both_write_flags(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        claude.invoke("write call", "/project", mutates_artifacts=True)
        cmd = mock_stream_run.call_args[0][0]
        assert "--permission-mode" in cmd
        assert "acceptEdits" in cmd
        assert "--dangerously-skip-permissions" in cmd

    def test_model_flag_threaded(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        claude.invoke("hi", "/project")
        cmd = mock_stream_run.call_args[0][0]
        assert "--model" in cmd
        assert "claude-sonnet-test" in cmd

    def test_effort_flag_threaded_when_set(
        self, mock_claude_bin: None, mock_stream_run: MagicMock,
    ) -> None:
        agent = ClaudeAgent(model="m", effort="high")
        agent.invoke("hi", "/project")
        cmd = mock_stream_run.call_args[0][0]
        assert "--effort" in cmd
        assert "high" in cmd

    def test_no_effort_flag_when_unset(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        claude.invoke("hi", "/project")
        cmd = mock_stream_run.call_args[0][0]
        assert "--effort" not in cmd

    def test_prompt_appended_as_last_positional_arg(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        claude.invoke("the prompt text", "/project")
        cmd = mock_stream_run.call_args[0][0]
        assert cmd[-1] == "the prompt text"

    def test_cwd_passed_through(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        claude.invoke("hi", "/my/project")
        assert mock_stream_run.call_args[1]["cwd"] == "/my/project"

    def test_wires_stream_and_log_filters_to_claude_formatter(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        """Both live stdout and ``output.log`` route through the
        provider-neutral Claude formatter, so transcript and log show the
        same human-readable tool / MCP lines."""
        from agents.stream_parsers import format_claude_line_for_stdout

        claude.invoke("hi", "/project")
        kwargs = mock_stream_run.call_args[1]
        assert kwargs["stdout_filter"] is format_claude_line_for_stdout
        assert kwargs["log_filter"] is format_claude_line_for_stdout
        assert kwargs["return_filter"].__name__ == "elide_tool_result_line_for_model"


# ── invoke(): assistant text extraction ────────────────────────────────────


class TestInvokeOutput:
    def test_returns_extracted_assistant_text(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        mock_stream_run.return_value = _stream_result(_assistant_event("hello"))
        assert claude.invoke("hi", "/project") == "hello"

    def test_returns_final_assistant_text_not_progress_chatter(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        mock_stream_run.return_value = _stream_result("\n".join([
            _assistant_event("Сейчас посмотрю и внесу изменения."),
            _assistant_event("Готово: изменены db.py и handlers.py."),
        ]))
        assert claude.invoke("hi", "/project") == (
            "Готово: изменены db.py и handlers.py."
        )

    def test_falls_back_to_raw_stdout_when_no_assistant_text(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        mock_stream_run.return_value = _stream_result("raw blob, no events")
        assert claude.invoke("hi", "/project") == "raw blob, no events"

    def test_stderr_surfaced_on_nonzero_returncode(
        self,
        claude: ClaudeAgent,
        mock_stream_run: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_stream_run.return_value = _stream_result(
            stdout="", returncode=1, stderr="claude exploded",
        )
        # A non-zero exit is an API-client failure: the runtime must raise so
        # the run halts instead of returning the error text as a response.
        # stderr is still surfaced (printed) before the raise.
        with pytest.raises(RuntimeError):
            claude.invoke("hi", "/project")
        captured = capsys.readouterr()
        assert "claude exploded" in captured.out

    def test_auth_failure_raises_user_facing_error(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        mock_stream_run.return_value = _stream_result(
            stdout="Failed to authenticate. API Error: 401 Invalid authentication credentials",
            returncode=1,
            stderr="",
        )
        with pytest.raises(AgentAuthenticationError) as exc_info:
            claude.invoke("hi", "/project")

        msg = str(exc_info.value)
        assert "runtime='claude'" in msg
        assert "model='claude-sonnet-test'" in msg
        assert "Runtime credentials were rejected" in msg
        assert "claude auth logout && claude auth login" in msg
        assert "claude auth login" in msg
        assert "claude auth status" in msg
        assert "claude --print --model claude-sonnet-test" in msg
        assert "Original CLI error" not in msg
        assert "Failed to authenticate" not in msg

    def test_auth_error_drops_protocol_json_noise(self) -> None:
        from core.observability.logging import set_verbose

        raw = (
            '{"type":"system","subtype":"init","tools":["large","list"]}\n'
            '{"type":"assistant","message":{"content":[{"type":"text",'
            '"text":"Failed to authenticate. API Error: 401 Invalid authentication credentials"}]},'
            '"error":"authentication_failed","request_id":"req_123"}\n'
            '{"type":"result","is_error":true,"api_error_status":401}\n'
        )
        set_verbose(True)
        try:
            with pytest.raises(AgentAuthenticationError) as exc_info:
                raise_authentication_error(
                    runtime="claude",
                    model="m",
                    cli="/bin/claude",
                    exit_code=1,
                    stdout=raw,
                )
        finally:
            set_verbose(False)

        msg = str(exc_info.value)
        assert "Original CLI error (--output debug):" in msg
        assert "Failed to authenticate" in msg
        assert '"type":"system"' not in msg
        assert '"type":"assistant"' not in msg
        assert "request_id" not in msg
        assert '"tools"' not in msg


# ── invoke(): session bridge ────────────────────────────────────────────────


class TestInvokeSessionBridge:
    """Universal session capture keeps the bridge alive across calls."""

    def test_first_call_captures_session_id_from_stream(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        mock_stream_run.return_value = _stream_result(
            _assistant_event("ok", session_id="sess-1")
        )
        claude.invoke("task", "/project")
        assert claude.session_id == "sess-1"

    def test_read_only_call_captures_session_id_too(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        """stream-json mandate: every call is bridge-capable, not just writes."""
        mock_stream_run.return_value = _stream_result(
            _assistant_event("read-only response", session_id="sess-read")
        )
        claude.invoke("read-only", "/project", mutates_artifacts=False)
        assert claude.session_id == "sess-read"

    def test_continue_session_passes_resume_flag(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        claude.session_id = "captured-sess"
        claude.invoke("task", "/project", continue_session=True)
        cmd = mock_stream_run.call_args[0][0]
        assert "--resume" in cmd
        assert "captured-sess" in cmd

    def test_continue_session_without_id_runs_stateless(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        mock_stream_run.return_value = _stream_result(
            _assistant_event("ok", session_id="sess-fresh")
        )
        claude.invoke("task", "/project", continue_session=True)
        cmd = mock_stream_run.call_args[0][0]
        assert "--resume" not in cmd
        assert claude.session_id == "sess-fresh"

    def test_preserves_existing_session_id_on_parse_miss(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        """A transient parsing failure must not drop the bridge."""
        claude.session_id = "old-sess"
        mock_stream_run.return_value = _stream_result(
            stdout="garbage that has no session_id",
        )
        claude.invoke("task", "/project")
        assert claude.session_id == "old-sess"

    def test_cross_mutation_resume_is_mechanical_not_raised(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        """Per ADR 0023: the runtime is mechanical — it does not raise on
        cross-mutation resume. Policy (when to reset across modes) lives
        at the orchestrator.
        """
        # Seed a session via a read-only call.
        mock_stream_run.return_value = _stream_result(
            _assistant_event("ok", session_id="sess-mixed")
        )
        claude.invoke("read", "/project", mutates_artifacts=False)
        assert claude.session_id == "sess-mixed"
        # Resume in write mode — must succeed without raising.
        claude.invoke(
            "write", "/project",
            mutates_artifacts=True, continue_session=True,
        )
        cmd = mock_stream_run.call_args[0][0]
        assert "--resume" in cmd
        assert "sess-mixed" in cmd


class TestResetSession:
    def test_clears_captured_session_id(self, claude: ClaudeAgent) -> None:
        claude.session_id = "to-be-cleared"
        claude.reset_session()
        assert claude.session_id is None


class TestCaptureUsage:
    """``_capture_usage`` reflects the *full* input scope, including cache
    buckets. Without this, a heavily-cached resume call shows ``in=9 tokens``
    when the actual input volume is thousands of tokens (the visible bug
    reported on the cross-plan demo).
    """

    def test_sums_all_three_input_buckets(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        stdout = "\n".join((
            json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}},
                ]},
            }),
            _result_event(
                cost=0.10, in_tok=9, out_tok=1492,
                cache_create=120, cache_read=15000,
            ),
        ))
        mock_stream_run.return_value = _stream_result(stdout)
        claude.invoke("hi", "/project")
        assert claude.last_tokens_in == 9 + 120 + 15000
        assert claude.last_tokens_in_fresh == 9
        assert claude.last_tokens_in_cache_create == 120
        assert claude.last_tokens_in_cache_read == 15000
        assert claude.last_tokens_out == 1492
        assert claude.last_cost_usd == 0.10
        assert claude.last_tool_use_count == 2

    def test_no_cache_fields_falls_back_to_fresh_only(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        mock_stream_run.return_value = _stream_result(_result_event(
            cost=0.01, in_tok=100, out_tok=200,
        ))
        claude.invoke("hi", "/project")
        assert claude.last_tokens_in == 100
        assert claude.last_tokens_in_fresh == 100
        assert claude.last_tokens_in_cache_create == 0
        assert claude.last_tokens_in_cache_read == 0

    def test_no_result_event_blanks_all_fields(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        mock_stream_run.return_value = _stream_result(
            _assistant_event("only assistant, no result line"),
        )
        claude.invoke("hi", "/project")
        assert claude.last_cost_usd is None
        assert claude.last_tokens_in is None
        assert claude.last_tokens_out is None
        assert claude.last_tokens_in_fresh is None
        assert claude.last_tokens_in_cache_create is None
        assert claude.last_tokens_in_cache_read is None
        # M14.4.1: runtime-reported context attrs also blanked when
        # the result line is missing — the resolver then falls back
        # to the orcho_estimated branch.
        assert claude.last_context_window_tokens is None
        assert claude.last_context_used_tokens is None
        assert claude.last_context_peak_tokens is None


class TestPickPrimaryModelUsage:
    """M14.4.1 — Claude Code's ``result.modelUsage`` is a per-model
    rollup keyed by full model id (variant suffix included). The
    helper picks the entry corresponding to the call's primary
    reasoning model so the captured ``contextWindow`` matches the
    actual prompt the model saw."""

    def test_prefix_match_against_configured_model(self) -> None:
        from agents.runtimes.claude import _pick_primary_model_usage
        mu = {
            "claude-haiku-4-5-20251001": {
                "inputTokens":    450, "contextWindow": 200_000,
            },
            "claude-opus-4-7[1m]": {
                "inputTokens":    6, "cacheCreationInputTokens": 11_677,
                "contextWindow":  1_000_000,
            },
        }
        chosen = _pick_primary_model_usage(mu, "claude-opus-4-7")
        assert chosen is not None
        assert chosen["contextWindow"] == 1_000_000

    def test_variant_suffix_stripped_on_both_sides(self) -> None:
        # Configured model already has a [1m] suffix; the matcher
        # must still pair it with the same-prefix CLI key.
        from agents.runtimes.claude import _pick_primary_model_usage
        mu = {"claude-opus-4-7[1m]": {"contextWindow": 1_000_000}}
        chosen = _pick_primary_model_usage(mu, "claude-opus-4-7[1m]")
        assert chosen is not None
        assert chosen["contextWindow"] == 1_000_000

    def test_fallback_picks_largest_input_contributor(self) -> None:
        # No prefix match — fall back to the entry that did the
        # heaviest input work this call (proxy for the primary
        # reasoning model).
        from agents.runtimes.claude import _pick_primary_model_usage
        mu = {
            "claude-haiku-4-5-20251001": {
                "inputTokens":    450, "contextWindow": 200_000,
            },
            "claude-opus-4-7[1m]": {
                "inputTokens":    6,    "cacheCreationInputTokens": 11_677,
                "contextWindow":  1_000_000,
            },
        }
        # Configured model that matches neither — fallback rules apply.
        chosen = _pick_primary_model_usage(mu, "gpt-something")
        assert chosen is not None
        assert chosen["contextWindow"] == 1_000_000

    def test_empty_dict_returns_none(self) -> None:
        from agents.runtimes.claude import _pick_primary_model_usage
        assert _pick_primary_model_usage({}, "claude-opus-4-7") is None

    def test_non_dict_input_returns_none(self) -> None:
        from agents.runtimes.claude import _pick_primary_model_usage
        assert _pick_primary_model_usage(None, "claude") is None  # type: ignore[arg-type]
        assert _pick_primary_model_usage([], "claude") is None  # type: ignore[arg-type]


class TestRuntimeReportedContextCapture:
    """M14.4.1 — when the CLI's ``result.modelUsage`` exposes a
    ``contextWindow`` for the call's primary model, the adapter
    stamps ``last_context_window_tokens`` + ``last_context_used_tokens``.
    The M14.4
    :func:`pipeline.observability.context_pressure.resolve_context_pressure`
    resolver reads these two attributes and flips its branch from
    ``orcho_estimated`` to ``runtime_reported`` without any
    contract change."""

    def _opus_model_usage(self) -> dict:
        return {
            "claude-haiku-4-5-20251001": {
                "inputTokens":    450, "outputTokens":  12,
                "contextWindow":  200_000,
                "maxOutputTokens": 32_000,
            },
            "claude-opus-4-7[1m]": {
                "inputTokens":     6, "outputTokens":  12,
                "cacheCreationInputTokens": 11_677,
                "cacheReadInputTokens": 0,
                "contextWindow":   1_000_000,
                "maxOutputTokens": 64_000,
            },
        }

    def test_stamps_window_and_used_when_modelUsage_present(
        self, mock_claude_bin, mock_stream_run: MagicMock,
    ) -> None:
        agent = ClaudeAgent(model="claude-opus-4-7")
        stdout = "\n".join((
            _assistant_event("round 1", usage={
                "input_tokens": 20,
                "cache_creation_input_tokens": 12_000,
                "cache_read_input_tokens": 0,
            }),
            _assistant_event("round 2", usage={
                "input_tokens": 37,
                "cache_creation_input_tokens": 410,
                "cache_read_input_tokens": 37_000,
            }),
            _result_event(
                cost=0.07, in_tok=19, out_tok=12,
                cache_create=22_453, cache_read=464_054,
                model_usage=self._opus_model_usage(),
            ),
        ))
        mock_stream_run.return_value = _stream_result(stdout)
        agent.invoke("hi", "/project")
        assert agent.last_context_window_tokens == 1_000_000
        # Provider usage remains the aggregate for the whole CLI invocation.
        assert agent.last_tokens_in == 19 + 22_453 + 464_054
        # Context usage is the latest assistant-message input scope, matching
        # the live context-window counter instead of the aggregate.
        assert agent.last_context_used_tokens == 37 + 410 + 37_000
        assert agent.last_context_peak_tokens == 37 + 410 + 37_000
        assert agent.last_context_used_tokens != agent.last_tokens_in

    def test_resolver_flips_to_runtime_reported(
        self, mock_claude_bin, mock_stream_run: MagicMock,
    ) -> None:
        # Sanity check: with the two attributes set, the M14.4
        # resolver returns RUNTIME_REPORTED without code changes.
        from pipeline.observability.context_pressure import (
            ContextSource,
            resolve_context_pressure,
        )
        agent = ClaudeAgent(model="claude-opus-4-7")
        live_usage = {
            "input_tokens": 37,
            "cache_creation_input_tokens": 410,
            "cache_read_input_tokens": 37_000,
        }
        stdout = "\n".join((
            _assistant_event("review", usage=live_usage),
            _result_event(
                cost=0.07, in_tok=19, out_tok=12,
                cache_create=22_453, cache_read=464_054,
                model_usage=self._opus_model_usage(),
            ),
        ))
        mock_stream_run.return_value = _stream_result(stdout)
        # ClaudeAgent.last_estimated_tokens_in is not set by the
        # adapter; the resolver only needs window+used. Pre-stamp a
        # legacy-style estimate to confirm runtime_reported still
        # wins.
        agent.last_estimated_tokens_in = 999  # type: ignore[attr-defined]
        agent.invoke("hi", "/project")
        reading = resolve_context_pressure(agent)
        assert reading.context_source is ContextSource.RUNTIME_REPORTED
        assert reading.context_window_tokens == 1_000_000
        assert reading.context_used_tokens == 37_447
        assert reading.context_remaining_tokens == 1_000_000 - 37_447
        # ratio = used / window
        assert reading.context_fill_ratio == pytest.approx(
            37_447 / 1_000_000,
        )

    def test_no_modelUsage_keeps_runtime_attrs_none(
        self, mock_claude_bin, mock_stream_run: MagicMock,
    ) -> None:
        # Older CLI builds / aborted turns may emit a result event
        # without modelUsage. The adapter must blank the runtime
        # attrs so the resolver falls back to orcho_estimated
        # instead of silently using stale values from a prior call.
        agent = ClaudeAgent(model="claude-opus-4-7")
        # Prior call would have stamped a window; simulate it.
        agent.last_context_window_tokens = 200_000
        agent.last_context_used_tokens = 1234
        mock_stream_run.return_value = _stream_result(_result_event(
            cost=0.01, in_tok=10, out_tok=5,
            # model_usage omitted
        ))
        agent.invoke("hi", "/project")
        assert agent.last_context_window_tokens is None
        assert agent.last_context_used_tokens is None
        assert agent.last_context_peak_tokens is None

    def test_no_assistant_usage_keeps_context_used_none(
        self, mock_claude_bin, mock_stream_run: MagicMock,
    ) -> None:
        # ``result.usage`` is aggregate provider usage. Without per-message
        # assistant usage, do not re-label the aggregate as live context.
        agent = ClaudeAgent(model="claude-opus-4-7")
        mock_stream_run.return_value = _stream_result(_result_event(
            cost=0.07, in_tok=19, out_tok=12,
            cache_create=22_453, cache_read=464_054,
            model_usage=self._opus_model_usage(),
        ))
        agent.invoke("hi", "/project")
        assert agent.last_tokens_in == 486_526
        assert agent.last_context_window_tokens == 1_000_000
        assert agent.last_context_used_tokens is None
        assert agent.last_context_peak_tokens is None

    def test_zero_or_missing_contextWindow_treated_as_unavailable(
        self, mock_claude_bin, mock_stream_run: MagicMock,
    ) -> None:
        agent = ClaudeAgent(model="claude-opus-4-7")
        mock_stream_run.return_value = _stream_result(_result_event(
            cost=0.01, in_tok=10, out_tok=5,
            model_usage={
                "claude-opus-4-7[1m]": {
                    "inputTokens": 10, "contextWindow": 0,
                },
            },
        ))
        agent.invoke("hi", "/project")
        assert agent.last_context_window_tokens is None
        assert agent.last_context_used_tokens is None
        assert agent.last_context_peak_tokens is None


class TestEventBridgeFields:
    """Phase 7.10: ``agent.start`` and ``agent.end`` events carry the
    bridge edge so a UI / decision-provenance graph can draw round-N
    resume-of-round-N-1 without parsing stdout."""

    def _events_for_call(self, mock_stream_run, claude, **invoke_kw):
        import core.observability.events as _events
        captured: list = []
        emit = _events.emit

        def _capture(kind, **payload):
            captured.append((kind, payload))
            return emit(kind, **payload)

        try:
            _events.emit = _capture  # type: ignore[assignment]
            claude.invoke("task", "/project", **invoke_kw)
        finally:
            _events.emit = emit  # type: ignore[assignment]
        return captured

    def test_agent_start_carries_continue_session_and_resumed_id(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        claude.session_id = "sess-prior"
        events = self._events_for_call(
            mock_stream_run, claude, continue_session=True,
        )
        start = next(p for k, p in events if k == "agent.start")
        assert start["continue_session"] is True
        assert start["resumed_session_id"] == "sess-prior"

    def test_agent_start_no_resume_when_no_session(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        # continue_session=True but session_id is None → resumed_session_id=None
        events = self._events_for_call(
            mock_stream_run, claude, continue_session=True,
        )
        start = next(p for k, p in events if k == "agent.start")
        assert start["continue_session"] is True
        assert start["resumed_session_id"] is None

    def test_agent_end_carries_captured_session_id(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        mock_stream_run.return_value = _stream_result(
            _assistant_event("ok", session_id="sess-fresh")
        )
        events = self._events_for_call(mock_stream_run, claude)
        end = next(p for k, p in events if k == "agent.end")
        assert end["captured_session_id"] == "sess-fresh"

    def test_agent_call_id_matches_start_and_end(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        """``agent_call_id`` is a per-invoke UUID emitted on both
        agent.start and agent.end so DAG fan-outs / cross-project
        sub-runs can pair starts to ends without relying on order or
        phase tag."""
        events = self._events_for_call(mock_stream_run, claude)
        start = next(p for k, p in events if k == "agent.start")
        end = next(p for k, p in events if k == "agent.end")
        assert start["agent_call_id"]
        assert start["agent_call_id"].startswith("call_")
        assert start["agent_call_id"] == end["agent_call_id"]

    def test_agent_call_id_unique_per_invoke(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        e1 = self._events_for_call(mock_stream_run, claude)
        e2 = self._events_for_call(mock_stream_run, claude)
        cid1 = next(p for k, p in e1 if k == "agent.start")["agent_call_id"]
        cid2 = next(p for k, p in e2 if k == "agent.start")["agent_call_id"]
        assert cid1 != cid2

    def test_retry_emits_paired_start_end_per_attempt(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock, monkeypatch,
    ) -> None:
        """Each retry attempt is its own subprocess, so it emits a fresh
        start→end pair with a unique ``agent_call_id``. A retry must not
        reuse the prior id (which would pair one start with N ends and
        break event consumers that match start↔end)."""
        from agents.runtimes import _failures
        monkeypatch.setattr(_failures, "_sleep", lambda _s: None)

        # First attempt: transient connection failure → retried. Second:
        # clean success.
        mock_stream_run.side_effect = [
            _stream_result(stdout="", returncode=1, stderr="connection refused"),
            _stream_result(_assistant_event("recovered")),
        ]
        events = self._events_for_call(mock_stream_run, claude)

        starts = [p for k, p in events if k == "agent.start"]
        ends = [p for k, p in events if k == "agent.end"]
        assert len(starts) == 2
        assert len(ends) == 2
        start_ids = [p["agent_call_id"] for p in starts]
        end_ids = [p["agent_call_id"] for p in ends]
        # Unique per attempt, and each start pairs with exactly one end.
        assert len(set(start_ids)) == 2
        assert start_ids == end_ids


# ── invoke(): guardrail ────────────────────────────────────────────────────


class TestInvokeGuardrailBlocked:
    def test_returns_guardrail_message_when_sentinel_in_stderr(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        from agents.command_guard import ORCHO_GUARDRAIL_BLOCKED
        mock_stream_run.return_value = _stream_result(
            stdout="",
            returncode=1,
            stderr=f"{ORCHO_GUARDRAIL_BLOCKED}: destructive_git: rm -rf .git",
        )
        out = claude.invoke(
            "rm everything", "/project", mutates_artifacts=True,
        )
        assert out.startswith(ORCHO_GUARDRAIL_BLOCKED)
        assert "destructive_git" in out


# ── invoke(): attachment defensive contract ────────────────────────────────


class TestInvokeAttachmentsContract:
    def test_text_attachment_raises_value_error(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        """TEXT attachments are rendered outside the runtime; passing TEXT
        into ``invoke(attachments=...)`` is a caller bug.
        """
        from pipeline.runtime.roles import AttachmentKind
        from pipeline.runtime.steps import Attachment

        text_att = Attachment(
            kind=AttachmentKind.TEXT,
            name="ctx.md",
            content_b64="aGk=",
        )
        with pytest.raises(ValueError, match="TEXT"):
            claude.invoke("hi", "/project", attachments=(text_att,))

    def test_multimodal_attachments_accepted(
        self, claude: ClaudeAgent, mock_stream_run: MagicMock,
    ) -> None:
        """IMAGE / BINARY pass through without raising. Runtime currently
        doesn't translate them into CLI args (Claude CLI doesn't expose a
        multimodal flag yet), but the Protocol contract accepts them.
        """
        from pipeline.runtime.roles import AttachmentKind
        from pipeline.runtime.steps import Attachment

        image_att = Attachment(
            kind=AttachmentKind.IMAGE,
            name="screenshot.png",
            content_b64="aGk=",
            mime_type="image/png",
        )
        # No raise.
        claude.invoke("look", "/project", attachments=(image_att,))


# ── probe_identity (account diagnostics) ───────────────────────────────────


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    """Shape mimicking ``subprocess.CompletedProcess``."""
    result = MagicMock()
    result.stdout = stdout
    result.returncode = returncode
    result.stderr = stderr
    return result


# Sample output shaped like real ``claude auth status`` — includes fields we
# must NOT surface (orgId, subscriptionType, authMethod) to prove sanitization.
_CLAUDE_STATUS_JSON = json.dumps({
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "email": "sales@example.com",
    "orgId": "60a9e843-5225-4c02-b573-64cfe3235e10",
    "orgName": "Smart-gamma",
    "subscriptionType": "max",
})


class TestClaudeProbeIdentity:
    def test_parses_account_label_and_email(self, claude, monkeypatch) -> None:
        claude.bin = "claude"
        run = MagicMock(return_value=_completed(_CLAUDE_STATUS_JSON))
        monkeypatch.setattr(agents_module.subprocess, "run", run)

        ident = claude.probe_identity()

        assert ident.available is True
        assert ident.runtime == "claude"
        assert ident.provider == "anthropic"
        assert ident.account_label == "Smart-gamma"
        assert ident.email == "sales@example.com"
        assert ident.source == "runtime_status"
        # Sanitization: no token/org-id/subscription/auth-method leaks anywhere.
        blob = repr(ident)
        for secret in ("60a9e843", "subscriptionType", "max", "claude.ai", "authMethod"):
            assert secret not in blob

    def test_uses_short_timeout_and_status_subcommand(self, claude, monkeypatch) -> None:
        claude.bin = "claude"
        run = MagicMock(return_value=_completed(_CLAUDE_STATUS_JSON))
        monkeypatch.setattr(agents_module.subprocess, "run", run)

        claude.probe_identity()

        args, kwargs = run.call_args
        cmd = args[0]
        assert cmd[-2:] == ["auth", "status"]
        assert kwargs["timeout"] <= 5
        assert kwargs["capture_output"] is True

    def test_empty_stdout_is_unavailable(self, claude, monkeypatch) -> None:
        claude.bin = "claude"
        monkeypatch.setattr(
            agents_module.subprocess, "run",
            MagicMock(return_value=_completed("")),
        )
        ident = claude.probe_identity()
        assert ident.available is False
        assert ident.account_label is None

    def test_garbage_stdout_is_unavailable(self, claude, monkeypatch) -> None:
        claude.bin = "claude"
        monkeypatch.setattr(
            agents_module.subprocess, "run",
            MagicMock(return_value=_completed("not json at all")),
        )
        ident = claude.probe_identity()
        assert ident.available is False
        assert ident.source == "unparsable_status"

    def test_status_without_account_fields_is_unavailable(self, claude, monkeypatch) -> None:
        claude.bin = "claude"
        body = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        monkeypatch.setattr(
            agents_module.subprocess, "run",
            MagicMock(return_value=_completed(body)),
        )
        ident = claude.probe_identity()
        assert ident.available is False
        assert ident.source == "no_account_in_status"

    def test_nonzero_returncode_is_unavailable(self, claude, monkeypatch) -> None:
        claude.bin = "claude"
        monkeypatch.setattr(
            agents_module.subprocess, "run",
            MagicMock(return_value=_completed(_CLAUDE_STATUS_JSON, returncode=1)),
        )
        ident = claude.probe_identity()
        assert ident.available is False

    def test_timeout_does_not_raise(self, claude, monkeypatch) -> None:
        claude.bin = "claude"
        import subprocess as _sp

        def boom(*a, **k):
            raise _sp.TimeoutExpired(cmd="claude", timeout=3)

        monkeypatch.setattr(agents_module.subprocess, "run", boom)
        ident = claude.probe_identity()  # must not raise
        assert ident.available is False
        assert ident.source == "no_status_surface"

    def test_missing_binary_is_unavailable(self, monkeypatch) -> None:
        from core.infra import config

        def missing() -> str:
            raise RuntimeError("Cannot find 'claude' binary")

        monkeypatch.setattr(config, "get_claude_bin", missing)
        agent = ClaudeAgent(model="m")
        ident = agent.probe_identity()  # must not raise
        assert ident.available is False
        assert ident.source == "no_binary"
