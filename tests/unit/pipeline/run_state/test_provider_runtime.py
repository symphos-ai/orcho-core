# SPDX-License-Identifier: Apache-2.0
"""Recoverable provider/runtime failure classification (ADR 0118).

Pins :mod:`pipeline.run_state.provider_runtime` and its routing through
``run._failure_metadata_for_exception``:

* the typed transient set ({RateLimit, ApiConnection, ApiTimeout,
  SystemResource}) classifies to a stable ``failure_kind='provider_runtime'``
  with ``recoverable=True``, ``recommended_action='resume_or_retry_phase'``, the
  failed phase, and a sanitized ``provider_message``;
* ``AgentAccessError`` keeps ``provider_access`` (precedence guard, since it is
  a subclass of ``AgentCallError``);
* a generic ``AgentCallError``, ``AgentAuthenticationError`` and
  ``ContextOverflowError`` are NOT re-tagged as ``provider_runtime``.
"""

from __future__ import annotations

import pytest

from core.io.retry import (
    AgentAccessError,
    AgentAuthenticationError,
    AgentCallError,
    AgentCancelledError,
    AgentProcessKilledError,
    ApiConnectionError,
    ApiTimeoutError,
    ContextOverflowError,
    RateLimitError,
    SystemResourceError,
    classify_from_exit,
)
from pipeline.project.run import _failure_metadata_for_exception
from pipeline.run_state.provider_runtime import (
    PROVIDER_RUNTIME_FAILURE_KIND,
    RECOMMENDED_ACTION,
    build_provider_runtime_failure,
    is_provider_runtime_failure,
)


def _meta(exc: Exception) -> dict:
    return _failure_metadata_for_exception(exc, failed_phase="implement")


# ── unit: build_provider_runtime_failure ──────────────────────────────────────

def test_build_returns_stable_recoverable_record() -> None:
    exc = RateLimitError(
        "Rate limit exceeded",
        exit_code=1,
        stderr="usage limit reached for this session, try again in 5h",
    )
    failure = build_provider_runtime_failure(
        exc, failed_phase="implement", runtime="claude", model="opus",
    )
    assert failure["failure_kind"] == PROVIDER_RUNTIME_FAILURE_KIND == "provider_runtime"
    assert failure["recoverable"] is True
    assert failure["recommended_action"] == RECOMMENDED_ACTION == "resume_or_retry_phase"
    assert failure["failed_phase"] == "implement"
    assert failure["runtime"] == "claude"
    assert failure["model"] == "opus"
    assert "usage limit reached for this session" in failure["provider_message"]


def test_build_omits_empty_provider_message() -> None:
    # Nothing readable (empty stderr AND empty message) → no provider_message
    # key, mirroring the generic excerpt branch.
    exc = RateLimitError("", exit_code=1, stderr="")
    failure = build_provider_runtime_failure(
        exc, failed_phase="implement", runtime="claude", model="",
    )
    assert "provider_message" not in failure


def test_provider_message_does_not_leak_raw_json_plumbing() -> None:
    exc = ApiConnectionError(
        "boom",
        exit_code=1,
        stderr='{"type":"system","apiKey":"sk-secret","session":"abc123"}',
    )
    failure = build_provider_runtime_failure(
        exc, failed_phase="implement", runtime="claude", model="",
    )
    message = failure.get("provider_message", "")
    assert "sk-secret" not in message
    assert "abc123" not in message


@pytest.mark.parametrize(
    "exc",
    [
        RateLimitError("rate", stderr="429"),
        ApiConnectionError("conn", stderr="connection refused"),
        ApiTimeoutError("timeout", stderr="timed out"),
        SystemResourceError("resource", stderr="resource temporarily unavailable"),
    ],
)
def test_predicate_true_for_typed_transients(exc: Exception) -> None:
    assert is_provider_runtime_failure(exc) is True


def test_kill_signal_is_provider_runtime() -> None:
    # AgentProcessKilledError subclasses SystemResourceError, so the isinstance
    # predicate over _PROVIDER_RUNTIME_TYPES includes it automatically.
    exc = classify_from_exit(-9, "")
    assert isinstance(exc, AgentProcessKilledError)
    assert is_provider_runtime_failure(exc) is True


