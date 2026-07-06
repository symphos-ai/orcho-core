"""
core/retry.py — Production-grade retry with exponential backoff.

Design principles:
  * Zero external dependencies (stdlib only).
  * Error-type-aware: different strategies for rate-limit vs timeout vs overflow.
  * Structured logging: every retry event is captured for meta.json.
  * Composable: works as a decorator or via call_with_retry().

Error taxonomy:
  AgentAuthenticationError — invalid or missing CLI credentials
  AgentAccessError   — account/subscription/org policy blocks model access
  RateLimitError     — HTTP 429 / "rate_limit_exceeded" in stderr
  ApiConnectionError — transport failure reaching the model API
                       (connection refused, DNS failure, stream disconnect)
  ApiTimeoutError    — subprocess.TimeoutExpired / "timed out" in stderr
  ContextOverflowError — "context_length_exceeded" / prompt too long
  SystemResourceError — local OS resource blocker, e.g. exhausted PTY pool
  AgentProcessKilledError — subprocess killed by a fatal signal
                       (SIGKILL/SIGSEGV/SIGABRT), classified by exit-code shape;
                       a SystemResourceError subclass, so it inherits the
                       recoverable provider_runtime projection
  AgentCancelledError — subprocess cancelled by SIGINT/SIGTERM, classified by
                       exit-code shape; a *direct* AgentCallError subclass, not
                       a SystemResourceError, so never retried or recoverable
  AgentCallError     — any other non-zero exit / RuntimeError from agent

Recovery strategy:
  AgentAuthenticationError → no retry; user must authenticate the CLI
  AgentAccessError   → no retry; operator must restore access or switch runtime
  RateLimitError     → exponential backoff, max 4 retries
  ApiConnectionError → exponential backoff, max 2 retries, then halt the run
  ApiTimeoutError    → fixed backoff (2s), max 2 retries
  ContextOverflowError → truncate prompt, max 1 retry (no point repeating same)
  SystemResourceError → no retry; operator must clear the local resource blocker
  AgentProcessKilledError → bounded retry (process_killed_max_retries, default 1);
                       a transient kill (OOM/segfault/abort) may clear on resume
  AgentCancelledError → no retry; an intentional interrupt/terminate, not transient
  AgentCallError     → exponential backoff, max 3 retries

Usage:

    from core.io.retry import with_retry, RetryConfig, RateLimitError

    @with_retry()
    def my_agent_call(prompt: str) -> str:
        ...

    # With custom config:
    @with_retry(RetryConfig(max_retries=2, base_delay=1.0))
    def conservative_call() -> str:
        ...

    # Call-site control:
    result = call_with_retry(my_func, args, config=RetryConfig(max_retries=1))
"""

from __future__ import annotations

import json
import signal
import subprocess
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


# ─────────────────────────────────────────────────────────────────────────────
# Error taxonomy
# ─────────────────────────────────────────────────────────────────────────────

