# SPDX-License-Identifier: Apache-2.0
"""Durable failure-metadata for agent-call failures (ADR 0101 sanitary boundary).

Pins ``run._failure_metadata_for_exception``: a generic ``AgentCallError`` now
persists a sanitized ``stderr_excerpt`` so the captured provider signature
survives the process exit + a later resume (which overwrites the raw run logs).
Access failures keep their full recovery projection; non-agent exceptions keep
the historical empty-meta behaviour.
"""

from __future__ import annotations

from core.io.retry import (
    AgentAccessError,
    AgentCallError,
    RateLimitError,
    classify_from_exit,
)
from pipeline.project.run import _failure_metadata_for_exception


def _meta(exc: Exception) -> dict:
    return _failure_metadata_for_exception(exc, failed_phase="review_changes")


def test_generic_agent_call_error_persists_sanitized_excerpt() -> None:
    exc = AgentCallError(
        "Agent call failed: exit=1",
        exit_code=1,
        stderr='{"type":"error","message":"usage limit reached, try again in 5h"}',
    )
    meta = _meta(exc)
    assert "usage limit reached, try again in 5h" in meta["stderr_excerpt"]


def test_excerpt_does_not_leak_raw_json_plumbing() -> None:
    exc = AgentCallError(
        "boom",
        exit_code=1,
        stderr='{"type":"system","apiKey":"sk-secret","session":"abc123"}',
    )
    meta = _meta(exc)
    excerpt = meta.get("stderr_excerpt", "")
    assert "sk-secret" not in excerpt
    assert "abc123" not in excerpt


def test_typed_transient_error_classifies_provider_runtime() -> None:
    # A typed transient (rate-limit / connection / timeout / resource) now
    # routes to the stable ``provider_runtime`` classification (ADR 0118): the
    # sanitized signature is carried as ``provider_message`` rather than the
    # generic ``stderr_excerpt`` branch.
    exc = RateLimitError("Rate limit exceeded", exit_code=1, stderr="429 too many requests")
    meta = _meta(exc)
    assert meta["failure_kind"] == "provider_runtime"
    assert meta["recoverable"] is True
    assert meta["recommended_action"] == "resume_or_retry_phase"
    assert "429 too many requests" in meta["provider_message"]
    assert "stderr_excerpt" not in meta


def test_kill_signal_classifies_provider_runtime_and_names_signal() -> None:
    # A kill-shaped signal death routes to provider_runtime (via the
    # SystemResourceError subclass) with the signal name in provider_message —
    # not a bare ``exit=-9``.
    meta = _meta(classify_from_exit(-9, ""))
    assert meta["failure_kind"] == "provider_runtime"
    assert meta["recoverable"] is True
    assert "SIGKILL" in meta["provider_message"]
    assert meta["provider_message"] != "exit=-9"


def test_cancel_signal_takes_generic_excerpt_branch() -> None:
    # A cancel-shaped signal death is neither provider_runtime nor recoverable;
    # it falls to the durable stderr_excerpt branch like generic/auth failures.
    meta = _meta(classify_from_exit(-15, ""))
    assert meta.get("failure_kind") != "provider_runtime"
    assert "recoverable" not in meta
    assert "SIGTERM" in meta["stderr_excerpt"]


def test_empty_stderr_omits_excerpt_key() -> None:
    # Nothing readable → no excerpt key (we never store an empty string).
    exc = AgentCallError("", exit_code=1, stderr="")
    assert "stderr_excerpt" not in _meta(exc)


def test_access_error_keeps_recovery_projection_not_excerpt() -> None:
    # AccessError stays on its richer recovery projection (unchanged path).
    exc = AgentAccessError("subscription access disabled", exit_code=1, stderr="")
    meta = _meta(exc)
    assert meta.get("failure_kind") == "provider_access"


def test_non_agent_exception_keeps_empty_meta() -> None:
    assert _meta(RuntimeError("ordinary bug")) == {}