def test_cancel_signal_is_not_provider_runtime() -> None:
    # AgentCancelledError is a direct AgentCallError subclass (not a
    # SystemResourceError), so it is excluded from the recoverable set.
    exc = classify_from_exit(-15, "")
    assert isinstance(exc, AgentCancelledError)
    assert is_provider_runtime_failure(exc) is False


def test_build_names_signal_for_kill() -> None:
    # provider_message must carry the signal name (from str(exc)), not a bare
    # exit code, so the durable record is diagnosable after logs are gone.
    exc = classify_from_exit(-9, "")
    failure = build_provider_runtime_failure(
        exc, failed_phase="implement", runtime="claude", model="opus",
    )
    assert failure["failure_kind"] == "provider_runtime"
    assert failure["recoverable"] is True
    assert failure["recommended_action"] == RECOMMENDED_ACTION
    assert "SIGKILL" in failure["provider_message"]


@pytest.mark.parametrize(
    "exc",
    [
        AgentAccessError("subscription access disabled"),
        AgentAuthenticationError("bad credentials"),
        ContextOverflowError("context_length_exceeded"),
        AgentCallError("Agent call failed: exit=1"),
        RuntimeError("ordinary bug"),
    ],
)
def test_predicate_false_for_non_transients(exc: Exception) -> None:
    assert is_provider_runtime_failure(exc) is False


# ── routing through _failure_metadata_for_exception ───────────────────────────

@pytest.mark.parametrize(
    ("exc", "needle"),
    [
        (RateLimitError("rate", stderr="usage limit reached, session limit"), "usage limit reached"),
        (ApiConnectionError("conn", stderr="connection refused"), "connection refused"),
        (ApiTimeoutError("timeout", stderr="request timed out"), "request timed out"),
        (SystemResourceError("resource", stderr="exhausted PTY pool"), "exhausted PTY pool"),
    ],
)
def test_routes_typed_transient_to_provider_runtime(exc: Exception, needle: str) -> None:
    meta = _meta(exc)
    assert meta["failure_kind"] == "provider_runtime"
    assert meta["recoverable"] is True
    assert meta["recommended_action"] == "resume_or_retry_phase"
    assert meta["failed_phase"] == "implement"
    assert needle in meta["provider_message"]


def test_routes_kill_signal_to_provider_runtime() -> None:
    meta = _meta(classify_from_exit(-9, ""))
    assert meta["failure_kind"] == "provider_runtime"
    assert meta["recoverable"] is True
    assert meta["recommended_action"] == "resume_or_retry_phase"
    assert "SIGKILL" in meta["provider_message"]


def test_routes_cancel_signal_to_generic_excerpt() -> None:
    # Cancel is not provider_runtime and not recoverable — it takes the same
    # durable stderr_excerpt branch as generic/auth/context-overflow.
    meta = _meta(classify_from_exit(-15, ""))
    assert meta.get("failure_kind") != "provider_runtime"
    assert "recoverable" not in meta
    assert "stderr_excerpt" in meta
    assert "SIGTERM" in meta["stderr_excerpt"]


def test_access_error_is_not_provider_runtime() -> None:
    # AgentAccessError is a subclass of AgentCallError but must keep its richer
    # provider_access projection (precedence guard).
    meta = _meta(AgentAccessError("subscription access disabled", exit_code=1, stderr=""))
    assert meta.get("failure_kind") == "provider_access"
    assert meta.get("failure_kind") != "provider_runtime"


def test_generic_agent_call_error_is_not_provider_runtime() -> None:
    meta = _meta(AgentCallError("Agent call failed: exit=1", exit_code=1, stderr="exit=1"))
    assert meta.get("failure_kind") != "provider_runtime"
    assert "stderr_excerpt" in meta


@pytest.mark.parametrize(
    "exc",
    [
        AgentAuthenticationError("bad credentials", exit_code=1, stderr="invalid api key"),
        ContextOverflowError("context_length_exceeded", exit_code=1, stderr="prompt too long"),
    ],
)
def test_auth_and_context_overflow_are_not_provider_runtime(exc: Exception) -> None:
    # Auth/prompt forms keep the generic excerpt path, never provider_runtime.
    meta = _meta(exc)
    assert meta.get("failure_kind") != "provider_runtime"


def test_non_agent_exception_keeps_empty_meta() -> None:
    assert _meta(RuntimeError("ordinary bug")) == {}
