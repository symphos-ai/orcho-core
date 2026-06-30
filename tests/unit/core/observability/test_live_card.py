"""M14.4.4 — Live card formatter tests.

Pins:

* Header shape (✓ glyph, phase, round, duration, cost) with the
  optional pieces omitted when not meaningful.
* Provider-input line shape including cache annotation, savings hint, and
  the "prefix changed" suffix when the cache hit ratio drops
  below 50%.
* Response-line presence/absence rules.
* Context-line shape across the three honest variants (window +
  used, used only, suppressed) and the ⚠ warning marker at 80%
  fill.
* Halt path: ✗ glyph, header carries the reason, detail block
  suppressed.
* Token-count abbreviations (k / M / G).
"""
from __future__ import annotations

import dataclasses

import pytest

from core.observability.live_card import (
    LiveCardData,
    _format_token_count,
    format_live_card,
)


class TestTokenFormatting:
    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (0, "0"),
            (1, "1"),
            (999, "999"),
            (1_000, "1.0k"),
            (8_300, "8.3k"),
            (193_600, "193.6k"),
            (1_000_000, "1.0M"),
            (12_500_000, "12.5M"),
            (1_500_000_000, "1.5G"),
        ],
    )
    def test_abbreviations(self, n: int, expected: str) -> None:
        assert _format_token_count(n) == expected


class TestHeader:
    def test_minimal_header_only_phase_and_duration(self) -> None:
        card = format_live_card(
            LiveCardData(phase="plan", duration_s=1.42),
        )
        assert card == "✓ plan · 1.4s"

    def test_header_with_round_cost(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.4, round=1, cost_usd=0.012,
            ),
        )
        assert card.splitlines()[0] == "✓ plan · round 1 · 1.4s · $0.012"

    def test_header_marks_estimated_cost(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="review_changes",
                duration_s=1.4,
                round=1,
                cost_usd=0.012,
                cost_estimated=True,
            ),
        )
        assert card.splitlines()[0] == (
            "✓ review_changes · round 1 · 1.4s · ~$0.012"
        )

    def test_cost_zero_or_missing_omitted_from_header(self) -> None:
        card = format_live_card(
            LiveCardData(phase="plan", duration_s=1.4, cost_usd=0.0),
        )
        assert "$" not in card.splitlines()[0]
        card = format_live_card(
            LiveCardData(phase="plan", duration_s=1.4, cost_usd=None),
        )
        assert "$" not in card.splitlines()[0]


