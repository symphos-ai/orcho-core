# SPDX-License-Identifier: Apache-2.0
"""Durable classification for recoverable provider/runtime agent failures.

A phase can die because the configured runtime hit a *transient* provider or
local-runtime condition rather than a code/test/review verdict, an operator
halt, or a bad prompt/credential. The taxonomy for those conditions already
lives in :mod:`core.io.retry` as typed subclasses of
:class:`~core.io.retry.AgentCallError`:

* :class:`~core.io.retry.RateLimitError` — HTTP 429 / usage-or-session limit.
* :class:`~core.io.retry.ApiConnectionError` — transport failure reaching the
  model API (refused / DNS / mid-stream disconnect).
* :class:`~core.io.retry.ApiTimeoutError` — request timed out.
* :class:`~core.io.retry.SystemResourceError` — local OS resource blocker
  (e.g. an exhausted PTY pool). Its :class:`~core.io.retry.AgentProcessKilledError`
  subclass (kill-shaped signal death — SIGKILL/SIGSEGV/SIGABRT) is therefore
  automatically included here via ``isinstance`` and projected as recoverable,
  while cancel-shaped :class:`~core.io.retry.AgentCancelledError` (a direct
  ``AgentCallError``, not a ``SystemResourceError``) is excluded.

When one of those escalates past the retry budget the run is terminal, but the
*next* safe operator/captain action is to resume or retry the same phase once
the condition clears — not to switch runtime (that is provider-*access*) and
not to treat the diff/review as rejected. This module turns the already-typed
exception into a stable, provider-neutral ``failure_kind='provider_runtime'``
record for ``session['failure']`` and the ``run.end`` event.

Design contract (see ADR 0118; sanitary boundary from ADR 0101):

* Classification is driven **only** by ``isinstance`` over the typed exception
  set — never by re-parsing strings or provider-branded signatures. The
  provider signatures stay in :mod:`core.io.retry`; this module is neutral.
* :class:`~core.io.retry.AgentAccessError`,
  :class:`~core.io.retry.AgentAuthenticationError`,
  :class:`~core.io.retry.ContextOverflowError` and a bare
  :class:`~core.io.retry.AgentCallError` are deliberately **excluded** — they
  are access / auth / prompt forms, not transient usage/session/transport.
* ``recoverable`` is ``True`` and ``recommended_action`` is the declarative
  ``resume_or_retry_phase`` tag — these are advisory metadata for captain/MCP,
  not a control-flow change. No retry loop or MCP tool is introduced here.
* ``provider_message`` is taken strictly from
  :func:`core.io.retry.sanitized_failure_excerpt`, so no raw JSONL / secrets /
  prompt text reaches an operator-visible or durable field. When the excerpt is
  empty the field is omitted (matching the generic excerpt branch).

Like the rest of ``run_state``, this module does no file IO, emits no events,
and touches no checkpoint — it only builds a plain dict.
"""

from __future__ import annotations

from core.io.retry import (
    ApiConnectionError,
    ApiTimeoutError,
    RateLimitError,
    SystemResourceError,
    sanitized_failure_excerpt,
)

#: Stable ``failure_kind`` discriminator for a recoverable provider/runtime
#: terminal failure (parallel to ``provider_access`` / ``stalled_command``).
PROVIDER_RUNTIME_FAILURE_KIND = "provider_runtime"

#: Recommended-action tag for the durable failure record — declarative only.
RECOMMENDED_ACTION = "resume_or_retry_phase"

#: The exact typed-exception set that classifies to ``provider_runtime``.
#: Sourced from the :mod:`core.io.retry` taxonomy; this module never re-parses
#: provider strings. ``AgentAccessError`` / ``AgentAuthenticationError`` /
#: ``ContextOverflowError`` are intentionally absent.
_PROVIDER_RUNTIME_TYPES: tuple[type[BaseException], ...] = (
    RateLimitError,
    ApiConnectionError,
    ApiTimeoutError,
    SystemResourceError,
)


def is_provider_runtime_failure(exc: Exception) -> bool:
    """True when ``exc`` is a transient provider/runtime agent failure.

    Routing predicate for ``_failure_metadata_for_exception``: a pure
    ``isinstance`` check over the typed taxonomy, so a provider-*access* or
    auth/context-overflow/generic ``AgentCallError`` is excluded.
    """
    return isinstance(exc, _PROVIDER_RUNTIME_TYPES)


def build_provider_runtime_failure(
    exc: Exception,
    *,
    failed_phase: str,
    runtime: str,
    model: str,
) -> dict[str, object]:
    """Provider-neutral durable failure fields for a provider/runtime failure.

    Pure: takes the already-typed exception plus the failed phase's resolved
    runtime/model and returns a plain dict to merge into ``session['failure']``
    and the ``run.end`` event payload. ``provider_message`` is the sanitized
    excerpt and is omitted entirely when nothing readable was captured.
    """
    failure: dict[str, object] = {
        "failure_kind": PROVIDER_RUNTIME_FAILURE_KIND,
        "recoverable": True,
        "recommended_action": RECOMMENDED_ACTION,
        "failed_phase": failed_phase,
        "runtime": runtime,
        "model": model,
    }
    provider_message = sanitized_failure_excerpt(exc)
    if provider_message:
        failure["provider_message"] = provider_message
    return failure


__all__ = [
    "PROVIDER_RUNTIME_FAILURE_KIND",
    "RECOMMENDED_ACTION",
    "build_provider_runtime_failure",
    "is_provider_runtime_failure",
]
