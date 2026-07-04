"""Shared agent-CLI failure detection + retry policy for runtime adapters.

Runtime ``invoke()`` methods must not return an API-client error as if it were
a normal assistant response. Doing so marks the phase ✓ and lets the run walk
on into the next phase against empty/garbage output. This module is the single
place that translates a raw CLI result ``(stdout, returncode, stderr)`` into a
typed :class:`~core.io.retry.AgentCallError`, and the single place that applies
the runtime retry policy on top of an invocation.

Split of responsibility (see ``../../AGENTS.md``): the *protocol* — the error
taxonomy and the retry engine — lives in ``core.io.retry``; this module is the
provider-side *error translation + retries* the workspace rules assign to the
registered runtime. Auth failures keep their dedicated, formatted guidance in
``agents.runtimes.auth``; everything else (transport, rate-limit, timeout, and
otherwise-unexplained non-zero exits) classifies here.

Two failure shapes are handled:

  * ``returncode != 0`` — the CLI itself reported failure. Classified through
    ``classify_from_exit`` (stderr + stdout) so connection/rate-limit/timeout
    signatures map to the right retry budget and a bare non-zero exit becomes a
    generic ``AgentCallError`` that halts at once.
  * ``returncode == 0`` but the model's *own reply* is a transport-error
    message — the CLI's reconnect loop gave up and emitted the error as its
    final reply, either as plain text or as a structured
    ``{"type":"error","message":...}`` stream event. Detection scans ONLY the
    extracted reply text the caller passes as ``reply_text``, never stdout
    plumbing or stderr logs. A success-exit CLI routinely prints unrelated
    operational noise to stderr (e.g. ``failed to record rollout``); scanning
    that would discard a valid model answer and loop forever on retry. The
    model answered — trust the exit code unless the answer itself is the error.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from agents.runtimes.auth import (
    looks_like_auth_failure,
    raise_authentication_error,
)
from core.io.retry import (
    ApiConnectionError,
    RetryConfig,
    call_with_retry,
    classify_from_exit,
)

# Generic CLI failures surface immediately (max_retries=0): an unexplained
# non-zero exit is not something a blind retry fixes, and the user asked for a
# controlled halt rather than a continued run. Only the transient transport
# shapes — connection drops, rate limits, timeouts — get a bounded retry
# before the typed error propagates and the FSM records the halt. A kill-shaped
# process death (SIGKILL/SIGSEGV/SIGABRT, e.g. the OOM killer) is likewise
# transient: it gets one bounded retry via process_killed_max_retries while
# generic exits stay 0. Cancel-shaped death (SIGINT/SIGTERM) is never retried
# by construction (AgentCancelledError pins its budget to 0 under any config).
RUNTIME_RETRY_CONFIG = RetryConfig(
    max_retries=0,
    connection_max_retries=2,
    rate_limit_max_retries=2,
    timeout_max_retries=1,
    context_overflow_max_retries=0,
    process_killed_max_retries=1,
)


def _sleep(seconds: float) -> None:
    """Indirection so tests can patch out backoff sleeps (``_failures._sleep``)."""
    time.sleep(seconds)


# Narrow set of CLI-emitted transport sentinels that signal an API-client
# failure even when the process exits 0 — the CLI reconnect loop gave up and
# emitted the error AS the model's final reply. Matched only against the
# extracted reply text (never stderr/plumbing), and only when the reply *is*
# the error (the first meaningful line starts with a sentinel), not when prose
# merely mentions the phrase. Kept specific on purpose; broad networking words
# live in retry._CONNECTION_PATTERNS and only apply to already-failed runs.
_EXIT0_API_FAILURE_SENTINELS = (
    "api error: unable to connect",
    "unable to connect to api",
    "stream disconnected before completion",
    "failed to lookup address information",
    "nodename nor servname provided",
)

# Leading decoration the extracted reply may carry before the error text:
# markdown fences/quotes/bullets/headers and surrounding quotes.
_REPLY_LEADING_NOISE = "`>*-#\"' \t"


def _sentinel_in(text: str) -> str | None:
    norm = (text or "").lower()
    for sentinel in _EXIT0_API_FAILURE_SENTINELS:
        if sentinel in norm:
            return sentinel
    return None


def _stream_error_event_message(line: str) -> str | None:
    """Return the message of a structured ``{"type":"error", ...}`` JSONL
    event, else ``None``. A structured error event is machine-emitted (not
    model prose), so the CLI's reconnect loop printing one AS its final reply
    is a transport failure even though it starts with ``{``.
    """
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("type") != "error":
        return None
    msg = obj.get("message")
    return msg if isinstance(msg, str) else ""


def _matched_exit0_sentinel(reply_text: str) -> str | None:
    """Return the sentinel an exit-0 reply carries, else ``None``.

    Two error shapes count (a transport error emitted AS the reply), in line
    order — the first content-bearing line decides:

      * a structured ``{"type":"error","message":...}`` JSONL event whose
        message carries a sentinel (machine-emitted, so substring is safe); or
      * a plain-text reply whose first content line *starts with* a sentinel
        (after stripping markdown fences/quotes/bullets).

    A line of JSON plumbing that is not an error event, or pure decoration, is
    skipped. The first real content line that is neither ends the scan — prose
    that merely *mentions* the phrase (e.g. a debugging answer) is not a
    failure.
    """
    for line in (reply_text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("{"):
            err_msg = _stream_error_event_message(stripped)
            if err_msg is None:
                # JSON plumbing that isn't an error event — keep scanning.
                continue
            return _sentinel_in(err_msg)
        norm = stripped.lstrip(_REPLY_LEADING_NOISE).lower()
        if not norm:
            # Pure decoration (e.g. a bare ``` fence) — keep scanning.
            continue
        for sentinel in _EXIT0_API_FAILURE_SENTINELS:
            if norm.startswith(sentinel):
                return sentinel
        # First real content line was not an error — it's a normal answer.
        return None
    return None


def _first_meaningful_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        err_msg = _stream_error_event_message(stripped)
        if err_msg:
            return err_msg[:200]
        deco = stripped.lstrip(_REPLY_LEADING_NOISE)
        if deco and not deco.startswith("{"):
            return deco[:200]
    return ""


def raise_on_runtime_failure(
    *,
    runtime: str,
    model: str,
    cli: str,
    returncode: int,
    stdout: str,
    stderr: str,
    reply_text: str | None = None,
) -> None:
    """Raise a typed :class:`AgentCallError` when the CLI result is a failure.

    ``reply_text`` is the model's effective answer — the text ``invoke()`` is
    about to return (extracted assistant text, or the last-message fallback).
    The exit-0 transport check scans ONLY this, never stdout plumbing or stderr
    logs: a success-exit CLI prints unrelated operational noise to stderr that
    must not discard a valid model answer.

    Returns ``None`` (the caller proceeds to parse output) when the result
    looks healthy. Guardrail-blocked results must be handled by the caller
    *before* calling this — they are an intentional stop, not a failure.
    """
    if returncode != 0 and looks_like_auth_failure(stdout, stderr):
        # Raises AgentAuthenticationError with login guidance (no retry).
        raise_authentication_error(
            runtime=runtime,
            model=model,
            cli=cli,
            exit_code=returncode,
            stdout=stdout,
            stderr=stderr,
        )
    if returncode != 0:
        # Never None for a non-zero code; maps transport/rate-limit/timeout
        # signatures to their retry budget, else a generic AgentCallError.
        raise classify_from_exit(returncode, stderr, stdout)  # type: ignore[misc]
    # exit 0: the CLI reported success. The only exit-0 failure we recognise is
    # the model's *own reply* being a transport-error message. Scan the reply,
    # not stdout/stderr — operational log noise there is unrelated to API
    # reachability and a false match would loop forever on retry.
    sentinel = _matched_exit0_sentinel(reply_text or "")
    if sentinel:
        detail = _first_meaningful_line(reply_text or "") or sentinel
        raise ApiConnectionError(
            f"API unreachable (runtime={runtime}, exit=0): {detail}",
            exit_code=0,
            stderr=stderr,
        )


def run_invoke_with_retry[T](attempt: Callable[[], T], *, runtime: str) -> T:
    """Run a single-invocation thunk under the runtime retry policy.

    ``attempt`` performs one CLI invocation and either returns its result or
    raises a typed :class:`AgentCallError` (via :func:`raise_on_runtime_failure`).
    Transient transport errors retry per :data:`RUNTIME_RETRY_CONFIG`; on
    exhaustion the typed error propagates so the lifecycle FSM converts it into
    a controlled, reasoned halt instead of a silently-continued run.
    """
    return call_with_retry(
        attempt,
        config=RUNTIME_RETRY_CONFIG,
        phase=f"{runtime}.invoke",
        _sleep=_sleep,
    )