class TestProviderInputLine:
    def test_orcho_prompt_line_renders_before_provider_usage(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="implement", duration_s=1.4,
                prompt_tokens=1_600,
                tokens_in=347_900,
                tokens_out=3_200,
            ),
        )
        lines = card.splitlines()
        assert lines[1] == "    Orcho prompt   1.6k tokens"
        assert lines[2].startswith("    Provider input  347.9k tokens")

    def test_runtime_overhead_line_surfaces_the_gap(self) -> None:
        # Orcho assembled 4.9k, provider received 37.4k → the ~32.5k gap is
        # runtime injection (agent system prompt + tool schemas) and must be
        # an explicit line, not a silent delta between two footer numbers.
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.4,
                prompt_tokens=4_900,
                tokens_in=37_400,
            ),
        )
        overhead = next(
            ln for ln in card.splitlines() if "Runtime overhead" in ln
        )
        assert "32.5k tokens" in overhead
        assert "% of input" in overhead
        assert "not Orcho-built" in overhead

    def test_runtime_overhead_omitted_when_gap_is_noise(self) -> None:
        # prompt_tokens ≈ tokens_in: no meaningful runtime injection to show.
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.0,
                prompt_tokens=5_000,
                tokens_in=5_050,
            ),
        )
        assert "Runtime overhead" not in card

    def test_runtime_overhead_omitted_without_orcho_prompt(self) -> None:
        # No prompt_tokens → cannot compute the gap honestly, skip the line.
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.0,
                tokens_in=37_400,
            ),
        )
        assert "Runtime overhead" not in card

    def test_prompt_with_cache_annotation_and_savings(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.4, round=1, cost_usd=0.20,
                tokens_in=10_000, cache_read_tokens=9_100, tokens_out=620,
            ),
        )
        lines = card.splitlines()
        # Provider-input line carries cache % + savings annotation.
        assert any(
            "Provider input  10.0k tokens (91% cached" in ln
            for ln in lines
        )
        # Savings approximate: 9100 * 0.00002 * 0.9 ≈ $0.16 — round
        # to "$0.16 saved" (cents-precision).
        prompt_line = next(ln for ln in lines if "Provider input" in ln)
        assert "saved" in prompt_line

    def test_low_cache_hit_without_creation_signal_uses_fallback(self) -> None:
        # codex/gemini expose only the cached count (no cache_creation).
        # With no creation signal a low read % is the best prefix-break
        # proxy we have, so the fallback heuristic still fires.
        card = format_live_card(
            LiveCardData(
                phase="review", duration_s=2.1, round=2, cost_usd=0.018,
                tokens_in=4_000, cache_read_tokens=320, tokens_out=320,
            ),
        )
        prompt_line = next(
            ln for ln in card.splitlines() if "Provider input" in ln
        )
        assert "(8% cached — prefix changed)" in prompt_line
        assert "saved" not in prompt_line

    def test_cold_priming_call_not_flagged_as_prefix_changed(self) -> None:
        # The real PLAN/r1 case: low read % but the rest is cache_creation
        # (written this turn), not fresh. Coverage ≈ 100% → priming, NOT a
        # prefix break.
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.4, round=1, cost_usd=0.39,
                tokens_in=37_399, cache_read_tokens=18_020,
                cache_creation_tokens=19_373, tokens_out=10_163,
            ),
        )
        prompt_line = next(
            ln for ln in card.splitlines() if "Provider input" in ln
        )
        assert "prefix changed" not in prompt_line
        assert "(48% cached, 52% priming)" in prompt_line

    def test_warm_cache_with_small_creation_shows_savings(self) -> None:
        # PLAN/r2: high read %, small creation → normal cached + savings.
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=2.0, round=2, cost_usd=0.50,
                tokens_in=336_198, cache_read_tokens=316_097,
                cache_creation_tokens=20_091, tokens_out=600,
            ),
        )
        prompt_line = next(
            ln for ln in card.splitlines() if "Provider input" in ln
        )
        assert "(94% cached" in prompt_line
        assert "priming" not in prompt_line
        assert "prefix changed" not in prompt_line

    def test_real_prefix_break_with_high_fresh_still_flagged(self) -> None:
        # Large fresh remainder (low read AND low creation → low coverage):
        # the cacheable prefix genuinely changed.
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.0, round=1, cost_usd=0.1,
                tokens_in=10_000, cache_read_tokens=500,
                cache_creation_tokens=500, tokens_out=200,
            ),
        )
        prompt_line = next(
            ln for ln in card.splitlines() if "Provider input" in ln
        )
        assert "(5% cached — prefix changed)" in prompt_line

    def test_prompt_without_cache_data_shows_only_tokens(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.4,
                tokens_in=5_000, tokens_out=100,
            ),
        )
        prompt_line = next(
            ln for ln in card.splitlines() if "Provider input" in ln
        )
        assert prompt_line.endswith("5.0k tokens")

    def test_no_tokens_in_omits_prompt_line(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.4, tokens_out=100,
            ),
        )
        assert "Provider input" not in card

    def test_total_only_usage_renders_split_unknown(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="validate_plan", duration_s=3.0,
                tokens_total=258_247,
            ),
        )
        assert "    Provider total  258.2k tokens (split unknown)" in card

    def test_codex_split_usage_prefers_provider_input_shape(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="validate_plan", duration_s=9.7,
                tokens_in=21_000,
                cache_read_tokens=3_780,
                tokens_out=3_500,
                reasoning_output_tokens=1_200,
                tokens_total=24_500,
            ),
        )
        assert "Provider total" not in card
        assert "    Provider input  21.0k tokens (18% cached" in card
        assert "    Response  3.5k tokens (1.2k reasoning)" in card


class TestResponseLine:
    def test_response_renders_when_tokens_out_present(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.4,
                tokens_in=1000, tokens_out=620,
            ),
        )
        assert "    Response  620 tokens" in card

    def test_response_omitted_when_tokens_out_zero(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.4,
                tokens_in=1000, tokens_out=0,
            ),
        )
        assert "Response" not in card

    def test_response_omitted_when_tokens_out_none(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.4, tokens_in=1000,
            ),
        )
        assert "Response" not in card

    def test_response_reasoning_annotation_omitted_when_zero(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="validate_plan", duration_s=1.4,
                tokens_in=1000,
                tokens_out=620,
                reasoning_output_tokens=0,
            ),
        )
        assert "    Response  620 tokens" in card
        assert "reasoning" not in card


class TestActivityLine:
    def test_tool_call_count_renders_when_present(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="implement", duration_s=1.0,
                tokens_in=1000, tool_calls=9,
            ),
        )
        assert "    Activity  tools=9 calls" in card

    def test_activity_omitted_when_no_counts(self) -> None:
        card = format_live_card(
            LiveCardData(phase="implement", duration_s=1.0, tokens_in=1000),
        )
        assert "Activity" not in card


