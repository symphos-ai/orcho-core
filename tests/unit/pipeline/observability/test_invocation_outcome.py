# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the transient AgentInvocationOutcome normalizer.

Each test uses a lightweight ``SimpleNamespace`` fake carrying the
``last_*`` attributes a given runtime would stamp on itself after
``agent.invoke``. No real CLI runtime is constructed or invoked.
"""
from __future__ import annotations

from types import SimpleNamespace

from core.observability.metrics import estimate_tokens
from pipeline.observability.invocation_outcome import (
    AgentInvocationOutcome,
    build_invocation_outcome,
)


def test_claude_like_derives_total_from_in_plus_out() -> None:
    # Claude exposes the cache split but no provider total and no
    # reasoning-output count.
    agent = SimpleNamespace(
        last_tokens_in=10_000,
        last_tokens_in_fresh=900,
        last_tokens_in_cache_read=9_000,
        last_tokens_in_cache_create=100,
        last_tokens_out=620,
        last_cost_usd=0.012,
    )
    out = build_invocation_outcome(
        agent=agent, runtime_id="claude", model="claude-x", wire_text="hi",
    )
    assert isinstance(out, AgentInvocationOutcome)
    assert out.tokens_total == 10_620  # derived in + out
    assert out.tokens_in_cache_create == 100
    assert out.tokens_out_reasoning is None
    assert out.usage_source == "runtime_reported"
    assert out.tokens_exact is True
    assert out.cost_usd_equivalent == 0.012


def test_codex_like_provider_total_and_no_cache_create() -> None:
    # Codex exposes a provider total and reasoning tokens, but never a
    # cache-creation count.
    agent = SimpleNamespace(
        last_tokens_in=21_000,
        last_tokens_in_fresh=17_220,
        last_tokens_in_cache_read=3_780,
        last_tokens_out=3_500,
        last_tokens_out_reasoning=1_200,
        last_tokens_total=24_500,
        last_tool_use_count=9,
    )
    out = build_invocation_outcome(
        agent=agent, runtime_id="codex", model="o-x", wire_text="hi",
    )
    assert out.tokens_in_cache_create is None
    assert out.tokens_out_reasoning == 1_200
    assert out.tokens_total == 24_500  # provider value, not derived
    assert out.usage_source == "runtime_reported"
    assert out.tokens_exact is True
    assert out.tool_calls == 9


def test_codex_legacy_total_only_is_runtime_partial() -> None:
    # Legacy parse: only a provider total survives; in/out are unknown.
    agent = SimpleNamespace(last_tokens_total=258_247)
    out = build_invocation_outcome(
        agent=agent, runtime_id="codex", model="o-legacy", wire_text="hi",
    )
    assert out.tokens_in is None
    assert out.tokens_out is None
    assert out.tokens_total == 258_247
    assert out.usage_source == "runtime_partial"
    assert out.tokens_exact is True
    assert out.runtime_overhead_tokens is None  # no tokens_in to subtract


def test_gemini_like_reads_values_without_recompute() -> None:
    # Gemini: cached is a SUBSET of input (not additive); fresh is
    # input - cached; no cache-creation; provider total present.
    agent = SimpleNamespace(
        last_tokens_in=12_000,
        last_tokens_in_fresh=3_000,
        last_tokens_in_cache_read=9_000,
        last_tokens_out=400,
        last_tokens_total=12_400,
    )
    out = build_invocation_outcome(
        agent=agent, runtime_id="gemini", model="g-x", wire_text="hi",
    )
    # Values are read as-is, never recomputed from the cache split.
    assert out.tokens_in == 12_000
    assert out.tokens_in_fresh == 3_000
    assert out.tokens_in_cache_read == 9_000
    assert out.tokens_in_cache_create is None
    assert out.tokens_total == 12_400  # provider value, not in + out
    assert out.usage_source == "runtime_reported"


def test_empty_mock_agent_falls_back_to_estimate() -> None:
    agent = SimpleNamespace()  # no last_* attributes at all
    wire_text = "some wire prompt text"
    out = build_invocation_outcome(
        agent=agent, runtime_id="mock", model="mock-model", wire_text=wire_text,
    )
    assert out.tokens_in is None
    assert out.tokens_out is None
    assert out.tokens_total is None
    assert out.tokens_in_cache_read is None
    assert out.tokens_in_cache_create is None
    assert out.tokens_out_reasoning is None
    assert out.cost_usd_equivalent is None
    assert out.tool_calls == 0
    assert out.usage_source == "estimate"
    assert out.tokens_exact is False
    assert out.wire_tokens_estimate == estimate_tokens(wire_text)
    assert out.runtime_overhead_tokens is None


def test_runtime_overhead_positive_when_input_exceeds_estimate() -> None:
    wire_text = "tiny"
    estimate = estimate_tokens(wire_text)
    tokens_in = estimate + 30_000  # provider input well above the estimate
    agent = SimpleNamespace(last_tokens_in=tokens_in, last_tokens_out=10)
    out = build_invocation_outcome(
        agent=agent, runtime_id="claude", model="c", wire_text=wire_text,
    )
    assert out.runtime_overhead_tokens == tokens_in - estimate


def test_runtime_overhead_none_when_input_below_estimate() -> None:
    # Heavily-cached delta render: provider input < wire estimate, so a
    # negative "overhead" is meaningless and collapses to None.
    wire_text = "word " * 2_000  # large estimate
    estimate = estimate_tokens(wire_text)
    assert estimate > 1
    agent = SimpleNamespace(last_tokens_in=1, last_tokens_out=1)
    out = build_invocation_outcome(
        agent=agent, runtime_id="claude", model="c", wire_text=wire_text,
    )
    assert out.runtime_overhead_tokens is None


def test_wire_tokens_estimate_matches_metrics_helper() -> None:
    wire_text = "the quick brown fox jumps over the lazy dog"
    agent = SimpleNamespace()
    out = build_invocation_outcome(
        agent=agent, runtime_id="mock", model="m", wire_text=wire_text,
    )
    assert out.wire_tokens_estimate == estimate_tokens(wire_text)


def test_runtime_and_model_passed_through_verbatim() -> None:
    agent = SimpleNamespace()
    out = build_invocation_outcome(
        agent=agent,
        runtime_id="some.Runtime.Path",
        model="model-id-7",
        wire_text="",
    )
    assert out.runtime == "some.Runtime.Path"
    assert out.model == "model-id-7"
