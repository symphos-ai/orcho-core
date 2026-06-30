"""GeminiAgent runtime adapter contracts.

The agent wraps the ``@google/gemini-cli`` Node CLI. Every invocation
runs through ``gemini -p <prompt> -m <model> -o stream-json
--skip-trust --approval-mode <plan|yolo>`` so the CLI emits one JSON
event per line. These tests pin the wrapper behaviour around
``_stream_run``:

* Read vs. write maps to ``--approval-mode plan`` vs. ``yolo``.
* Stream-json output is always requested so a ``session_id`` lands in
  stdout and the runtime is bridge-capable.
* ``continue_session=True`` with a previously captured ``session_id``
  threads ``-r <id>`` so the bridge survives across invocations.
* Module-level helpers (``_extract_assistant_text``,
  ``_extract_last_result``, ``_extract_session_id``, ``_capture_usage``)
  tolerate non-JSON noise and unexpected shapes — they run on every
  invocation, so a parser miss must not crash the agent.

No real subprocess: ``agents._stream_run`` is monkeypatched per-test
via the local ``mock_stream_run`` fixture.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import agents as agents_module
from agents.runtimes.gemini import (
    APPROVAL_MODE_READ,
    APPROVAL_MODE_WRITE,
    GeminiAgent,
    _capture_usage,
    _extract_assistant_text,
    _extract_last_result,
    _extract_session_id,
)
from core.io.retry import AgentAuthenticationError

# ── helpers / fixtures ─────────────────────────────────────────────────────


def _stream_result(
    stdout: str = "",
    returncode: int = 0,
    stderr: str = "",
    duration: float = 0.1,
):
    """``_stream_run`` returns a (stdout, returncode, stderr, duration) tuple."""
    return (stdout, returncode, stderr, duration)


def _init_event(session_id: str, model: str = "gemini-2.5-flash") -> str:
    return json.dumps(
        {"type": "init", "session_id": session_id, "model": model}
    )


def _assistant_event(text: str) -> str:
    return json.dumps(
        {"type": "message", "role": "assistant", "content": text, "delta": True}
    )


def _result_event(
    *,
    total: int = 5577,
    input_tok: int = 5553,
    output_tok: int = 1,
    cached: int = 0,
    duration_ms: int = 2377,
    tool_calls: int = 0,
    status: str = "success",
) -> str:
    return json.dumps({
        "type": "result",
        "status": status,
        "stats": {
            "total_tokens": total,
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "cached": cached,
            "duration_ms": duration_ms,
            "tool_calls": tool_calls,
        },
    })


@pytest.fixture
def gemini(mock_gemini_bin: None) -> GeminiAgent:
    return GeminiAgent(model="gemini-test")


@pytest.fixture
def mock_stream_run(monkeypatch) -> MagicMock:
    mock = MagicMock(return_value=_stream_result(""))
    monkeypatch.setattr(agents_module, "_stream_run", mock)
    return mock


# ── construction + binary lookup ───────────────────────────────────────────


def test_missing_gemini_binary_error_names_runtime(monkeypatch) -> None:
    from core.infra import config

    def missing() -> str:
        raise RuntimeError("Cannot find 'gemini' binary")

    monkeypatch.setattr(config, "get_gemini_bin", missing)
    agent = GeminiAgent(model="gemini-test")

    with pytest.raises(RuntimeError, match="gemini runtime cannot start"):
        _ = agent.bin


def test_default_runtime_attr() -> None:
    assert GeminiAgent.runtime == "gemini"


def test_construction_is_side_effect_free(mock_gemini_bin: None) -> None:
    # Constructor must not touch disk or invoke the CLI.
    agent = GeminiAgent("gemini-test", effort="high")
    assert agent.model == "gemini-test"
    assert agent.effort == "high"
    assert agent.session_id is None
    assert agent.last_tokens_in is None
    assert agent.last_cost_usd is None


# ── invoke(): CLI command shape ────────────────────────────────────────────


class TestInvokeCliShape:
    """Pin the exact CLI flags Orcho passes — they're the contract with the
    Gemini CLI version we test against."""

    def test_stream_json_and_skip_trust_always_present(
        self, gemini: GeminiAgent, mock_stream_run: MagicMock,
    ) -> None:
        gemini.invoke("any prompt", "/project")
        cmd = mock_stream_run.call_args[0][0]
        assert "-o" in cmd
        assert "stream-json" in cmd
        assert "--skip-trust" in cmd

    def test_read_uses_plan_approval_mode(
        self, gemini: GeminiAgent, mock_stream_run: MagicMock,
    ) -> None:
        gemini.invoke("read-only call", "/project")
        cmd = mock_stream_run.call_args[0][0]
        idx = cmd.index("--approval-mode")
        assert cmd[idx + 1] == APPROVAL_MODE_READ == "plan"

    def test_mutates_artifacts_uses_yolo_approval_mode(
        self, gemini: GeminiAgent, mock_stream_run: MagicMock,
    ) -> None:
        gemini.invoke("write call", "/project", mutates_artifacts=True)
        cmd = mock_stream_run.call_args[0][0]
        idx = cmd.index("--approval-mode")
        assert cmd[idx + 1] == APPROVAL_MODE_WRITE == "yolo"

    def test_model_flag_threaded(
        self, gemini: GeminiAgent, mock_stream_run: MagicMock,
    ) -> None:
        gemini.invoke("hi", "/project")
        cmd = mock_stream_run.call_args[0][0]
        idx = cmd.index("-m")
        assert cmd[idx + 1] == "gemini-test"

    def test_prompt_passed_via_p_flag(
        self, gemini: GeminiAgent, mock_stream_run: MagicMock,
    ) -> None:
        gemini.invoke("the prompt text", "/project")
        cmd = mock_stream_run.call_args[0][0]
        idx = cmd.index("-p")
        assert cmd[idx + 1] == "the prompt text"

    def test_cwd_passed_through(
        self, gemini: GeminiAgent, mock_stream_run: MagicMock,
    ) -> None:
        gemini.invoke("hi", "/my/project")
        assert mock_stream_run.call_args[1]["cwd"] == "/my/project"

    def test_wires_stream_and_log_filters_to_gemini_formatter(
        self, gemini: GeminiAgent, mock_stream_run: MagicMock,
    ) -> None:
        from agents.stream_parsers import format_gemini_line_for_stdout

        gemini.invoke("hi", "/project")
        kwargs = mock_stream_run.call_args[1]
        assert kwargs["stdout_filter"] is format_gemini_line_for_stdout
        assert kwargs["log_filter"] is format_gemini_line_for_stdout
        assert kwargs["return_filter"].__name__ == "elide_tool_result_line_for_model"


# ── invoke(): assistant text extraction ────────────────────────────────────


class TestInvokeOutput:
    def test_returns_concatenated_assistant_text(
        self, gemini: GeminiAgent, mock_stream_run: MagicMock,
    ) -> None:
        mock_stream_run.return_value = _stream_result(
            "\n".join([
                _init_event("sid-1"),
                _assistant_event("hello "),
                _assistant_event("world"),
                _result_event(),
            ])
        )
        assert gemini.invoke("hi", "/project") == "hello world"

    def test_falls_back_to_raw_stdout_when_no_assistant_text(
        self, gemini: GeminiAgent, mock_stream_run: MagicMock,
    ) -> None:
        mock_stream_run.return_value = _stream_result("raw blob, no events")
        assert gemini.invoke("hi", "/project") == "raw blob, no events"

    def test_stderr_surfaced_on_nonzero_returncode(
        self,
        gemini: GeminiAgent,
        mock_stream_run: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_stream_run.return_value = _stream_result(
            stdout="", returncode=1, stderr="gemini exploded",
        )
        # A non-zero exit is an API-client failure: the runtime must raise so
        # the run halts instead of returning the error text as a response.
        # stderr is still surfaced (printed) before the raise.
        with pytest.raises(RuntimeError):
            gemini.invoke("hi", "/project")
        captured = capsys.readouterr()
        assert "gemini exploded" in captured.out

    def test_auth_failure_raises_user_facing_error(
        self, gemini: GeminiAgent, mock_stream_run: MagicMock,
    ) -> None:
        mock_stream_run.return_value = _stream_result(
            stdout="Failed to authenticate. Invalid API key",
            returncode=1,
            stderr="",
        )
        with pytest.raises(AgentAuthenticationError) as exc_info:
            gemini.invoke("hi", "/project")

        msg = str(exc_info.value)
        assert "runtime='gemini'" in msg
        assert "model='gemini-test'" in msg
        assert "gemini /login" in msg
        assert "gemini /auth" in msg


# ── invoke(): session bridge ───────────────────────────────────────────────


class TestSessionBridge:
    def test_first_call_has_no_resume_flag(
        self, gemini: GeminiAgent, mock_stream_run: MagicMock,
    ) -> None:
        gemini.invoke("hi", "/project")
        cmd = mock_stream_run.call_args[0][0]
        assert "-r" not in cmd
        assert gemini._last_resumed_session_id is None

    def test_session_id_captured_from_init_event(
        self, gemini: GeminiAgent, mock_stream_run: MagicMock,
    ) -> None:
        mock_stream_run.return_value = _stream_result(
            "\n".join([
                _init_event("sid-captured"),
                _assistant_event("done"),
                _result_event(),
            ])
        )
        gemini.invoke("hi", "/project")
        assert gemini.session_id == "sid-captured"

    def test_continue_session_resumes_captured_id(
        self, gemini: GeminiAgent, mock_stream_run: MagicMock,
    ) -> None:
        mock_stream_run.return_value = _stream_result(
            "\n".join([_init_event("sid-A"), _result_event()])
        )
        gemini.invoke("first", "/project")
        gemini.invoke("second", "/project", continue_session=True)
        cmd = mock_stream_run.call_args[0][0]
        idx = cmd.index("-r")
        assert cmd[idx + 1] == "sid-A"
        assert gemini._last_resumed_session_id == "sid-A"
        assert gemini._last_continue_session is True

    def test_continue_session_without_captured_id_is_noop(
        self, gemini: GeminiAgent, mock_stream_run: MagicMock,
    ) -> None:
        gemini.invoke("first", "/project", continue_session=True)
        cmd = mock_stream_run.call_args[0][0]
        assert "-r" not in cmd
        assert gemini._last_resumed_session_id is None


class TestResetSession:
    def test_clears_a_non_empty_session_id(self, mock_gemini_bin: None) -> None:
        agent = GeminiAgent("gemini-test")
        agent.session_id = "abc123"
        agent._followup_resume_pending = True
        agent._last_resumed_session_id = "abc123"
        agent.reset_session()
        assert agent.session_id is None
        assert agent._followup_resume_pending is False
        assert agent._last_resumed_session_id is None


# ── module helpers ─────────────────────────────────────────────────────────


class TestExtractSessionId:
    def test_init_event_wins(self) -> None:
        stdout = "\n".join([_init_event("sid-init"), _assistant_event("...")])
        assert _extract_session_id(stdout) == "sid-init"

    def test_returns_none_when_absent(self) -> None:
        assert _extract_session_id(_assistant_event("no session here")) is None

    def test_tolerates_non_json_noise(self) -> None:
        noise = (
            "Warning: terminal does not support 256 colors.\n"
            "Falling back...\n"
            + _init_event("sid-after-noise")
        )
        assert _extract_session_id(noise) == "sid-after-noise"

    def test_empty_input(self) -> None:
        assert _extract_session_id("") is None


class TestExtractAssistantText:
    def test_concatenates_delta_chunks(self) -> None:
        stdout = "\n".join([
            _assistant_event("part one. "),
            _assistant_event("part two."),
        ])
        assert _extract_assistant_text(stdout) == "part one. part two."

    def test_ignores_non_assistant_messages(self) -> None:
        stdout = "\n".join([
            json.dumps({
                "type": "message", "role": "user", "content": "ignored",
            }),
            _assistant_event("kept"),
        ])
        assert _extract_assistant_text(stdout) == "kept"

    def test_empty_when_no_events(self) -> None:
        assert _extract_assistant_text("garbage no json here") == ""


class TestExtractLastResult:
    def test_finds_terminal_result(self) -> None:
        stdout = "\n".join([_assistant_event("..."), _result_event(total=42)])
        result = _extract_last_result(stdout)
        assert result is not None
        assert result.get("stats", {}).get("total_tokens") == 42

    def test_returns_none_when_absent(self) -> None:
        assert _extract_last_result(_assistant_event("no result line")) is None


class TestCaptureUsage:
    def test_captures_token_split_from_stats(
        self, mock_gemini_bin: None,
    ) -> None:
        agent = GeminiAgent("gemini-test")
        stdout = "\n".join([
            _init_event("sid"),
            _assistant_event("..."),
            _result_event(
                total=1100, input_tok=1000, output_tok=100, cached=200,
                tool_calls=3,
            ),
        ])
        _capture_usage(agent, stdout)
        assert agent.last_tokens_in == 1000
        assert agent.last_tokens_in_fresh == 800  # 1000 - 200
        assert agent.last_tokens_in_cache_read == 200
        assert agent.last_tokens_out == 100
        assert agent.last_tokens_total == 1100
        assert agent.last_tool_use_count == 0  # no tool_use events in stream
        assert agent.last_cost_usd is None

    def test_cached_exceeding_input_clamps_fresh_to_zero(
        self, mock_gemini_bin: None,
    ) -> None:
        """Degenerate stat where ``cached > input_tokens`` must clamp fresh
        input to 0, not overstate it back up to ``input_tokens``."""
        agent = GeminiAgent("gemini-test")
        stdout = "\n".join([
            _init_event("sid"),
            _result_event(input_tok=100, cached=250, output_tok=10),
        ])
        _capture_usage(agent, stdout)
        assert agent.last_tokens_in == 100
        assert agent.last_tokens_in_cache_read == 250
        assert agent.last_tokens_in_fresh == 0

    def test_clears_on_missing_result(self, mock_gemini_bin: None) -> None:
        agent = GeminiAgent("gemini-test")
        agent.last_tokens_in = 999
        agent.last_tokens_out = 999
        _capture_usage(agent, "no result here")
        assert agent.last_tokens_in is None
        assert agent.last_tokens_out is None
        assert agent.last_tokens_total is None


# ── guardrail wiring ───────────────────────────────────────────────────────


class TestGuardrail:
    def test_destructive_git_aborts_stream(
        self, monkeypatch, tmp_path, mock_gemini_bin: None,
    ) -> None:
        from agents.command_guard import ORCHO_GUARDRAIL_BLOCKED
        from agents.stream import StreamAbort

        def fake_stream_run(cmd, **kwargs):
            on_line = kwargs.get("on_line")
            assert on_line is not None
            line = json.dumps({
                "type": "tool_use",
                "tool_name": "run_shell_command",
                "tool_id": "t1",
                "parameters": {"command": "git checkout -- test_calc.py"},
            }) + "\n"
            try:
                on_line(line)
            except StreamAbort as exc:
                return "", 1, f"[ABORTED by stream guard: {exc}]", 0.01
            raise AssertionError("expected StreamAbort from command guard")

        monkeypatch.setattr(agents_module, "_stream_run", fake_stream_run)

        out = GeminiAgent(model="gemini-test").invoke(
            "fix it", str(tmp_path), mutates_artifacts=True,
        )

        assert ORCHO_GUARDRAIL_BLOCKED in out


class TestAttachmentsContract:
    def test_text_attachment_rejected(
        self, gemini: GeminiAgent, mock_stream_run: MagicMock,
    ) -> None:
        from pipeline.runtime.roles import AttachmentKind
        from pipeline.runtime.steps import Attachment

        bad = Attachment(
            name="ignored.txt",
            kind=AttachmentKind.TEXT,
            content_path="/tmp/ignored.txt",
        )
        with pytest.raises(ValueError, match="TEXT must be rendered"):
            gemini.invoke("hi", "/p", attachments=(bad,))
        # No subprocess call should have fired.
        mock_stream_run.assert_not_called()
