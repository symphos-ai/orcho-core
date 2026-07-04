"""API-client failure detection + controlled halt for runtime adapters.

Regression cover for the bug where a runtime swallowed an API-client error
(connection refused, stream disconnect, "Unable to connect to API") and
returned the error text as a normal assistant response — marking the phase ✓
and letting the run walk on into the next phase. The contract now: an
API-client failure raises a typed ``AgentCallError`` so the lifecycle FSM
converts it into a reasoned halt; transient transport shapes retry first.

Three layers are exercised:
  * ``core.io.retry`` classification — connection signatures map to
    :class:`ApiConnectionError`.
  * ``agents.runtimes._failures.raise_on_runtime_failure`` — the single
    translation point (auth / non-zero exit / exit-0 transport sentinel).
  * The runtimes themselves — a real ``invoke()`` raises (and retries
    transient errors) instead of returning the error text.
"""

from __future__ import annotations

import pytest

import agents as agents_module
from agents.runtimes import _failures
from agents.runtimes.claude import ClaudeAgent
from agents.runtimes.codex import CodexAgent
from agents.runtimes.gemini import GeminiAgent
from core.io.retry import (
    AgentAccessError,
    AgentAuthenticationError,
    AgentCallError,
    AgentCancelledError,
    AgentProcessKilledError,
    ApiConnectionError,
    SystemResourceError,
    classify_error,
    classify_from_exit,
)

# ── classification ──────────────────────────────────────────────────────────

class TestClassifyConnection:
    @pytest.mark.parametrize(
        "text",
        [
            "Unable to connect to API (ConnectionRefused)",
            "connection refused",
            "stream disconnected before completion",
            "failed to lookup address information: nodename nor servname provided",
            "503 service unavailable",
        ],
    )
    def test_connection_signatures_map_to_api_connection_error(self, text: str) -> None:
        assert isinstance(classify_error(RuntimeError("x"), stderr=text), ApiConnectionError)

    def test_nonzero_exit_with_connection_text(self) -> None:
        err = classify_from_exit(1, "Unable to connect to API (ConnectionRefused)")
        assert isinstance(err, ApiConnectionError)

    def test_connection_does_not_shadow_auth(self) -> None:
        # An auth signature still wins over a connection one in the same blob.
        err = classify_error(RuntimeError("x"), stderr="401 invalid api key; connection refused")
        assert isinstance(err, AgentAuthenticationError)


class TestClassifyProviderAccess:
    def test_claude_subscription_access_disabled_is_access_error(self) -> None:
        err = classify_from_exit(
            1,
            (
                "Your organization has disabled Claude subscription access for "
                "Claude Code. Use an Anthropic API key instead, or ask your "
                "admin to enable access."
            ),
        )

        assert isinstance(err, AgentAccessError)
        assert "Provider access unavailable" in str(err)

    def test_access_error_does_not_shadow_auth(self) -> None:
        err = classify_from_exit(
            1,
            "invalid api key; subscription access disabled",
        )

        assert isinstance(err, AgentAuthenticationError)


# ── raise_on_runtime_failure (the single translation point) ──────────────────

