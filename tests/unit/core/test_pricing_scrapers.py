"""HTML/JSON scrapers + validation for the pricing-refresh CLI.

Contracts under test (no real network — `fetch_html` is monkeypatched
when ``refresh`` is exercised; everything else takes inline strings):

* `known_providers()` reports the names that `refresh()` accepts.
* `scrape_pricepertoken()` parses the documented `<table>` layout,
  normalises model names, and raises `PricingScrapeError` with an
  operator-actionable message on each documented layout drift.
* `scrape_openai_pricing()` extracts model rates via Next.js
  `__NEXT_DATA__` JSON or the Astro hydration-markup fallback;
  raises when neither yields models or when the Next.js blob is
  malformed JSON.
* `validate_models()` reports empty / non-numeric / out-of-range
  rates per the documented `[0.01, 1000]` window. ``rates.get(half,
  0.0)`` means a missing key falls through to out-of-range-low.
* `refresh()` orchestrates fetch → scrape → validate and surfaces
  the right `PricingScrapeError` at each step.

Explicit non-goals: real `urllib.request.urlopen` calls inside
`fetch_html` (lines 154-156 of the source) are not exercised — they
require network and are tracked as a deferred follow-up.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from core.observability import pricing_scrapers as ps
from core.observability.pricing_scrapers import (
    PricingScrapeError,
    RefreshResult,
    known_providers,
    refresh,
    scrape_openai_pricing,
    scrape_pricepertoken,
    validate_models,
)

# ── known_providers ─────────────────────────────────────────────────────────


class TestKnownProviders:
    def test_contains_documented_providers(self) -> None:
        names = known_providers()
        assert "openai" in names
        assert "pricepertoken" in names

    def test_is_tuple(self) -> None:
        # Tuple (not list) so callers can hash / pin / pattern-match.
        assert isinstance(known_providers(), tuple)


# ── scrape_pricepertoken ────────────────────────────────────────────────────


def _ppt_table(rows_html: str) -> str:
    """Wrap raw row HTML in the table shape pricepertoken uses."""
    return f"<html><body><table>{rows_html}</table></body></html>"


class TestScrapePricepertokenHappy:
    def test_basic_table_parses(self) -> None:
        html = _ppt_table(
            "<tr><th>Model</th><th>Input</th><th>Output</th></tr>"
            "<tr><td>GPT-5 Nano</td><td>$0.50</td><td>$1.20</td></tr>"
            "<tr><td>GPT-5</td><td>$2.50</td><td>$10.00</td></tr>"
        )
        out = scrape_pricepertoken(html)
        assert out == {
            "gpt-5-nano": {"input_per_1m_usd": 0.50, "output_per_1m_usd": 1.20},
            "gpt-5": {"input_per_1m_usd": 2.50, "output_per_1m_usd": 10.00},
        }

    def test_normalizes_spaces_and_case(self) -> None:
        html = _ppt_table(
            "<tr><th>Model</th><th>Input</th><th>Output</th></tr>"
            "<tr><td>Claude   Sonnet 4.6</td><td>$3.00</td><td>$15.00</td></tr>"
        )
        out = scrape_pricepertoken(html)
        # Lowercased + collapsed whitespace → single hyphen.
        assert "claude-sonnet-4.6" in out

    def test_ignores_extra_columns(self) -> None:
        # Real pages have Cached / Batch / Context-window columns we
        # don't care about; header-driven indexing must still find the
        # right ones.
        html = _ppt_table(
            "<tr><th>Model</th><th>Cached</th><th>Input</th>"
            "<th>Output</th><th>Batch</th></tr>"
            "<tr><td>GPT-5</td><td>$0.25</td><td>$2.50</td>"
            "<td>$10.00</td><td>$1.25</td></tr>"
        )
        out = scrape_pricepertoken(html)
        assert out == {
            "gpt-5": {"input_per_1m_usd": 2.50, "output_per_1m_usd": 10.00},
        }

    def test_header_case_insensitive(self) -> None:
        html = _ppt_table(
            "<tr><th>MODEL</th><th>input</th><th>Output</th></tr>"
            "<tr><td>X</td><td>$1.00</td><td>$2.00</td></tr>"
        )
        assert "x" in scrape_pricepertoken(html)


class TestScrapePricepertokenErrors:
    def test_no_table_raises(self) -> None:
        with pytest.raises(PricingScrapeError, match=r"no ``<table>``"):
            scrape_pricepertoken("<html><body>no table here</body></html>")

    def test_table_without_rows_raises(self) -> None:
        with pytest.raises(PricingScrapeError, match=r"no ``<tr>`` rows"):
            scrape_pricepertoken("<table></table>")

    def test_headers_without_required_columns_raises(self) -> None:
        html = _ppt_table(
            "<tr><th>Foo</th><th>Bar</th><th>Baz</th></tr>"
            "<tr><td>x</td><td>y</td><td>z</td></tr>"
        )
        with pytest.raises(PricingScrapeError, match="Model/Input/Output"):
            scrape_pricepertoken(html)


class TestScrapePricepertokenSkipsMalformedRows:
    """Defensive parsing — a single bad row mustn't crash the scrape."""

    def test_row_with_too_few_cells_skipped(self) -> None:
        html = _ppt_table(
            "<tr><th>Model</th><th>Input</th><th>Output</th></tr>"
            "<tr><td>complete</td><td>$1.00</td><td>$2.00</td></tr>"
            "<tr><td>only-two-cells</td><td>$1.00</td></tr>"
        )
        out = scrape_pricepertoken(html)
        assert "complete" in out
        assert "only-two-cells" not in out

    def test_row_with_placeholder_name_skipped(self) -> None:
        html = _ppt_table(
            "<tr><th>Model</th><th>Input</th><th>Output</th></tr>"
            "<tr><td>real</td><td>$1.00</td><td>$2.00</td></tr>"
            "<tr><td>-</td><td>$1.00</td><td>$2.00</td></tr>"
            "<tr><td>—</td><td>$1.00</td><td>$2.00</td></tr>"
        )
        out = scrape_pricepertoken(html)
        assert set(out.keys()) == {"real"}

    def test_row_with_unparseable_dollar_cell_skipped(self) -> None:
        html = _ppt_table(
            "<tr><th>Model</th><th>Input</th><th>Output</th></tr>"
            "<tr><td>good</td><td>$1.00</td><td>$2.00</td></tr>"
            "<tr><td>no-dollar</td><td>n/a</td><td>$2.00</td></tr>"
        )
        out = scrape_pricepertoken(html)
        assert "good" in out
        assert "no-dollar" not in out