class AgentCallError(RuntimeError):
    """Base class for all retryable agent errors."""
    def __init__(self, message: str, *, exit_code: int = -1, stderr: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class AgentAuthenticationError(AgentCallError):
    """The configured CLI runtime rejected or lacks credentials."""


class AgentAccessError(AgentCallError):
    """The configured CLI runtime cannot access the requested provider surface."""


class RateLimitError(AgentCallError):
    """HTTP 429 / rate_limit_exceeded from Anthropic or OpenAI."""


class ApiConnectionError(AgentCallError):
    """Transport failure reaching the model API.

    Covers connection refused, DNS resolution failures, and mid-stream
    disconnects — i.e. the runtime could not complete a request because the
    API was unreachable, not because the prompt or credentials were bad.
    Transient, so retried a few times before the run halts.
    """


class ApiTimeoutError(AgentCallError):
    """subprocess.TimeoutExpired or 'timed out' in stderr."""


class ContextOverflowError(AgentCallError):
    """Prompt exceeds model's context window."""


class SystemResourceError(AgentCallError):
    """The local machine could not allocate a required OS resource."""


class AgentProcessKilledError(SystemResourceError):
    """The agent subprocess was killed by a fatal OS signal.

    A kill-shaped signal death — SIGKILL / SIGSEGV / SIGABRT, seen as a
    negative ``returncode`` (``-9``/``-11``/``-6``) or the shell ``128 + N``
    convention (``137``/``139``/``134``) — is treated as transient local
    pressure (typically the OOM killer or a crashed child) that a bounded
    resume/retry may clear. It subclasses :class:`SystemResourceError` so it is
    automatically included in the recoverable ``provider_runtime`` projection
    via ``isinstance``, while carrying its own bounded retry budget; the base
    :class:`SystemResourceError` (e.g. PTY exhaustion) still gets zero retries.
    """


class AgentCancelledError(AgentCallError):
    """The agent subprocess was cancelled by an interrupt/terminate signal.

    A cancel-shaped signal death — SIGINT / SIGTERM, seen as a negative
    ``returncode`` (``-2``/``-15``) or the shell ``128 + N`` convention
    (``130``/``143``) — is an operator or supervisor cancellation, not a
    transient provider/runtime condition. It is a *direct* subclass of
    :class:`AgentCallError` (deliberately NOT :class:`SystemResourceError`), so
    it is never retried and never projected as a recoverable ``provider_runtime``
    failure.
    """


# ─────────────────────────────────────────────────────────────────────────────
# Error classifier
# ─────────────────────────────────────────────────────────────────────────────

# Patterns that indicate rate limiting
_RATE_LIMIT_PATTERNS = (
    "rate_limit_exceeded",
    "rate limit",
    "429",
    "too many requests",
    "retry after",
    "quota exceeded",
)

# Patterns that indicate missing or invalid CLI credentials.
_AUTH_PATTERNS = (
    "failed to authenticate",
    "invalid authentication credentials",
    "authentication failed",
    "not authenticated",
    "not logged in",
    "login required",
    "invalid api key",
    "api key is invalid",
    "missing api key",
    "unauthorized",
)

# Patterns that indicate valid credentials/account context, but no access to
# the requested runtime surface. These are terminal until the operator restores
# provider access or switches runtime/model; blind retry does not help.
_ACCESS_PATTERNS = (
    "subscription access",
    "subscription required",
    "subscription has expired",
    "subscription expired",
    "disabled claude subscription access",
    "claude subscription access",
    "ask your admin to enable access",
    "organization has disabled",
    "use an anthropic api key instead",
    "not available on your plan",
    "plan does not include",
    "account does not have access",
    "access to this model is disabled",
    "access disabled",
)

# Patterns that indicate context overflow
_CONTEXT_OVERFLOW_PATTERNS = (
    "context_length_exceeded",
    "context length",
    "maximum context",
    "too many tokens",
    "prompt is too long",
    "reduce the length",
)

# Patterns that indicate timeout
_TIMEOUT_PATTERNS = (
    "timed out",
    "timeout",
    "request timeout",
    "deadline exceeded",
)

# Patterns that indicate a transport failure reaching the model API.
# These are CLI/SDK-emitted networking errors, distinct from auth (bad
# credentials), rate-limit (429), and timeout (slow but reachable). A run
# cannot make progress through them, so they classify to ApiConnectionError
# and halt the run after a few transient retries.
_CONNECTION_PATTERNS = (
    "unable to connect to api",
    "connection refused",
    "econnrefused",
    "connection reset",
    "connection aborted",
    "connection error",
    "could not connect",
    "failed to connect",
    "stream disconnected",
    "stream error",
    "failed to lookup address information",
    "nodename nor servname provided",
    "temporary failure in name resolution",
    "name or service not known",
    "network is unreachable",
    "network error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "reconnecting",
)

_SYSTEM_RESOURCE_PATTERNS = (
    "orcho_system_pty_exhausted",
    "pty pool exhausted",
    "out of pty devices",
    "could not allocate a pseudo-terminal",
)


def classify_error(
    exc: Exception,
    *,
    stderr: str = "",
    stdout: str = "",
) -> AgentCallError:
    """Convert a raw exception or error output into a typed AgentCallError.

    Checks stderr + stdout text for known error signatures. Falls back to
    generic AgentCallError for unknown non-zero exits.
    """
    combined = (str(exc) + " " + stderr + " " + stdout).lower()

    if isinstance(exc, subprocess.TimeoutExpired):
        return ApiTimeoutError(
            f"Agent call timed out: {exc}",
            exit_code=-1,
            stderr=stderr,
        )

    if isinstance(exc, AgentAuthenticationError):
        return exc

    for pattern in _AUTH_PATTERNS:
        if pattern in combined:
            return AgentAuthenticationError(
                f"Authentication failed: {stderr[:200] or str(exc)[:200]}",
                exit_code=getattr(exc, "returncode", -1),
                stderr=stderr,
            )

    for pattern in _ACCESS_PATTERNS:
        if pattern in combined:
            return AgentAccessError(
                f"Provider access unavailable: "
                f"{_provider_access_detail(stderr=stderr, stdout=stdout, exc=exc)}",
                exit_code=getattr(exc, "returncode", -1),
                stderr=stderr,
            )

    for pattern in _RATE_LIMIT_PATTERNS:
        if pattern in combined:
            return RateLimitError(
                f"Rate limit exceeded: {stderr[:200] or str(exc)[:200]}",
                exit_code=getattr(exc, "returncode", -1),
                stderr=stderr,
            )

    for pattern in _CONNECTION_PATTERNS:
        if pattern in combined:
            return ApiConnectionError(
                f"API unreachable: {stderr[:200] or stdout[:200] or str(exc)[:200]}",
                exit_code=getattr(exc, "returncode", -1),
                stderr=stderr,
            )

    for pattern in _SYSTEM_RESOURCE_PATTERNS:
        if pattern in combined:
            return SystemResourceError(
                f"System resource exhausted: "
                f"{stderr[:300] or stdout[:300] or str(exc)[:300]}",
                exit_code=getattr(exc, "returncode", -1),
                stderr=stderr,
            )

    for pattern in _CONTEXT_OVERFLOW_PATTERNS:
        if pattern in combined:
            return ContextOverflowError(
                f"Context overflow: {stderr[:200] or str(exc)[:200]}",
                exit_code=getattr(exc, "returncode", -1),
                stderr=stderr,
            )

    for pattern in _TIMEOUT_PATTERNS:
        if pattern in combined:
            return ApiTimeoutError(
                f"Agent call timed out: {stderr[:200] or str(exc)[:200]}",
                exit_code=getattr(exc, "returncode", -1),
                stderr=stderr,
            )

    # Unrecognized failure: no known-transient signature matched. By policy
    # this bucket is NOT auto-retried (RUNTIME_RETRY_CONFIG pins generic
    # max_retries=0) — an unclassified non-zero exit is usually deterministic
    # (bad prompt, CLI misuse, a real error), so repeating it just burns
    # tokens. That makes the MESSAGE the whole remediation surface, so lead
    # with an actionable next step; the terminal FAILED banner only shows the
    # first line, so the guidance must come before the raw detail (which is
    # preserved for --output debug and the structured failure record).
    return AgentCallError(
        "Agent call failed with an unrecognized error, so it was not retried "
        "(only transient network / rate-limit / timeout failures auto-retry). "
        "Re-run with --output debug for the full agent CLI output; if it looks "
        f"transient, re-run the pipeline. Detail: {str(exc)[:300]}",
        exit_code=getattr(exc, "returncode", -1),
        stderr=stderr,
    )


# Signal numbers whose process-death *shape* is meaningful, kept as raw ints so
# classification stays provider- and OS-neutral (and does not depend on Windows
# lacking e.g. ``signal.SIGKILL`` as an attribute).
_SIG_ABRT = 6    # SIGABRT — abort()
_SIG_KILL = 9    # SIGKILL — OOM killer / forced kill
_SIG_SEGV = 11   # SIGSEGV — segfault
_SIG_INT = 2     # SIGINT — interrupt (Ctrl-C)
_SIG_TERM = 15   # SIGTERM — graceful termination request

# Kill-shaped fatal signals → transient local pressure, bounded retry, and a
# recoverable provider_runtime projection (via AgentProcessKilledError).
_KILL_SIGNALS = frozenset({_SIG_KILL, _SIG_SEGV, _SIG_ABRT})
# Cancel-shaped signals → intentional interrupt/terminate, never retried,
# never recoverable (via AgentCancelledError).
_CANCEL_SIGNALS = frozenset({_SIG_INT, _SIG_TERM})


def _signal_name(signum: int) -> str:
    """Return the canonical signal name (e.g. ``SIGKILL``) with a safe fallback.

    ``signal.Signals`` only enumerates signals valid on the current platform, so
    an unknown number raises ``ValueError`` — fall back to a stable label rather
    than propagate the platform difference into the error message.
    """
    try:
        return signal.Signals(signum).name
    except (ValueError, KeyError):
        return f"signal {signum}"


def classify_signal_exit(exit_code: int, *, stderr: str = "") -> AgentCallError | None:
    """Type a process-death exit code purely by its signal *shape*.

    Provider-neutral: only the form of the exit code decides, never any string
    pattern. A negative ``returncode`` of ``-N`` (Python's subprocess
    convention) and the shell ``128 + N`` convention (``137`` == SIGKILL,
    ``143`` == SIGTERM, …) both normalise to the same signal ``N``.

    Returns:
      * :class:`AgentProcessKilledError` for a kill-shaped fatal signal
        (SIGKILL / SIGSEGV / SIGABRT).
      * :class:`AgentCancelledError` for a cancel-shaped signal
        (SIGINT / SIGTERM).
      * ``None`` when the code is not a recognised signal death — the caller
        keeps the generic :class:`AgentCallError`.

    The message names the signal (e.g. ``agent process killed by SIGKILL
    (exit=-9)``) so the durable failure excerpt carries the cause, not a bare
    ``exit=-9``.
    """
    if exit_code < 0:
        signum = -exit_code
    elif 129 <= exit_code <= 192:
        signum = exit_code - 128
    else:
        return None

    name = _signal_name(signum)
    if signum in _KILL_SIGNALS:
        return AgentProcessKilledError(
            f"agent process killed by {name} (exit={exit_code})",
            exit_code=exit_code,
            stderr=stderr,
        )
    if signum in _CANCEL_SIGNALS:
        return AgentCancelledError(
            f"agent process cancelled by {name} (exit={exit_code})",
            exit_code=exit_code,
            stderr=stderr,
        )
    return None


def classify_from_exit(exit_code: int, stderr: str, stdout: str = "") -> AgentCallError | None:
    """Classify a non-zero exit code without a Python exception.

    Returns None if exit_code == 0 (success).

    The text-based :func:`classify_error` taxonomy runs first, so any recognised
    rate-limit / timeout / connection / access / overflow signature keeps
    priority. Only when it falls through to a *bare* generic
    :class:`AgentCallError` (``type(...) is AgentCallError`` — nothing matched)
    does the exit code's *shape* get a chance: a kill/cancel signal death is
    typed by :func:`classify_signal_exit`. Any other non-zero exit stays
    generic. ``classify_error`` itself is left untouched — for a real exception
    ``returncode`` defaults to the ``-1`` sentinel, which must not be read as a
    signal death.
    """
    if exit_code == 0:
        return None
    dummy = RuntimeError(f"exit={exit_code}")
    dummy.returncode = exit_code  # type: ignore[attr-defined]
    classified = classify_error(dummy, stderr=stderr, stdout=stdout)
    if type(classified) is AgentCallError:
        signal_error = classify_signal_exit(exit_code, stderr=stderr)
        if signal_error is not None:
            return signal_error
    return classified


def provider_access_detail(exc: Exception) -> str:
    """Operator-facing, sanitized detail for a provider-access failure.

    Public entry point onto the same JSONL-stripping channel used at
    classification time. Routes the exception message and any captured
    ``stderr`` through :func:`_provider_access_detail`, so no raw provider
    init payload / JSON event reaches an operator-visible field — the failure
    record's ``error``, the ``run.end`` error, or the recovery projection.
    """
    return _provider_access_detail(
        stderr=getattr(exc, "stderr", "") or "",
        stdout="",
        exc=exc,
    )


def sanitized_failure_excerpt(
    exc: Exception, *, limit: int = 500, max_lines: int = 4
) -> str:
    """Operator-safe, durable excerpt of an agent call's raw failure output.

    Generic sibling to :func:`provider_access_detail`: routes the exception's
    captured ``stderr`` (with the exception message as fallback) through the
    same JSONL-stripping channel (:func:`_readable_error_lines`), so no raw
    provider init payload / JSON event reaches an operator-visible or durable
    field. Unlike :func:`provider_access_detail` it appends NO access-recovery
    next-step — it is the neutral diagnostic signature persisted for *any*
    agent-call failure (rate-limit / timeout / connection / generic), so the
    captured failure shape survives the process exit and a later resume, where
    the raw run logs are overwritten. Returns ``""`` when nothing readable was
    captured (caller then stores no excerpt).
    """
    stderr = getattr(exc, "stderr", "") or ""
    lines = _failure_excerpt_lines(stderr, fallback=str(exc))
    if not lines:
        return ""
    return _compact_line(" / ".join(lines[:max_lines]), limit=limit)


def _failure_excerpt_lines(stderr: str, *, fallback: str) -> list[str]:
    """Readable failure lines that PRESERVE unknown provider error messages.

    Differs from :func:`_readable_error_lines` (which keeps only lines already
    matching a classified pattern, so a novel provider message is dropped):
    here a structured ``{"message"/"error"/"detail": ...}`` JSONL event has that
    named field extracted directly — machine-emitted, so the field is safe and
    NOT pattern-gated — while any other ``{`` plumbing line is dropped rather
    than dumped raw (ADR 0101 sanitary boundary). Plain-text lines are kept
    compacted. ``fallback`` (the exception message) is used only when nothing
    readable was captured, so a bare ``exit=N`` still records something.
    """
    out: list[str] = []
    for raw in (stderr or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("{"):
            msg = _json_event_message(line)
            if msg:
                out.append(_compact_line(msg))
            continue
        out.append(_compact_line(line))
    out = [line for line in out if line]
    if not out:
        fb = _compact_line(fallback or "")
        if fb:
            out.append(fb)
    return out


def _json_event_message(line: str) -> str:
    """Return the operator-facing message field of a JSONL event, else ``""``.

    Only the named ``message`` / ``error`` / ``detail`` string field is
    returned — never the raw JSON body — so no provider init payload or
    credential material leaks into the durable excerpt.
    """
    try:
        obj = json.loads(line)
    except (TypeError, ValueError):
        return ""
    if not isinstance(obj, dict):
        return ""
    for key in ("message", "error", "detail"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _provider_access_detail(
    *,
    stderr: str,
    stdout: str,
    exc: Exception,
) -> str:
    """Return a compact operator-facing detail for provider access failures."""
    fallback = str(exc)
    lines = _readable_error_lines(stderr, stdout, fallback)
    access_lines = [
        line for line in lines if _contains_any(line.lower(), _ACCESS_PATTERNS)
    ]
    if access_lines:
        return _with_access_next_step(access_lines[0])
    if lines:
        return _with_access_next_step(lines[0])
    return _with_access_next_step("provider access is unavailable")


def _readable_error_lines(*parts: str) -> list[str]:
    """Extract human-readable error lines, skipping provider JSONL plumbing."""
    lines: list[str] = []
    for part in parts:
        for raw_line in (part or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("{"):
                extracted = _extract_json_error_text(line)
                lines.extend(extracted)
                continue
            lines.append(line)
    return [_compact_line(line) for line in lines if _compact_line(line)]


def _extract_json_error_text(line: str) -> list[str]:
    try:
        obj = json.loads(line)
    except (TypeError, ValueError):
        return []
    out: list[str] = []
    _collect_json_strings(obj, out)
    return [
        _compact_line(text)
        for text in out
        if _looks_like_human_error_text(text)
    ]


def _collect_json_strings(node: object, out: list[str]) -> None:
    if isinstance(node, str):
        out.append(node)
        return
    if isinstance(node, list):
        for item in node:
            _collect_json_strings(item, out)
        return
    if not isinstance(node, dict):
        return
    for key in ("message", "error", "text", "content"):
        if key in node:
            _collect_json_strings(node[key], out)


def _looks_like_human_error_text(text: str) -> bool:
    norm = text.strip().lower()
    if not norm:
        return False
    return _contains_any(norm, (*_ACCESS_PATTERNS, *_AUTH_PATTERNS, *_RATE_LIMIT_PATTERNS))


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern in text for pattern in patterns)


def _compact_line(text: str, *, limit: int = 300) -> str:
    compact = " ".join(text.strip().split())
    return compact[:limit]


def _with_access_next_step(detail: str) -> str:
    detail = _compact_line(detail)
    next_step = (
        "Restore provider access, configure an API key, or switch this "
        "phase/runtime to another available provider."
    )
    if not detail:
        return next_step
    if detail.endswith("."):
        return f"{detail} {next_step}"
    return f"{detail}. {next_step}"


# ─────────────────────────────────────────────────────────────────────────────
# Retry event (for structured logging)
# ─────────────────────────────────────────────────────────────────────────────

class ErrorType(str, Enum):  # noqa: UP042  # StrEnum changes __str__; emitted into structured retry events, keep value-only repr stable
    AUTHENTICATION = "authentication"
    ACCESS = "access"
    RATE_LIMIT = "rate_limit"
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    CONTEXT_OVERFLOW = "context_overflow"
    SYSTEM_RESOURCE = "system_resource"
    AGENT_CALL = "agent_call"
    UNKNOWN = "unknown"


@dataclass
class RetryEvent:
    """One retry attempt — captured for meta.json / progress.log."""

    attempt: int           # 1-based attempt number
    error_type: ErrorType
    message: str
    delay_s: float         # seconds waited before this retry
    phase: str = ""        # e.g. "plan", "build", "review"
    timestamp: str = ""    # ISO timestamp of the event

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "error_type": self.error_type.value,
            "message": self.message,
            "delay_s": round(self.delay_s, 2),
            "phase": self.phase,
            "timestamp": self.timestamp,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Retry configuration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RetryConfig:
    """Per-error-type retry policy.

    Attributes
    ----------
    max_retries : int
        Maximum number of retry attempts (0 = no retries, raise immediately).
    base_delay : float
        Base delay in seconds before first retry.
    backoff_multiplier : float
        Each retry multiplies the delay by this factor.
    max_delay : float
        Cap on delay (prevents runaway backoff).
    rate_limit_max_retries : int
        Override max_retries specifically for RateLimitError.
    connection_max_retries : int
        Override max_retries specifically for ApiConnectionError.
    timeout_max_retries : int
        Override max_retries specifically for ApiTimeoutError.
    context_overflow_max_retries : int
        Override max_retries for ContextOverflowError (usually 0 or 1).
    process_killed_max_retries : int
        Override max_retries for AgentProcessKilledError (kill-shaped signal
        death). Bounded on purpose (default 1): a transient kill may clear on
        one resume, but repeating indefinitely would just re-trigger the same
        OOM/crash. Only affects the kill subclass — the base SystemResourceError
        (e.g. PTY exhaustion) still gets 0.
    jitter : bool
        Add ±10% random jitter to delays to avoid thundering herd.
    """

    max_retries: int = 3
    base_delay: float = 1.0
    backoff_multiplier: float = 2.0
    max_delay: float = 60.0
    rate_limit_max_retries: int = 4
    connection_max_retries: int = 2
    timeout_max_retries: int = 2
    context_overflow_max_retries: int = 1
    process_killed_max_retries: int = 1
    jitter: bool = True

    def max_retries_for(self, error: AgentCallError) -> int:
        """Return the effective max_retries for this error type."""
        if isinstance(error, AgentAuthenticationError):
            return 0
        if isinstance(error, AgentAccessError):
            return 0
        if isinstance(error, AgentCancelledError):
            # Cancel-shaped signal death (SIGINT/SIGTERM) — an intentional
            # interrupt/terminate, never retried. Placed alongside auth/access
            # (above the generic fall-through) so it stays 0 under ANY config,
            # including DEFAULT_RETRY_CONFIG where the generic budget is 3.
            return 0
        if isinstance(error, RateLimitError):
            return self.rate_limit_max_retries
        # NB: ApiConnectionError is a subclass-sibling of ApiTimeoutError,
        # both extend AgentCallError directly, so order here is by identity
        # not inheritance — check connection before the generic fallthrough.
        if isinstance(error, ApiConnectionError):
            return self.connection_max_retries
        if isinstance(error, ApiTimeoutError):
            return self.timeout_max_retries
        if isinstance(error, ContextOverflowError):
            return self.context_overflow_max_retries
        if isinstance(error, AgentProcessKilledError):
            # Kill-shaped signal death (OOM/segfault/abort) — bounded retry.
            # MUST precede the SystemResourceError branch: it is a subclass, and
            # the base branch below pins PTY-style resource errors at 0.
            return self.process_killed_max_retries
        if isinstance(error, SystemResourceError):
            return 0
        return self.max_retries

    def delay_for(self, attempt: int) -> float:
        """Return delay in seconds for the N-th retry attempt (1-based)."""
        import random
        raw = min(self.base_delay * (self.backoff_multiplier ** (attempt - 1)), self.max_delay)
        if self.jitter:
            raw *= random.uniform(0.9, 1.1)
        return raw


# Sensible production default — fast in tests (base_delay overridden there)
DEFAULT_RETRY_CONFIG = RetryConfig()


# ─────────────────────────────────────────────────────────────────────────────
# Core retry logic
# ─────────────────────────────────────────────────────────────────────────────

def call_with_retry(
    func: Callable[..., Any],
    *args: Any,
    config: RetryConfig = DEFAULT_RETRY_CONFIG,
    phase: str = "",
    retry_events: list[RetryEvent] | None = None,
    _sleep: Callable[[float], None] = time.sleep,  # injectable for tests
    **kwargs: Any,
) -> Any:
    """Call *func* with retry + exponential backoff.

    Parameters
    ----------
    func : callable
        The agent call to retry.
    *args : Any
        Positional arguments passed to func.
    config : RetryConfig
        Retry policy.
    phase : str
        Pipeline phase name — embedded in RetryEvent for structured logging.
    retry_events : list[RetryEvent] | None
        If provided, RetryEvent records are appended here (for meta.json).
    _sleep : callable
        Overridable sleep function (swap for no-op in unit tests).
    **kwargs : Any
        Keyword arguments passed to func.

    Raises
    ------
    AgentCallError
        When all retries are exhausted.
    """

    # Per-error retry budget: the loop runs until the *caught* error's own
    # ``max_retries_for`` is exhausted (``_record_and_maybe_raise`` raises at
    # that point). Bounding by the generic budget would cap every error type
    # at the generic count — wrong when, e.g., generic=0 (surface plain CLI
    # failures at once) but connection=2 (retry transient transport errors).
    attempt = 0
    while True:
        attempt += 1
        try:
            return func(*args, **kwargs)

        except ContextOverflowError as exc:
            # Context overflow can't be fixed by repeating — give up after 1
            _record_and_maybe_raise(
                exc, attempt, config.max_retries_for(exc),
                config, phase, retry_events, _sleep,
            )

        except AgentCallError as exc:
            _record_and_maybe_raise(
                exc, attempt, config.max_retries_for(exc),
                config, phase, retry_events, _sleep,
            )

        except (subprocess.TimeoutExpired, TimeoutError) as exc:
            typed = ApiTimeoutError(str(exc), exit_code=-1)
            _record_and_maybe_raise(
                typed, attempt, config.max_retries_for(typed),
                config, phase, retry_events, _sleep,
            )

        except RuntimeError as exc:
            typed_exc = classify_error(exc)
            _record_and_maybe_raise(
                typed_exc, attempt, config.max_retries_for(typed_exc),
                config, phase, retry_events, _sleep,
            )


def _record_and_maybe_raise(
    exc: AgentCallError,
    attempt: int,
    max_retries: int,
    config: RetryConfig,
    phase: str,
    retry_events: list[RetryEvent] | None,
    _sleep: Callable[[float], None],
) -> None:
    """Log the retry event. Sleep before next attempt. Raise if exhausted."""
    from datetime import datetime

    error_type = _error_type_for(exc)
    delay = config.delay_for(attempt) if attempt <= max_retries else 0.0

    event = RetryEvent(
        attempt=attempt,
        error_type=error_type,
        message=str(exc)[:300],
        delay_s=delay,
        phase=phase,
        timestamp=datetime.now().isoformat(),
    )
    if retry_events is not None:
        retry_events.append(event)

    if attempt > max_retries:
        # max_retries == 0 means this error type is never retried (e.g. auth,
        # or a generic CLI failure under the runtime policy). No retry was
        # attempted, so don't emit misleading "Retry 1/0 exhausted" chatter —
        # let the caller / run boundary render the halt with its cause.
        if max_retries > 0:
            _print_retry(
                f"  ✗ Retry {attempt}/{max_retries} exhausted "
                f"[{error_type.value}]: {str(exc)[:120]}"
            )
        raise exc

    _print_retry(
        f"  ⟳ Retry {attempt}/{max_retries} [{error_type.value}]: {str(exc)[:120]} "
        f"— waiting {delay:.1f}s…"
    )
    if delay > 0:
        _sleep(delay)


def _error_type_for(exc: AgentCallError) -> ErrorType:
    if isinstance(exc, AgentAuthenticationError):
        return ErrorType.AUTHENTICATION
    if isinstance(exc, AgentAccessError):
        return ErrorType.ACCESS
    if isinstance(exc, RateLimitError):
        return ErrorType.RATE_LIMIT
    if isinstance(exc, ApiConnectionError):
        return ErrorType.CONNECTION
    if isinstance(exc, ApiTimeoutError):
        return ErrorType.TIMEOUT
    if isinstance(exc, ContextOverflowError):
        return ErrorType.CONTEXT_OVERFLOW
    if isinstance(exc, SystemResourceError):
        return ErrorType.SYSTEM_RESOURCE
    return ErrorType.AGENT_CALL


def _print_retry(msg: str) -> None:
    """Print retry message — uses warnings so tests can capture it."""
    print(msg)
    warnings.warn(msg, stacklevel=4)


# ─────────────────────────────────────────────────────────────────────────────
# Decorator
# ─────────────────────────────────────────────────────────────────────────────

def with_retry(
    config: RetryConfig | None = None,
    *,
    phase: str = "",
) -> Callable[[F], F]:
    """Decorator that wraps a function with retry logic.

    Usage:
        @with_retry()
        def call_api(prompt: str) -> str: ...

        @with_retry(RetryConfig(max_retries=2), phase="plan")
        def plan_step() -> str: ...
    """
    _config = config or DEFAULT_RETRY_CONFIG

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return call_with_retry(
                func, *args,
                config=_config,
                phase=phase or func.__name__,
                **kwargs,
            )
        return wrapper  # type: ignore[return-value]

    return decorator