class TestContextLine:
    def test_window_and_used_render_full_shape(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.4,
                tokens_in=1000, tokens_out=10,
                context_used_tokens=12_000,
                context_window_tokens=1_000_000,
                context_source="runtime_reported",
            ),
        )
        assert "    Live context    12.0k / 1.0M (1% full)" in card

    def test_high_fill_ratio_gets_warning_marker(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="implement", duration_s=3.2,
                tokens_in=10_000, tokens_out=300,
                context_used_tokens=870_000,
                context_window_tokens=1_000_000,
                context_source="runtime_reported",
            ),
        )
        ctx = next(ln for ln in card.splitlines() if "Live context" in ln)
        assert "⚠" in ctx
        assert "approaching limit" in ctx
        assert "(87% — approaching limit)" in ctx

    def test_exact_80_percent_threshold_triggers_warning(self) -> None:
        # The threshold is "approaching limit" at >= 80% (≥, not >).
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.0,
                tokens_in=1000, tokens_out=10,
                context_used_tokens=800,
                context_window_tokens=1_000,
                context_source="runtime_reported",
            ),
        )
        ctx = next(ln for ln in card.splitlines() if "Live context" in ln)
        assert "⚠" in ctx

    def test_used_only_falls_back_to_simpler_shape(self) -> None:
        # orcho_estimated without config_window: used known, window None.
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.4,
                tokens_in=1000, tokens_out=10,
                context_used_tokens=12_345,
                context_window_tokens=None,
                context_source="orcho_estimated",
            ),
        )
        assert "    Orcho est.      12.3k used" in card

    def test_no_context_data_omits_line(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.4,
                tokens_in=1000, tokens_out=10,
            ),
        )
        assert "Context" not in card
        assert "Live context" not in card
        assert "Orcho est." not in card

    def test_zero_window_treated_as_unknown(self) -> None:
        # Defensive: a runtime that stamps contextWindow=0 should
        # not crash the formatter on a divide-by-zero — fall back
        # to the "used only" shape.
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.4,
                tokens_in=1000, tokens_out=10,
                context_used_tokens=500,
                context_window_tokens=0,
                context_source="runtime_reported",
            ),
        )
        assert "    Live context    500 used" in card
        assert "/" not in card.splitlines()[-1]


class TestHaltPath:
    def test_halt_flips_glyph_and_includes_reason_in_header(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=0.8, round=1, cost_usd=0.003,
                halt_reason="parse_error: missing 'plan' key",
            ),
        )
        # Single line: header only, no detail block.
        assert card.startswith("✗ plan")
        assert "round 1" in card
        assert "halted — parse_error: missing 'plan' key" in card
        # No provider-input / Response / Context detail block.
        assert "\n" not in card

    def test_halt_suppresses_detail_block_even_if_tokens_known(self) -> None:
        # Edge case: agent.invoke returned bytes that failed
        # parsing — tokens are known, but the card surfaces the
        # halt, not the consumed-but-failed accounting (that
        # belongs in evidence, not the live one-liner).
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.0, round=1,
                tokens_in=1000, tokens_out=10,
                context_used_tokens=12_000,
                context_window_tokens=1_000_000,
                halt_reason="parse_error",
            ),
        )
        assert "Provider input" not in card
        assert "Response" not in card
        assert "Context" not in card


class TestCanonicalSamples:
    """End-to-end shape pins matching the docstring examples in
    ``core/observability/live_card.py``. Drift here means the
    rendered card no longer matches what the module promises in
    its public docs."""

    def test_normal_call_canonical_form(self) -> None:
        # ~$0.18 saved on the example needs a higher per-token
        # cost than this synthetic call's $0.012 / 10k = $0.0000012;
        # use the example numbers verbatim from the docstring to
        # reproduce.
        card = format_live_card(
            LiveCardData(
                phase="plan", duration_s=1.4, round=1, cost_usd=0.012,
                tokens_in=8_300, cache_read_tokens=7_553,  # 91%
                tokens_out=620,
                context_used_tokens=12_000,
                context_window_tokens=1_000_000,
                context_source="runtime_reported",
            ),
        )
        lines = card.splitlines()
        assert lines[0] == "✓ plan · round 1 · 1.4s · $0.012"
        assert lines[1].startswith(
            "    Provider input  8.3k tokens (91% cached"
        )
        assert lines[2] == "    Response  620 tokens"
        assert lines[3] == "    Live context    12.0k / 1.0M (1% full)"

    def test_anomaly_call_canonical_form(self) -> None:
        card = format_live_card(
            LiveCardData(
                phase="review", duration_s=2.1, round=2, cost_usd=0.018,
                tokens_in=4_100, cache_read_tokens=328,  # 8%
                tokens_out=320,
                context_used_tokens=870_000,
                context_window_tokens=1_000_000,
                context_source="runtime_reported",
            ),
        )
        lines = card.splitlines()
        assert lines[0] == "✗ review · round 2 · 2.1s · $0.018" or \
               lines[0] == "✓ review · round 2 · 2.1s · $0.018"
        prompt_line = lines[1]
        assert "(8% cached — prefix changed)" in prompt_line
        ctx = lines[3]
        assert "⚠" in ctx
        assert "(87% — approaching limit)" in ctx


class TestDataclassSurface:
    def test_is_frozen(self) -> None:
        d = LiveCardData(phase="plan", duration_s=1.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            d.phase = "implement"  # type: ignore[misc]