# ── scrape_openai_pricing ───────────────────────────────────────────────────


def _next_data_blob(models: list[dict]) -> str:
    """Wrap a models list in the Next.js __NEXT_DATA__ shape."""
    payload = {"props": {"pageProps": {"pricing": {"models": models}}}}
    return (
        '<html><head>'
        '<script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(payload)}"
        '</script></head><body></body></html>'
    )


class TestScrapeOpenaiPricingNextData:
    def test_extracts_from_next_data_blob(self) -> None:
        html = _next_data_blob([
            {"name": "gpt-5",
             "input_per_1m_usd": 2.5,
             "output_per_1m_usd": 10.0},
            {"name": "gpt-5-mini",
             "input_per_1m_usd": 0.25,
             "output_per_1m_usd": 2.0},
        ])
        out = scrape_openai_pricing(html)
        assert out == {
            "gpt-5": {"input_per_1m_usd": 2.5, "output_per_1m_usd": 10.0},
            "gpt-5-mini": {"input_per_1m_usd": 0.25, "output_per_1m_usd": 2.0},
        }

    def test_extracts_with_alternate_key_names(self) -> None:
        # `_extract_from_next_data` accepts model / name / id / slug for
        # the name; inputCost / prompt / prompt_cost for input; etc.
        html = _next_data_blob([
            {"model": "alt-keys",
             "inputCost": 1.0,
             "outputCost": 2.0},
        ])
        out = scrape_openai_pricing(html)
        assert "alt-keys" in out

    def test_extracts_with_dollar_string_prices(self) -> None:
        # Price values can be strings like "$2.50 / 1M tokens".
        html = _next_data_blob([
            {"name": "string-price",
             "input": "$2.50 / 1M tokens",
             "output": "$10.00 / 1M tokens"},
        ])
        out = scrape_openai_pricing(html)
        assert out == {
            "string-price": {"input_per_1m_usd": 2.5, "output_per_1m_usd": 10.0},
        }

    def test_malformed_json_raises(self) -> None:
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{not valid json}'
            '</script>'
        )
        with pytest.raises(PricingScrapeError, match="not valid JSON"):
            scrape_openai_pricing(html)

    def test_next_data_empty_falls_through_to_astro(self) -> None:
        # __NEXT_DATA__ present but yields zero models → Astro fallback
        # gets a chance. Embed an empty payload + Astro hydration markup
        # in the same HTML.
        next_empty = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props": {}}'
            '</script>'
        )
        astro_markup = (
            '[0,"gpt-5"], extra padding here, '
            '[0,"$2.50 / 1M input tokens"], more, '
            '[0,"$0.25 / 1M cached tokens"], more, '
            '[0,"$10.00 / 1M output tokens"]'
        )
        out = scrape_openai_pricing(next_empty + astro_markup)
        assert "gpt-5" in out


