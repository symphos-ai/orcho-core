# SPDX-License-Identifier: Apache-2.0
"""Transient per-invocation usage normalizer.

Focused observability sibling of :mod:`pipeline.observability.context_growth`
and friends. Where those modules describe durable, persisted shapes, this
module owns a purely **transient** container: :class:`AgentInvocationOutcome`
normalizes the ``last_*`` usage attributes a runtime stamps on itself right
after ``agent.invoke`` into one stable, provider-agnostic view that the live
card (and only the live card, for now) reads.

The object is never written to ``session.json``, ``metrics.json``, evidence,
or any public artifact schema. It exists only for the duration of a single
invocation render and carries no policy methods — just normalized numbers.

Per-runtime read semantics (all fields read via ``getattr`` with safe
defaults; no ``last_*`` attribute is renamed, mutated, or required):

- **Claude** exposes ``last_tokens_in`` / ``last_tokens_out`` and the cache
  split, but no ``last_tokens_total`` and no ``last_tokens_out_reasoning``.
  ``tokens_total`` is therefore derived as ``tokens_in + tokens_out`` when
  both are known.
- **Codex** exposes a provider ``last_tokens_total`` and reasoning tokens,
  but no ``last_tokens_in_cache_create``. Legacy total-only parses surface
  as ``runtime_partial``.
- **Gemini** exposes a provider ``last_tokens_total`` and a cache-read
  count. Note: Gemini's cached tokens are a **subset** of input, not an
  additive component — they are read as-is and ``tokens_in`` is never
  recomputed from the cache split.
- A bare mock agent has none of these attributes; every numeric field
  resolves to ``None`` (or ``0`` for ``tool_calls``) and ``usage_source``
  is ``'estimate'``.

This module imports nothing from ``session_invoke``: the dependency is
strictly one-directional (the invoke boundary depends on this normalizer,
never the reverse).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.observability.metrics import estimate_tokens


@dataclass(frozen=True, slots=True)
class AgentInvocationOutcome:
    """Normalized, transient view of one ``agent.invoke`` usage report.

    A pure container: it holds the provider token/cost numbers a runtime
    stamped on itself, normalized across providers, plus a wire-text
    estimate and a derived runtime-overhead figure. It carries no policy
    methods and is never persisted.
    """

    runtime: str
    """Runtime identifier as passed in (e.g. ``"claude"``)."""
    model: str
    """Model identifier as passed in."""
    tokens_in: int | None
    """Provider input tokens, or ``None`` when not reported."""
    tokens_in_fresh: int | None
    """Non-cached portion of input tokens, or ``None``."""
    tokens_in_cache_read: int | None
    """Cache-read input tokens, or ``None``. For Gemini this is a subset
    of input, not additive."""
    tokens_in_cache_create: int | None
    """Cache-creation input tokens, or ``None`` (absent for Codex/Gemini)."""
    tokens_out: int | None
    """Provider output tokens, or ``None`` when not reported."""
    tokens_out_reasoning: int | None
    """Reasoning output tokens, or ``None`` (absent for Claude)."""
    tokens_total: int | None
    """Provider total tokens, or for Claude a derived ``in + out``."""
    tool_calls: int
    """Tool/function invocation count the provider reported (``0`` default)."""
    cost_usd_equivalent: float | None
    """Provider cost in USD-equivalent, or ``None``."""
    tokens_exact: bool
    """True when at least one provider token field was present."""
    usage_source: str
    """One of ``'runtime_reported'`` | ``'runtime_partial'`` | ``'estimate'``."""
    wire_tokens_estimate: int | None
    """Byte-heuristic estimate of the wire text (``0`` for empty input)."""
    runtime_overhead_tokens: int | None
    """``tokens_in - wire_tokens_estimate`` when both known and non-negative;
    else ``None``."""


def build_invocation_outcome(
    *,
    agent: Any,
    runtime_id: str,
    model: str,
    wire_text: str,
) -> AgentInvocationOutcome:
    """Normalize an agent's post-invoke ``last_*`` usage into an outcome.

    Reads only existing ``last_*`` attributes via ``getattr`` with safe
    defaults; no attribute is renamed, removed, or assumed present. A mock
    agent with no usage attributes yields an all-``None`` outcome with
    ``usage_source='estimate'`` and ``tokens_exact=False``.
    """
    tokens_in = getattr(agent, "last_tokens_in", None)
    tokens_in_fresh = getattr(agent, "last_tokens_in_fresh", None)
    tokens_in_cache_read = getattr(agent, "last_tokens_in_cache_read", None)
    tokens_in_cache_create = getattr(agent, "last_tokens_in_cache_create", None)
    tokens_out = getattr(agent, "last_tokens_out", None)
    tokens_out_reasoning = getattr(agent, "last_tokens_out_reasoning", None)
    cost_usd_equivalent = getattr(agent, "last_cost_usd", None)
    tool_calls = int(getattr(agent, "last_tool_use_count", 0) or 0)

    # tokens_total: provider value when present, else derive in + out
    # (Claude has no provider total; Codex/Gemini report one directly).
    tokens_total = getattr(agent, "last_tokens_total", None)
    if tokens_total is None and tokens_in is not None and tokens_out is not None:
        tokens_total = tokens_in + tokens_out

    # No model arg: keep the number identical to the live card's current
    # byte-heuristic estimate. Empty wire_text yields 0.
    wire_tokens_estimate = estimate_tokens(wire_text)

    # runtime_overhead_tokens: only meaningful when both numbers exist. On
    # heavily-cached resume / delta renders the provider input can fall below
    # the estimate, so a negative "overhead" is meaningless → collapse to None.
    runtime_overhead_tokens: int | None
    if tokens_in is not None and wire_tokens_estimate is not None:
        overhead = tokens_in - wire_tokens_estimate
        runtime_overhead_tokens = overhead if overhead >= 0 else None
    else:
        runtime_overhead_tokens = None

    if tokens_in is None and tokens_out is None and tokens_total is None:
        usage_source = "estimate"
        tokens_exact = False
    elif tokens_in is not None and tokens_out is not None:
        usage_source = "runtime_reported"
        tokens_exact = True
    else:
        usage_source = "runtime_partial"
        tokens_exact = True

    return AgentInvocationOutcome(
        runtime=runtime_id,
        model=model,
        tokens_in=tokens_in,
        tokens_in_fresh=tokens_in_fresh,
        tokens_in_cache_read=tokens_in_cache_read,
        tokens_in_cache_create=tokens_in_cache_create,
        tokens_out=tokens_out,
        tokens_out_reasoning=tokens_out_reasoning,
        tokens_total=tokens_total,
        tool_calls=tool_calls,
        cost_usd_equivalent=cost_usd_equivalent,
        tokens_exact=tokens_exact,
        usage_source=usage_source,
        wire_tokens_estimate=wire_tokens_estimate,
        runtime_overhead_tokens=runtime_overhead_tokens,
    )


__all__ = [
    "AgentInvocationOutcome",
    "build_invocation_outcome",
]