class TestRaiseOnRuntimeFailure:
    def _call(
        self,
        *,
        returncode: int,
        stdout: str = "",
        stderr: str = "",
        reply_text: str | None = None,
    ) -> None:
        _failures.raise_on_runtime_failure(
            runtime="claude", model="m", cli="claude",
            returncode=returncode, stdout=stdout, stderr=stderr,
            reply_text=reply_text,
        )

    def test_healthy_result_returns_none(self) -> None:
        # rc==0 with ordinary output is not a failure — must not raise.
        assert self._call(returncode=0, reply_text="here is your plan") is None

    def test_generic_nonzero_raises_agent_call_error(self) -> None:
        with pytest.raises(AgentCallError):
            self._call(returncode=1, stderr="some opaque failure")

    def test_auth_nonzero_raises_auth_error(self) -> None:
        with pytest.raises(AgentAuthenticationError):
            self._call(returncode=1, stderr="401 invalid api key")

    def test_access_nonzero_raises_access_error(self) -> None:
        with pytest.raises(AgentAccessError):
            self._call(
                returncode=1,
                stderr=(
                    "Your organization has disabled Claude subscription access "
                    "for Claude Code. Use an Anthropic API key instead."
                ),
            )

    def test_connection_nonzero_raises_connection_error(self) -> None:
        with pytest.raises(ApiConnectionError):
            self._call(returncode=1, stderr="connection refused")

    @pytest.mark.parametrize("returncode", [-9, 137])
    def test_kill_signal_raises_process_killed_error(self, returncode: int) -> None:
        # Signal-death shape with no classifiable stderr text → kill-type,
        # which is a SystemResourceError subclass (auto provider_runtime).
        with pytest.raises(AgentProcessKilledError) as excinfo:
            self._call(returncode=returncode, stderr="")
        assert isinstance(excinfo.value, SystemResourceError)
        assert "SIGKILL" in str(excinfo.value)

    @pytest.mark.parametrize("returncode", [-15, -2])
    def test_cancel_signal_raises_cancelled_error(self, returncode: int) -> None:
        with pytest.raises(AgentCancelledError) as excinfo:
            self._call(returncode=returncode, stderr="")
        # Cancel-type must NOT be a SystemResourceError (never recoverable).
        assert not isinstance(excinfo.value, SystemResourceError)

    def test_exit_zero_transport_reply_raises_connection_error(self) -> None:
        # The transcript case: CLI exits 0 but the model's own reply IS the
        # transport error. Must still halt.
        with pytest.raises(ApiConnectionError):
            self._call(
                returncode=0,
                reply_text="API Error: Unable to connect to API (ConnectionRefused)",
            )

    def test_exit_zero_error_reply_with_leading_fence_raises(self) -> None:
        # The error-shaped reply may arrive wrapped in markdown fences/quotes;
        # strip leading decoration before matching the first line.
        with pytest.raises(ApiConnectionError):
            self._call(
                returncode=0,
                reply_text='```\nAPI Error: Unable to connect to API\n```',
            )

    def test_exit_zero_stream_error_event_raises(self) -> None:
        # The CLI's reconnect loop can emit a structured stream error event
        # AS its final reply on a clean exit. A ``{"type":"error",...}`` event
        # is machine-emitted (not model prose), so a transport sentinel in its
        # message must still halt even though the line starts with ``{``.
        with pytest.raises(ApiConnectionError):
            self._call(
                returncode=0,
                reply_text='{"type":"error","message":"API Error: Unable to connect to API"}',
            )

    def test_exit_zero_non_error_json_event_is_not_a_failure(self) -> None:
        # A non-error JSON event mentioning a sentinel is plumbing, not the
        # reply — must not halt.
        assert self._call(
            returncode=0,
            reply_text='{"type":"system","note":"unable to connect to api docs"}',
        ) is None

    def test_exit_zero_prose_mentioning_phrase_is_not_a_failure(self) -> None:
        # Regression (P1): a legitimate answer that *mentions* the phrase in
        # prose is not a transport failure. Only a reply that *is* the error
        # (first meaningful line starts with a sentinel) halts.
        assert self._call(
            returncode=0,
            reply_text=(
                "Here is how to debug the 'API Error: Unable to connect to "
                "API' message you are seeing: check the proxy settings first."
            ),
        ) is None

    def test_exit_zero_stderr_noise_is_not_a_failure(self) -> None:
        # Regression: a clean exit-0 run with a valid reply must NOT be
        # discarded just because the CLI logged unrelated transport noise to
        # stderr (e.g. Codex "failed to record rollout"). The model answered.
        assert self._call(
            returncode=0,
            reply_text='{"verdict":"REJECTED","ship_ready":false}',
            stderr=(
                "2026-06-01T16:50:15Z ERROR codex_core::session: "
                "failed to record rollout: stream disconnected before completion"
            ),
        ) is None

    def test_exit_zero_ordinary_reply_is_not_a_failure(self) -> None:
        assert self._call(
            returncode=0, reply_text="the connection pool is healthy",
        ) is None


# ── runtimes raise (and retry transient) instead of returning error text ─────