class TestScrapeOpenaiPricingAstro:
    def test_extracts_from_astro_hydration_markup(self) -> None:
        # Pattern observed: [0,"<model>"] followed within ~800 chars by
        # two [0,"$X / 1M ... tokens"] entries.
        html = (
            '[0,"gpt-5"], padding, '
            '[0,"$2.50 / 1M input tokens"], more, '
            '[0,"$0.25 / 1M cached tokens"], more, '
            '[0,"$10.00 / 1M output tokens"]'
        )
        out = scrape_openai_pricing(html)
        assert out == {
            "gpt-5": {
                "input_per_1m_usd": 2.5,
                "cached_input_per_1m_usd": 0.25,
                "output_per_1m_usd": 10.0,
            },
        }

    def test_duplicate_astro_rows_keep_first_standard_price(self) -> None:
        # Official pricing pages can list Standard, then Batch/Flex/Priority.
        # The cost model stores Standard rates, so later repeats must not
        # overwrite the first row.
        html = (
            '[0,"gpt-5.5"], [0,"$5.00 / 1M input tokens"], '
            '[0,"$0.50 / 1M cached tokens"], '
            '[0,"$30.00 / 1M output tokens"], '
            '[0,"gpt-5.5"], [0,"$12.50 / 1M input tokens"], '
            '[0,"$1.25 / 1M cached tokens"], '
            '[0,"$75.00 / 1M output tokens"]'
        )
        out = scrape_openai_pricing(html)

        assert out["gpt-5.5"] == {
            "input_per_1m_usd": 5.0,
            "cached_input_per_1m_usd": 0.5,
            "output_per_1m_usd": 30.0,
        }

    def test_skips_embeddings_and_non_chat_models(self) -> None:
        # Skip-list: any model with "embedding", or starting with tts-,
        # dall-, whisper.
        skipped_html = "".join(
            f'[0,"{name}"], [0,"$1.00 / 1M tokens"], [0,"$2.00 / 1M tokens"]; '
            for name in (
                "text-embedding-3-small",
                "tts-1",
                "dall-e-3",
                "whisper-1",
            )
        )
        good_html = (
            '[0,"gpt-5"], [0,"$1.00 / 1M tokens"], '
            '[0,"$0.10 / 1M tokens"], [0,"$2.00 / 1M tokens"]'
        )
        out = scrape_openai_pricing(skipped_html + good_html)
        assert set(out.keys()) == {"gpt-5"}

    def test_skips_model_with_fewer_than_two_rates(self) -> None:
        html = (
            '[0,"gpt-5"], padding, '
            '[0,"$2.50 / 1M tokens"], '
            '[0,"$0.25 / 1M tokens"]'
            # Only two rates — model gets skipped.
        )
        with pytest.raises(PricingScrapeError):
            scrape_openai_pricing(html)

    def test_skips_zero_rates_pair(self) -> None:
        # Both rates 0.0 in a row → defensive skip (parse glitch).
        html = (
            '[0,"gpt-5"], padding, '
            '[0,"$0.0 / 1M tokens"], more, '
            '[0,"$0.0 / 1M tokens"], more, '
            '[0,"$0.0 / 1M tokens"]'
        )
        with pytest.raises(PricingScrapeError):
            scrape_openai_pricing(html)


