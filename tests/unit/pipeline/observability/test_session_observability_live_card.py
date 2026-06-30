from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline.observability.invocation_outcome import AgentInvocationOutcome
from pipeline.phases.builtin.session_observability import render_live_card


def test_live_card_estimates_cost_when_runtime_reports_tokens_without_cost(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from core.infra import config
    from core.observability import logging as obs_logging, pricing

    monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
    monkeypatch.setattr(obs_logging, "get_output_mode", lambda: "live")
    monkeypatch.setattr(
        pricing,
        "estimate_cost_usd",
        lambda model, *, tokens_in, tokens_out, cached_tokens_in=0: 0.456,
    )
    config._reset_config()
    try:
        render_live_card(
            agent=SimpleNamespace(model="gpt-5.5"),
            outcome=AgentInvocationOutcome(
                runtime="codex",
                model="gpt-5.5",
                tokens_in=1000,
                tokens_in_fresh=None,
                tokens_in_cache_read=None,
                tokens_in_cache_create=None,
                tokens_out=200,
                tokens_out_reasoning=None,
                tokens_total=1200,
                tool_calls=0,
                cost_usd_equivalent=None,
                tokens_exact=True,
                usage_source="runtime_reported",
                wire_tokens_estimate=None,
                runtime_overhead_tokens=None,
            ),
            pressure=SimpleNamespace(
                context_used_tokens=None,
                context_window_tokens=None,
                context_source=SimpleNamespace(value="runtime_reported"),
            ),
            duration_s=12.3,
            trace_slot="review_changes",
            loop_round=1,
        )
    finally:
        config._reset_config()

    assert "✓ review_changes · round 1 · 12.3s · ~$0.456" in capsys.readouterr().out