def _stream(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return (stdout, returncode, stderr, 0.01)


@pytest.fixture(autouse=True)
def _runtime_test_environment(
    monkeypatch: pytest.MonkeyPatch,
    mock_claude_bin: None,
    mock_codex_bin: None,
    mock_gemini_bin: None,
) -> None:
    """Keep runtime retry backoff instant and CLI lookup hermetic."""
    monkeypatch.setattr(_failures, "_sleep", lambda _s: None)


class TestRuntimesHaltOnApiFailure:
    def test_claude_exit_zero_sentinel_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            agents_module, "_stream_run",
            lambda *a, **k: _stream(
                stdout="API Error: Unable to connect to API (ConnectionRefused)",
            ),
        )
        with pytest.raises(ApiConnectionError):
            ClaudeAgent(model="claude-test").invoke("hi", "/project")

    def test_claude_exit_zero_stream_error_event_raises(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Claude exits 0 but the stream's final line is a structured error
        # event (no assistant text), so reply_text falls back to raw stdout.
        # The JSON error event must still be recognised as a transport halt.
        monkeypatch.setattr(
            agents_module, "_stream_run",
            lambda *a, **k: _stream(
                stdout='{"type":"error","message":"API Error: Unable to connect to API"}',
            ),
        )
        with pytest.raises(ApiConnectionError):
            ClaudeAgent(model="claude-test").invoke("hi", "/project")

    def test_gemini_nonzero_connection_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            agents_module, "_stream_run",
            lambda *a, **k: _stream(returncode=1, stderr="connection refused"),
        )
        with pytest.raises(ApiConnectionError):
            GeminiAgent(model="gemini-test").invoke("hi", "/project")

    def test_codex_connection_is_retried_then_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def _fake(*a, **k):
            calls["n"] += 1
            return _stream(returncode=1, stderr="stream disconnected before completion")

        monkeypatch.setattr(agents_module, "_stream_run", _fake)
        with pytest.raises(ApiConnectionError):
            CodexAgent(model="gpt-test").invoke("task", "/project")
        # connection_max_retries=2 → 1 initial attempt + 2 retries = 3 calls.
        assert calls["n"] == 3

    def test_claude_subscription_access_disabled_is_not_retried(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = {"n": 0}

        def _fake(*a, **k):
            calls["n"] += 1
            return _stream(
                returncode=1,
                stderr=(
                    "Your organization has disabled Claude subscription access "
                    "for Claude Code. Use an Anthropic API key instead."
                ),
            )

        monkeypatch.setattr(agents_module, "_stream_run", _fake)
        with pytest.raises(AgentAccessError):
            ClaudeAgent(model="claude-test").invoke("hi", "/project")
        assert calls["n"] == 1

    def test_claude_transient_connection_then_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def _fake(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return _stream(returncode=1, stderr="connection refused")
            return _stream(stdout="recovered output")

        monkeypatch.setattr(agents_module, "_stream_run", _fake)
        out = ClaudeAgent(model="claude-test").invoke("hi", "/project")
        assert out == "recovered output"
        assert calls["n"] == 2

    def test_codex_sets_last_call_id_on_success(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Parity with Claude/Gemini: _last_call_id reflects the most recent
        # attempt after a successful invoke (the per-attempt id moved inside
        # the retry thunk and must still be recorded for codex).
        agent_message = (
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"done"}}'
        )
        monkeypatch.setattr(
            agents_module, "_stream_run",
            lambda *a, **k: _stream(stdout=agent_message),
        )
        agent = CodexAgent(model="gpt-test")
        agent.invoke("task", "/project")
        assert getattr(agent, "_last_call_id", None)
        assert agent._last_call_id.startswith("call_")

    def test_clean_exit_with_stderr_noise_does_not_retry(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The reported false positive: the model answered (exit 0, valid
        # reply), but the CLI logged an unrelated transport error to stderr.
        # The valid answer must be returned as-is — no discard, no retry loop.
        calls = {"n": 0}

        def _fake(*a, **k):
            calls["n"] += 1
            return _stream(
                stdout='{"verdict":"REJECTED"}',
                returncode=0,
                stderr=(
                    "ERROR codex_core::session: failed to record rollout: "
                    "stream disconnected before completion"
                ),
            )

        monkeypatch.setattr(agents_module, "_stream_run", _fake)
        out = ClaudeAgent(model="claude-test").invoke("hi", "/project")
        assert out == '{"verdict":"REJECTED"}'
        assert calls["n"] == 1


# ── run_invoke_with_retry: kill retries once under RUNTIME_RETRY_CONFIG ───────

class TestRunInvokeWithRetrySignalBudget:
    """RUNTIME_RETRY_CONFIG gives a kill-shaped death exactly one retry while a
    cancel-shaped death is never retried (generic non-zero exits stay 0)."""

    def test_runtime_config_pins_kill_budget_to_one(self) -> None:
        # The budget is intentional, not an inherited default.
        assert _failures.RUNTIME_RETRY_CONFIG.process_killed_max_retries == 1

    def test_kill_is_retried_exactly_once_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(_failures, "_sleep", lambda _s: None)
        calls = {"n": 0}

        def attempt() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                # Killed by SIGKILL (OOM), no classifiable stderr → kill-type.
                raise classify_from_exit(-9, "")
            return "recovered"

        out = _failures.run_invoke_with_retry(attempt, runtime="claude")
        assert out == "recovered"
        assert calls["n"] == 2  # one kill + one successful retry

    def test_cancel_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_failures, "_sleep", lambda _s: None)
        calls = {"n": 0}

        def attempt() -> str:
            calls["n"] += 1
            raise classify_from_exit(-15, "")

        with pytest.raises(AgentCancelledError):
            _failures.run_invoke_with_retry(attempt, runtime="claude")
        assert calls["n"] == 1  # no retry