class TestScrapeOpenaiPricingRenderedText:
    def test_extracts_current_rendered_flagship_rows(self) -> None:
        html = """
        Flagship models
        Prices per 1M tokens.
        Standard Batch Flex Priority
        Standard
        Short context Long context
        Model Input Cached input Output Input Cached input Output
        gpt-5.5$5.00$0.50$30.00$10.00$1.00$45.00
        gpt-5.5-pro$30.00-$180.00$60.00-$270.00
        gpt-5.4$2.50$0.25$15.00$5.00$0.50$22.50
        gpt-5.4-mini$0.75$0.075$4.50---
        gpt-5.4-nano$0.20$0.02$1.25---
        gpt-5.4-pro$30.00-$180.00$60.00-$270.00
        """
        out = scrape_openai_pricing(html)

        assert out["gpt-5.5"] == {
            "input_per_1m_usd": 5.0,
            "cached_input_per_1m_usd": 0.5,
            "output_per_1m_usd": 30.0,
        }
        assert out["gpt-5.5-pro"] == {
            "input_per_1m_usd": 30.0,
            "output_per_1m_usd": 180.0,
        }
        assert out["gpt-5.4-nano"] == {
            "input_per_1m_usd": 0.20,
            "cached_input_per_1m_usd": 0.02,
            "output_per_1m_usd": 1.25,
        }

    def test_rendered_text_skips_rows_without_regular_output_price(self) -> None:
        html = (
            "Realtime and audio generation models\n"
            "gpt-realtime-translate Audio--$0.034 / minute\n"
            "gpt-5.5$5.00$0.50$30.00\n"
        )
        out = scrape_openai_pricing(html)

        assert "gpt-realtime-translate" not in out
        assert out["gpt-5.5"]["output_per_1m_usd"] == 30.0

    def test_rendered_text_keeps_first_occurrence_for_standard_price(self) -> None:
        html = """
        Standard
        gpt-5.5$5.00$0.50$30.00$10.00$1.00$45.00
        Batch
        gpt-5.5$2.50$0.25$15.00$5.00$0.50$22.50
        Priority
        gpt-5.5$12.50$1.25$75.00---
        """
        out = scrape_openai_pricing(html)

        assert out["gpt-5.5"] == {
            "input_per_1m_usd": 5.0,
            "cached_input_per_1m_usd": 0.5,
            "output_per_1m_usd": 30.0,
        }


class TestScrapeOpenaiPricingErrors:
    def test_no_data_anywhere_raises(self) -> None:
        with pytest.raises(PricingScrapeError, match="layout has likely changed"):
            scrape_openai_pricing("<html><body>nothing useful here</body></html>")


# ── validate_models ─────────────────────────────────────────────────────────


