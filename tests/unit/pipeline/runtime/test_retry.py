"""
M1.3 Error Recovery & Retry tests.

Tests cover:
 * Error taxonomy: classify_error / classify_from_exit
 * RetryConfig: per-error max_retries, delay calculation, jitter
 * call_with_retry: success on first try, retry after N failures, exhaustion
 * Error types: auth, RateLimitError, ApiTimeoutError, ContextOverflowError, generic
 * Structured RetryEvent logging
 * @with_retry decorator
 * FailingMockProvider: N fails then success
"""

from __future__ import annotations

import subprocess

import pytest

from core.io.retry import (
    DEFAULT_RETRY_CONFIG,
    AgentAccessError,
    AgentAuthenticationError,
    AgentCallError,
    AgentCancelledError,
    AgentProcessKilledError,
    ApiTimeoutError,
    ContextOverflowError,
    ErrorType,
    RateLimitError,
    RetryConfig,
    RetryEvent,
    SystemResourceError,
    call_with_retry,
    classify_error,
    classify_from_exit,
    classify_signal_exit,
    sanitized_failure_excerpt,
    with_retry,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _no_sleep(s: float) -> None:
    """Drop-in sleep replacement for tests — instant."""


def _make_config(max_retries: int = 3, base_delay: float = 0.0) -> RetryConfig:
    return RetryConfig(
        max_retries=max_retries,
        rate_limit_max_retries=max_retries + 1,
        timeout_max_retries=max_retries,
        context_overflow_max_retries=1,
        base_delay=base_delay,
        jitter=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Error taxonomy
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifyError:
    def test_timeout_exception_maps_to_api_timeout(self) -> None:
        exc = subprocess.TimeoutExpired(cmd=["claude"], timeout=30)
        result = classify_error(exc)
        assert isinstance(result, ApiTimeoutError)

    def test_rate_limit_in_stderr(self) -> None:
        exc = RuntimeError("error")
        result = classify_error(exc, stderr="rate_limit_exceeded: quota reached")
        assert isinstance(result, RateLimitError)

    def test_auth_failure_in_stderr(self) -> None:
        exc = RuntimeError("error")
        result = classify_error(
            exc,
            stderr="Failed to authenticate. API Error: 401 Invalid authentication credentials",
        )
        assert isinstance(result, AgentAuthenticationError)

    def test_access_failure_in_stderr(self) -> None:
        exc = RuntimeError("error")
        result = classify_error(
            exc,
            stderr=(
                "Your organization has disabled Claude subscription access "
                "for Claude Code. Use an Anthropic API key instead."
            ),
        )
        assert isinstance(result, AgentAccessError)
        assert "Provider access unavailable" in str(result)

    def test_429_in_stderr(self) -> None:
        exc = RuntimeError("HTTP 429")
        result = classify_error(exc, stderr="429 too many requests")
        assert isinstance(result, RateLimitError)

    def test_context_overflow_in_stderr(self) -> None:
        exc = RuntimeError("error")
        result = classify_error(exc, stderr="context_length_exceeded")
        assert isinstance(result, ContextOverflowError)

    def test_prompt_too_long_in_stderr(self) -> None:
        exc = RuntimeError("error")
        result = classify_error(exc, stderr="prompt is too long, reduce the length")
        assert isinstance(result, ContextOverflowError)

    def test_timeout_in_stderr(self) -> None:
        exc = RuntimeError("error")
        result = classify_error(exc, stderr="request timed out after 30s")
        assert isinstance(result, ApiTimeoutError)

    def test_system_resource_in_stderr(self) -> None:
        exc = RuntimeError("exit=126")
        result = classify_error(exc, stderr="ORCHO_SYSTEM_PTY_EXHAUSTED")
        assert isinstance(result, SystemResourceError)
        assert "System resource exhausted" in str(result)

    def test_unknown_error_maps_to_agent_call_error(self) -> None:
        exc = RuntimeError("something weird happened")
        result = classify_error(exc)
        assert type(result) is AgentCallError

    def test_error_preserves_message(self) -> None:
        exc = RuntimeError("the specific message")
        result = classify_error(exc)
        assert "specific message" in str(result)

    def test_unknown_error_carries_actionable_hint(self) -> None:
        # The generic bucket is not auto-retried, so its message is the whole
        # remediation surface: it must lead with an actionable next step (the
        # terminal FAILED banner shows only the first line) while still
        # preserving the raw detail for --output debug.
        exc = RuntimeError("weird provider glitch xyz")
        msg = str(classify_error(exc))
        first_line = msg.splitlines()[0]
        assert "--output debug" in first_line
        assert "not retried" in first_line
        assert "weird provider glitch xyz" in msg


class TestClassifyFromExit:
    def test_exit_zero_returns_none(self) -> None:
        assert classify_from_exit(0, "") is None

    def test_nonzero_exit_with_rate_limit(self) -> None:
        result = classify_from_exit(1, "rate_limit_exceeded: quota")
        assert isinstance(result, RateLimitError)

    def test_nonzero_exit_with_auth_failure(self) -> None:
        result = classify_from_exit(1, "401 invalid authentication credentials")
        assert isinstance(result, AgentAuthenticationError)

    def test_nonzero_exit_with_access_failure(self) -> None:
        result = classify_from_exit(
            1,
            "subscription access disabled; ask your admin to enable access",
        )
        assert isinstance(result, AgentAccessError)

    def test_access_failure_message_skips_jsonl_plumbing(self) -> None:
        result = classify_from_exit(
            1,
            "",
            stdout=(
                '{"type":"system","subtype":"init","cwd":"/repo"}\n'
                '{"type":"assistant","message":{"content":[{"type":"text",'
                '"text":"Your organization has disabled Claude subscription '
                'access for Claude Code. Use an Anthropic API key instead."}]}}\n'
            ),
        )

        assert isinstance(result, AgentAccessError)
        message = str(result)
        assert "Your organization has disabled Claude subscription access" in message
        assert "switch this phase/runtime" in message
        assert '{"type":"system"' not in message
        assert "/repo" not in message

    def test_nonzero_exit_with_pty_exhaustion(self) -> None:
        result = classify_from_exit(126, "out of pty devices")
        assert isinstance(result, SystemResourceError)

    def test_nonzero_exit_generic(self) -> None:
        result = classify_from_exit(1, "something went wrong")
        assert isinstance(result, AgentCallError)


class TestClassifySignalExit:
    """Signal-death typing driven purely by the exit code's shape."""

    @pytest.mark.parametrize("exit_code", [-9, 137, -11, 139, -6, 134])
    def test_kill_shape_maps_to_process_killed(self, exit_code: int) -> None:
        result = classify_from_exit(exit_code, "")
        assert isinstance(result, AgentProcessKilledError)
        # Kill type is a SystemResourceError subclass → auto provider_runtime.
        assert isinstance(result, SystemResourceError)

    def test_kill_message_names_the_signal(self) -> None:
        result = classify_from_exit(-9, "")
        assert result is not None
        assert "SIGKILL" in str(result)
        # Shell 128+N convention normalises to the same signal name.
        assert "SIGKILL" in str(classify_from_exit(137, ""))

    @pytest.mark.parametrize("exit_code", [-2, -15, 130, 143])
    def test_cancel_shape_maps_to_cancelled(self, exit_code: int) -> None:
        result = classify_from_exit(exit_code, "")
        assert isinstance(result, AgentCancelledError)
        # Cancel type must NOT be a SystemResourceError (never recoverable).
        assert not isinstance(result, SystemResourceError)

    def test_cancel_message_names_the_signal(self) -> None:
        assert "SIGTERM" in str(classify_from_exit(-15, ""))
        assert "SIGINT" in str(classify_from_exit(-2, ""))

    def test_plain_nonzero_stays_generic(self) -> None:
        # 3 is not a signal shape → exactly generic AgentCallError, not a
        # signal subclass.
        result = classify_from_exit(3, "")
        assert type(result) is AgentCallError

    def test_unrecognised_signal_stays_generic(self) -> None:
        # SIGHUP (1) is a signal but not in the kill/cancel taxonomy → generic.
        result = classify_from_exit(-1, "")
        assert type(result) is AgentCallError

    def test_text_classification_keeps_priority_over_signal_shape(self) -> None:
        # A recognised rate-limit signature on a kill-shaped code must stay a
        # RateLimitError — the signal branch only fires for a bare generic.
        result = classify_from_exit(137, "rate_limit_exceeded: quota reached")
        assert isinstance(result, RateLimitError)
        assert not isinstance(result, AgentProcessKilledError)

    def test_helper_returns_none_for_non_signal(self) -> None:
        assert classify_signal_exit(3) is None
        assert classify_signal_exit(1) is None
        assert classify_signal_exit(-1) is None


# ─────────────────────────────────────────────────────────────────────────────
# RetryConfig
# ─────────────────────────────────────────────────────────────────────────────

class TestRetryConfig:
    def test_rate_limit_uses_rate_limit_max(self) -> None:
        cfg = RetryConfig(max_retries=3, rate_limit_max_retries=5)
        err = RateLimitError("rl")
        assert cfg.max_retries_for(err) == 5

    def test_authentication_is_not_retried(self) -> None:
        cfg = RetryConfig(max_retries=3)
        err = AgentAuthenticationError("auth")
        assert cfg.max_retries_for(err) == 0

    def test_access_is_not_retried(self) -> None:
        cfg = RetryConfig(max_retries=3)
        err = AgentAccessError("subscription access disabled")
        assert cfg.max_retries_for(err) == 0

    def test_timeout_uses_timeout_max(self) -> None:
        cfg = RetryConfig(max_retries=3, timeout_max_retries=2)
        err = ApiTimeoutError("to")
        assert cfg.max_retries_for(err) == 2

    def test_context_overflow_uses_context_max(self) -> None:
        cfg = RetryConfig(max_retries=3, context_overflow_max_retries=1)
        err = ContextOverflowError("ctx")
        assert cfg.max_retries_for(err) == 1

    def test_system_resource_is_not_retried(self) -> None:
        cfg = RetryConfig(max_retries=3)
        err = SystemResourceError("pty")
        assert cfg.max_retries_for(err) == 0

    def test_process_killed_uses_explicit_knob(self) -> None:
        cfg = RetryConfig(max_retries=3, process_killed_max_retries=1)
        err = AgentProcessKilledError("killed by SIGKILL (exit=-9)")
        assert cfg.max_retries_for(err) == 1

    def test_process_killed_default_knob_is_one(self) -> None:
        err = AgentProcessKilledError("killed by SIGKILL (exit=-9)")
        assert DEFAULT_RETRY_CONFIG.max_retries_for(err) == 1

    def test_process_killed_does_not_disturb_base_system_resource(self) -> None:
        # The kill subclass gets a budget; the base PTY-style form stays 0.
        cfg = RetryConfig(max_retries=3, process_killed_max_retries=2)
        assert cfg.max_retries_for(SystemResourceError("pty")) == 0
        assert cfg.max_retries_for(AgentProcessKilledError("killed")) == 2

    def test_cancelled_is_never_retried(self) -> None:
        err = AgentCancelledError("cancelled by SIGTERM (exit=-15)")
        # Zero under the default config...
        assert DEFAULT_RETRY_CONFIG.max_retries_for(err) == 0
        # ...and zero even when the generic budget is a positive 3.
        assert RetryConfig(max_retries=3).max_retries_for(err) == 0

    def test_generic_uses_max_retries(self) -> None:
        cfg = RetryConfig(max_retries=3)
        err = AgentCallError("generic")
        assert cfg.max_retries_for(err) == 3

    def test_delay_increases_exponentially(self) -> None:
        cfg = RetryConfig(base_delay=1.0, backoff_multiplier=2.0, jitter=False)
        assert cfg.delay_for(1) == 1.0
        assert cfg.delay_for(2) == 2.0
        assert cfg.delay_for(3) == 4.0

    def test_delay_capped_at_max(self) -> None:
        cfg = RetryConfig(base_delay=1.0, backoff_multiplier=100.0, max_delay=10.0, jitter=False)
        assert cfg.delay_for(5) == 10.0


# ─────────────────────────────────────────────────────────────────────────────
# call_with_retry
# ─────────────────────────────────────────────────────────────────────────────

class TestCallWithRetry:
    def test_success_on_first_call(self) -> None:
        result = call_with_retry(lambda: "ok", config=_make_config(), _sleep=_no_sleep)
        assert result == "ok"

    def test_retries_on_rate_limit(self) -> None:
        call_count = [0]

        def flaky():
            call_count[0] += 1
            if call_count[0] < 3:
                raise RateLimitError("rate_limit_exceeded")
            return "success"

        result = call_with_retry(flaky, config=_make_config(max_retries=3), _sleep=_no_sleep)
        assert result == "success"
        assert call_count[0] == 3

    def test_admission_denial_raises_original_before_event_or_sleep(self) -> None:
        error = RateLimitError("rate_limit_exceeded")
        order: list[str] = []
        events: list[RetryEvent] = []

        def fail() -> None:
            order.append("attempt")
            raise error

        with pytest.raises(RateLimitError) as excinfo:
            call_with_retry(
                fail,
                config=_make_config(max_retries=1),
                retry_events=events,
                retry_admission=lambda received: order.append("admission") or False,
                _sleep=lambda _delay: order.append("sleep"),
            )

        assert excinfo.value is error
        assert order == ["attempt", "admission"]
        assert events == []

    def test_exhausts_and_raises(self) -> None:
        def always_fail():
            raise RateLimitError("rate_limit_exceeded")

        with pytest.raises(RateLimitError):
            call_with_retry(always_fail, config=_make_config(max_retries=2), _sleep=_no_sleep)

    def test_retries_on_timeout(self) -> None:
        call_count = [0]

        def flaky():
            call_count[0] += 1
            if call_count[0] == 1:
                raise ApiTimeoutError("timed out")
            return "recovered"

        result = call_with_retry(flaky, config=_make_config(max_retries=2), _sleep=_no_sleep)
        assert result == "recovered"

    def test_context_overflow_retries_once(self) -> None:
        """ContextOverflowError: max 1 retry by default."""
        call_count = [0]

        def flaky():
            call_count[0] += 1
            if call_count[0] == 1:
                raise ContextOverflowError("context_length_exceeded")
            return "truncated and ok"

        cfg = RetryConfig(context_overflow_max_retries=1, base_delay=0.0, jitter=False)
        result = call_with_retry(flaky, config=cfg, _sleep=_no_sleep)
        assert result == "truncated and ok"

    def test_context_overflow_exhausted_after_one(self) -> None:
        def always_overflow():
            raise ContextOverflowError("context_length_exceeded")

        cfg = RetryConfig(context_overflow_max_retries=1, base_delay=0.0, jitter=False)
        with pytest.raises(ContextOverflowError):
            call_with_retry(always_overflow, config=cfg, _sleep=_no_sleep)

    def test_system_resource_error_is_not_retried(self) -> None:
        call_count = [0]

        def pty_exhausted():
            call_count[0] += 1
            raise SystemResourceError("pty pool exhausted")

        with pytest.raises(SystemResourceError):
            call_with_retry(pty_exhausted, config=_make_config(max_retries=3), _sleep=_no_sleep)

        assert call_count[0] == 1

    def test_process_killed_retries_exactly_once(self) -> None:
        # A RUNTIME-like config (generic=0) still retries the kill shape once
        # via the explicit knob; second attempt succeeds → the phase continues.
        call_count = [0]

        def killed_then_ok():
            call_count[0] += 1
            if call_count[0] == 1:
                raise classify_from_exit(-9, "")
            return "recovered"

        cfg = RetryConfig(
            max_retries=0,
            process_killed_max_retries=1,
            base_delay=0.0,
            jitter=False,
        )
        result = call_with_retry(killed_then_ok, config=cfg, _sleep=_no_sleep)
        assert result == "recovered"
        assert call_count[0] == 2  # one failure + one successful retry

    def test_cancelled_is_not_retried(self) -> None:
        call_count = [0]

        def cancelled():
            call_count[0] += 1
            raise classify_from_exit(-15, "")

        cfg = RetryConfig(max_retries=3, base_delay=0.0, jitter=False)
        with pytest.raises(AgentCancelledError):
            call_with_retry(cancelled, config=cfg, _sleep=_no_sleep)
        assert call_count[0] == 1  # no retry, even with generic budget 3

    def test_access_error_is_not_retried(self) -> None:
        call_count = [0]
        events: list[RetryEvent] = []

        def access_disabled():
            call_count[0] += 1
            raise AgentAccessError("subscription access disabled")

        with pytest.raises(AgentAccessError):
            call_with_retry(
                access_disabled,
                config=_make_config(max_retries=3),
                retry_events=events,
                _sleep=_no_sleep,
            )

        assert call_count[0] == 1
        assert events[0].error_type == ErrorType.ACCESS
        assert events[0].delay_s == 0.0

    def test_generic_runtime_error_classified_and_retried(self) -> None:
        call_count = [0]

        def flaky():
            call_count[0] += 1
            if call_count[0] < 2:
                raise RuntimeError("Codex CLI failed with exit code 1\nStderr: something")
            return "ok"

        result = call_with_retry(flaky, config=_make_config(max_retries=3), _sleep=_no_sleep)
        assert result == "ok"

    def test_subprocess_timeout_classified(self) -> None:
        def always_timeout():
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=30)

        with pytest.raises(ApiTimeoutError):
            call_with_retry(always_timeout, config=_make_config(max_retries=1), _sleep=_no_sleep)

    def test_retry_events_recorded(self) -> None:
        events: list[RetryEvent] = []
        call_count = [0]

        def flaky():
            call_count[0] += 1
            if call_count[0] < 2:
                raise RateLimitError("rate_limit_exceeded")
            return "ok"

        call_with_retry(
            flaky,
            config=_make_config(max_retries=3),
            retry_events=events,
            _sleep=_no_sleep,
        )
        assert len(events) == 1
        assert events[0].error_type == ErrorType.RATE_LIMIT
        assert events[0].attempt == 1

    def test_retry_events_have_phase(self) -> None:
        events: list[RetryEvent] = []

        def flaky():
            raise RateLimitError("rl")

        with pytest.raises(RateLimitError):
            call_with_retry(
                flaky,
                config=_make_config(max_retries=1),
                phase="implement",
                retry_events=events,
                _sleep=_no_sleep,
            )

        assert events[0].phase == "implement"

    def test_retry_event_as_dict(self) -> None:
        event = RetryEvent(
            attempt=1,
            error_type=ErrorType.RATE_LIMIT,
            message="rate_limit_exceeded",
            delay_s=2.0,
            phase="plan",
        )
        d = event.as_dict()
        assert d["error_type"] == "rate_limit"
        assert d["attempt"] == 1
        assert d["delay_s"] == 2.0
        assert d["phase"] == "plan"


# ─────────────────────────────────────────────────────────────────────────────
# @with_retry decorator
# ─────────────────────────────────────────────────────────────────────────────

class TestWithRetryDecorator:
    def test_decorator_success(self) -> None:
        @with_retry(_make_config())
        def call():
            return "decorated ok"

        assert call() == "decorated ok"

    def test_decorator_retries(self) -> None:
        call_count = [0]

        @with_retry(_make_config(max_retries=3))
        def flaky():
            call_count[0] += 1
            if call_count[0] < 2:
                raise RateLimitError("rl")
            return "done"

        # Note: decorator uses real sleep — not injectable. Use tiny delay.
        cfg = RetryConfig(max_retries=3, base_delay=0.0, jitter=False)

        @with_retry(cfg)
        def flaky2():
            call_count[0] += 1
            if call_count[0] < 4:
                raise RateLimitError("rl")
            return "done"

        call_count[0] = 0
        result = flaky2()
        assert result == "done"


# ─────────────────────────────────────────────────────────────────────────────
# FailingMockProvider
# ─────────────────────────────────────────────────────────────────────────────

class TestFailingMockProvider:
    def test_claude_fails_then_succeeds(self) -> None:
        from agents.runtimes import FailingMockProvider

        provider = FailingMockProvider(fail_times=2, error_type="rate_limit")
        claude = provider.claude("mock")

        # First two calls raise
        with pytest.raises(RateLimitError):
            claude.run("prompt", "/tmp")
        with pytest.raises(RateLimitError):
            claude.run("prompt", "/tmp")
        # Third call succeeds
        result = claude.run("prompt", "/tmp")
        assert result  # non-empty string

    def test_codex_fails_then_succeeds(self) -> None:
        from agents.runtimes import FailingMockProvider

        provider = FailingMockProvider(fail_times=1, error_type="timeout")
        codex = provider.codex("mock")

        with pytest.raises(ApiTimeoutError):
            codex.review_uncommitted("/tmp")
        result = codex.review_uncommitted("/tmp")
        from pipeline.review_parser import parse_review
        assert parse_review(result).approved

    def test_runtime_labels_match_provider_slots(self) -> None:
        from agents.runtimes import FailingMockProvider

        provider = FailingMockProvider()

        assert provider.claude("mock").runtime == "claude"
        assert provider.codex("mock").runtime == "codex"
        assert provider.gemini("mock").runtime == "gemini"

    def test_all_error_types(self) -> None:
        from agents.runtimes import FailingMockProvider

        for error_type in ("rate_limit", "timeout", "context_overflow", "generic"):
            provider = FailingMockProvider(fail_times=1, error_type=error_type)
            claude = provider.claude("mock")
            with pytest.raises(AgentCallError):
                claude.run("test", "/tmp")


# ─────────────────────────────────────────────────────────────────────────────
# Integration: call_with_retry + FailingMockProvider
# ─────────────────────────────────────────────────────────────────────────────

class TestRetryWithFailingProvider:
    def test_rate_limit_recovered(self) -> None:
        from agents.runtimes import FailingMockProvider

        provider = FailingMockProvider(fail_times=2, error_type="rate_limit")
        claude = provider.claude("mock")
        events: list[RetryEvent] = []
        cfg = RetryConfig(
            rate_limit_max_retries=4,
            base_delay=0.0,
            jitter=False,
        )

        result = call_with_retry(
            claude.run,
            "Build the feature",
            "/tmp",
            config=cfg,
            phase="implement",
            retry_events=events,
            _sleep=_no_sleep,
        )
        assert result  # got a response
        assert len(events) == 2
        assert all(e.error_type == ErrorType.RATE_LIMIT for e in events)
        assert events[0].phase == "implement"

    def test_timeout_recovered(self) -> None:
        from agents.runtimes import FailingMockProvider

        provider = FailingMockProvider(fail_times=1, error_type="timeout")
        codex = provider.codex("mock")
        cfg = RetryConfig(timeout_max_retries=2, base_delay=0.0, jitter=False)

        result = call_with_retry(
            codex.review_uncommitted,
            "/tmp",
            config=cfg,
            phase="review_changes",
            _sleep=_no_sleep,
        )
        from pipeline.review_parser import parse_review
        assert parse_review(result).approved

    def test_exhaustion_raises_last_error(self) -> None:
        from agents.runtimes import FailingMockProvider

        provider = FailingMockProvider(fail_times=99, error_type="rate_limit")
        claude = provider.claude("mock")
        cfg = RetryConfig(rate_limit_max_retries=2, base_delay=0.0, jitter=False)

        with pytest.raises(RateLimitError):
            call_with_retry(
                claude.run, "task", "/tmp",
                config=cfg,
                _sleep=_no_sleep,
            )

    def test_structured_error_dict_for_meta_json(self) -> None:
        """Verify RetryEvent.as_dict() produces meta.json-ready structure."""
        from agents.runtimes import FailingMockProvider

        provider = FailingMockProvider(fail_times=1, error_type="rate_limit")
        claude = provider.claude("mock")
        events: list[RetryEvent] = []
        cfg = RetryConfig(rate_limit_max_retries=3, base_delay=0.0, jitter=False)

        call_with_retry(
            claude.run, "task", "/tmp",
            config=cfg,
            phase="plan",
            retry_events=events,
            _sleep=_no_sleep,
        )

        d = events[0].as_dict()
        # All fields required for meta.json
        assert "attempt" in d
        assert "error_type" in d
        assert "message" in d
        assert "delay_s" in d
        assert "phase" in d
        assert "timestamp" in d


# ─────────────────────────────────────────────────────────────────────────────
# sanitized_failure_excerpt — durable, operator-safe failure signature
# ─────────────────────────────────────────────────────────────────────────────

class TestSanitizedFailureExcerpt:
    def test_preserves_unknown_provider_message_from_json_event(self) -> None:
        # The key durability property: a novel provider message that matches NO
        # classified pattern must still survive (so it can later be inspected
        # and turned into a pattern). _readable_error_lines drops it; this does
        # not.
        exc = AgentCallError(
            "Agent call failed: exit=1",
            exit_code=1,
            stderr='{"type":"error","message":"usage limit reached, try again in 5h"}',
        )
        out = sanitized_failure_excerpt(exc)
        assert "usage limit reached, try again in 5h" in out

    def test_keeps_plain_text_lines(self) -> None:
        exc = AgentCallError("boom", exit_code=1, stderr="reconnecting...\nstream gave up")
        out = sanitized_failure_excerpt(exc)
        assert "reconnecting..." in out
        assert "stream gave up" in out

    def test_empty_stderr_falls_back_to_exception_message(self) -> None:
        exc = AgentCallError("Agent call failed: exit=1", exit_code=1, stderr="")
        assert sanitized_failure_excerpt(exc) == "Agent call failed: exit=1"

    def test_does_not_dump_raw_json_without_message_field(self) -> None:
        # A JSON plumbing line with no message/error/detail field (e.g. an init
        # payload carrying credentials) must NOT be dumped raw — sanitary
        # boundary. Only the fallback message is recorded.
        exc = AgentCallError(
            "boom",
            exit_code=1,
            stderr='{"type":"system","apiKey":"sk-secret","session":"abc123"}',
        )
        out = sanitized_failure_excerpt(exc)
        assert "sk-secret" not in out
        assert "abc123" not in out
        assert out == "boom"

    def test_respects_byte_limit(self) -> None:
        exc = AgentCallError("boom", exit_code=1, stderr="x" * 5000)
        out = sanitized_failure_excerpt(exc, limit=120)
        assert len(out) <= 120

    def test_caps_number_of_lines(self) -> None:
        stderr = "\n".join(f"line {i}" for i in range(20))
        exc = AgentCallError("boom", exit_code=1, stderr=stderr)
        out = sanitized_failure_excerpt(exc, max_lines=3)
        assert out.count(" / ") == 2  # 3 lines → 2 joins

    def test_returns_empty_when_nothing_readable(self) -> None:
        exc = AgentCallError("", exit_code=1, stderr="")
        assert sanitized_failure_excerpt(exc) == ""