class TestValidateModels:
    def test_empty_dict_yields_one_error(self) -> None:
        errors = validate_models({})
        assert len(errors) == 1
        assert "no models extracted" in errors[0]

    def test_happy_case_returns_empty_list(self) -> None:
        errors = validate_models({
            "gpt-5": {"input_per_1m_usd": 2.5, "output_per_1m_usd": 10.0},
            "gpt-5-mini": {"input_per_1m_usd": 0.25, "output_per_1m_usd": 2.0},
        })
        assert errors == []

    def test_non_numeric_input_flagged(self) -> None:
        errors = validate_models({
            "broken": {"input_per_1m_usd": "abc", "output_per_1m_usd": 1.0},
        })
        assert any("non-numeric" in e for e in errors)
        assert any("broken.input_per_1m_usd" in e for e in errors)

    def test_out_of_range_high_flagged(self) -> None:
        errors = validate_models({
            "scraper-glitch": {
                "input_per_1m_usd": 9999.0,
                "output_per_1m_usd": 1.0,
            },
        })
        assert any("out of range" in e for e in errors)
        assert any("scraper-glitch.input_per_1m_usd" in e for e in errors)

    def test_out_of_range_low_zero_flagged(self) -> None:
        # 0.0 is below the 0.01 minimum.
        errors = validate_models({
            "zero-rate": {
                "input_per_1m_usd": 0.0,
                "output_per_1m_usd": 1.0,
            },
        })
        assert any("out of range" in e for e in errors)

    def test_missing_key_defaults_to_zero_and_trips_low_bound(self) -> None:
        # rates.get(half, 0.0) means a missing key falls through to the
        # range check, which then catches it as out-of-range-low. There
        # is no separate "missing key" error path.
        errors = validate_models({
            "incomplete": {"input_per_1m_usd": 2.5},  # output_per_1m_usd missing
        })
        assert any(
            "incomplete.output_per_1m_usd" in e and "out of range" in e
            for e in errors
        )


# ── refresh ─────────────────────────────────────────────────────────────────


class TestRefreshDispatch:
    def test_unknown_provider_raises_with_known_list(self) -> None:
        with pytest.raises(PricingScrapeError) as exc_info:
            refresh("nonexistent")
        msg = str(exc_info.value)
        assert "unknown provider" in msg
        assert "Known:" in msg
        # The error message should enumerate known providers, so the
        # operator sees the valid options without grepping the source.
        for name in known_providers():
            assert name in msg


class TestRefreshOrchestration:
    def test_refresh_pricepertoken_happy(self, monkeypatch) -> None:
        fake_html = _ppt_table(
            "<tr><th>Model</th><th>Input</th><th>Output</th></tr>"
            "<tr><td>GPT-5</td><td>$2.50</td><td>$10.00</td></tr>"
        )
        monkeypatch.setattr(ps, "fetch_html", lambda url, **_: fake_html)
        result = refresh("pricepertoken")
        assert isinstance(result, RefreshResult)
        assert result.models == {
            "gpt-5": {"input_per_1m_usd": 2.5, "output_per_1m_usd": 10.0},
        }
        assert urlparse(result.url).netloc == "pricepertoken.com"
        assert "third-party" in result.provenance.lower()

    def test_refresh_openai_happy(self, monkeypatch) -> None:
        fake_html = _next_data_blob([
            {"name": "gpt-5",
             "input_per_1m_usd": 2.5,
             "output_per_1m_usd": 10.0},
        ])
        monkeypatch.setattr(ps, "fetch_html", lambda url, **_: fake_html)
        result = refresh("openai")
        assert isinstance(result, RefreshResult)
        assert result.models == {
            "gpt-5": {"input_per_1m_usd": 2.5, "output_per_1m_usd": 10.0},
        }
        assert urlparse(result.url).netloc == "developers.openai.com"
        assert "official" in result.provenance.lower()

    def test_refresh_propagates_scrape_error(self, monkeypatch) -> None:
        # fetch_html returns garbage → scraper raises PricingScrapeError;
        # refresh should bubble it without wrapping in "validation failed".
        monkeypatch.setattr(
            ps, "fetch_html",
            lambda url, **_: "<html><body>no table here</body></html>",
        )
        with pytest.raises(PricingScrapeError, match=r"no ``<table>``"):
            refresh("pricepertoken")

    def test_refresh_raises_on_validation_failure(self, monkeypatch) -> None:
        # Scraper succeeds but produces out-of-range rates → validation
        # error path. The bundled error string starts with "validation
        # failed:" so the CLI can show all problems at once.
        bad_html = _ppt_table(
            "<tr><th>Model</th><th>Input</th><th>Output</th></tr>"
            "<tr><td>scraper-glitch</td><td>$9999.00</td><td>$10.00</td></tr>"
        )
        monkeypatch.setattr(ps, "fetch_html", lambda url, **_: bad_html)
        with pytest.raises(PricingScrapeError, match="validation failed"):
            refresh("pricepertoken")
