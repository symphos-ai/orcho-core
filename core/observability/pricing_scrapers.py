"""core.observability.pricing_scrapers — provider-specific HTML scrapers.

Lives next to ``pricing.py`` (which owns the snapshot loader / cost
estimator). This module owns the business of *acquiring* the table
from a public source — HTTP fetch, HTML parsing, structural
validation. Split out of ``cli/orcho.py`` (where it had grown to
~400 LoC) so that:

  * The CLI command stays a thin facade — dispatch, display, write.
  * When a vendor changes their pricing page layout, the diff lives
    in one focused file rather than buried in 1.5K LoC of CLI.
  * Tests can import scrapers directly without booting the argparse
    machinery.

Public surface:

    refresh(provider)       — fetch + parse + validate, returns
                              (models, url, provenance) or raises.
    PricingScrapeError      — raised when a page's structure no longer
                              matches the scraper's expectations.

Two providers are supported today:

    "openai"        — developers.openai.com (Astro SSR — fragile).
    "pricepertoken" — pricepertoken.com (third-party aggregator —
                      stable HTML <table>, more reliable parser, but
                      not authoritative; user must verify).

Adding a third provider is a function + an entry in ``_PROVIDERS``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


class PricingScrapeError(RuntimeError):
    """Raised when a scraper can't extract a believable model→rate
    map from the HTML. Specific enough to distinguish from network
    errors so callers can show a more actionable hint.
    """


@dataclass(frozen=True)
class RefreshResult:
    """What ``refresh()`` returns on success.

    ``models`` is the parsed table; ``url`` is where it came from;
    ``provenance`` is a human-readable label callers can print so the
    operator knows whether the numbers are official or third-party.
    """
    models: dict[str, dict[str, float]]
    url: str
    provenance: str


# ─────────────────────────────────────────────────────────────────────────────
# HTTP fetch — kept here so callers don't import urllib themselves
# ─────────────────────────────────────────────────────────────────────────────

def fetch_html(url: str, *, timeout: float = 30.0) -> str:
    """GET ``url`` with a short timeout and a stable user-agent.

    Raises whatever ``urllib`` raises (URLError, HTTPError, etc.) —
    the CLI catches and prints the actionable hint. Keeping the
    network call here means tests can monkeypatch ``fetch_html``
    instead of ``urllib`` itself.
    """
    import urllib.request
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "orcho-pricing-refresh/0.3 "
                "(+https://github.com/symphos-ai/orcho-core)"
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ─────────────────────────────────────────────────────────────────────────────
# Provider: openai — developers.openai.com (Astro SSR)
# ─────────────────────────────────────────────────────────────────────────────

def scrape_openai_pricing(html: str) -> dict[str, dict[str, float]]:
    """Best-effort scrape of the public OpenAI pricing page.

    Two fallback strategies, tried in order:

    1. **Next.js SSR**: ``<script id="__NEXT_DATA__" …>{…}</script>``
       — worked when docs.openai.com was Next.js. Walked recursively
       for model→rate rows.

    2. **Astro SSR**: developers.openai.com is built with Astro;
       pricing tables live inside HTML-encoded JSON-like fragments
       embedded in ``<script>`` hydration data, of the shape
       ``[0,"gpt-5.4"], …, [0,"$2.50 / 1M tokens"], …``. We
       html.unescape, then walk all (model, rate1, rate2) triples
       within a small text window.

    3. **Rendered table text**: current docs expose visible rows as
       ``gpt-5.5$5.00$0.50$30.00…``. We parse Standard sections and
       store the regular short-context input/output rates.

    Raises ``PricingScrapeError`` when neither strategy yields models.
    """
    import re

    next_data = re.search(
        r'<script[^>]*id="__NEXT_DATA__"[^>]*type="application/json"[^>]*>(.+?)</script>',
        html, re.DOTALL,
    )
    if next_data:
        try:
            blob = json.loads(next_data.group(1))
        except json.JSONDecodeError as exc:
            raise PricingScrapeError(
                f"__NEXT_DATA__ blob is not valid JSON: {exc}"
            ) from exc
        models = _extract_from_next_data(blob)
        if models:
            return models
        # Don't raise yet — try Astro fallback below.

    models = _extract_from_astro_html(html)
    if models:
        return models

    models = _extract_from_rendered_pricing_text(html)
    if models:
        return models

    raise PricingScrapeError(
        "neither ``__NEXT_DATA__`` nor Astro hydration data nor rendered "
        "pricing text yielded a model→rate map. Page layout has likely "
        "changed; orcho's scraper needs an update for the current shape."
    )


def _extract_from_next_data(blob) -> dict[str, dict[str, float]]:
    """Walk the Next.js ``__NEXT_DATA__`` blob recursively, looking
    for nodes that look like ``{name: <model>, input: <num>, output:
    <num>}`` or analogous shapes. Tolerant of structural changes —
    we don't require a fixed path. Returns an empty dict when nothing
    matches; the caller treats that as "page changed".
    """
    out: dict[str, dict[str, float]] = {}

    def _coerce_price(v) -> float | None:
        """Accept ``2.5`` / ``"2.5"`` / ``"$2.50 / 1M"``."""
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            import re
            m = re.search(r"(\d+\.?\d*)", v)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    return None
        return None

    def _walk(node):
        if isinstance(node, dict):
            name_keys = ("model", "name", "id", "slug")
            in_keys = ("input", "input_per_1m", "input_per_1m_usd",
                       "inputCost", "input_cost", "prompt", "prompt_cost")
            out_keys = ("output", "output_per_1m", "output_per_1m_usd",
                        "outputCost", "output_cost", "completion",
                        "completion_cost")
            name = next(
                (str(node[k]) for k in name_keys
                 if k in node and isinstance(node[k], str) and node[k]),
                None,
            )
            in_v = next(
                (_coerce_price(node[k]) for k in in_keys
                 if k in node and _coerce_price(node[k]) is not None),
                None,
            )
            out_v = next(
                (_coerce_price(node[k]) for k in out_keys
                 if k in node and _coerce_price(node[k]) is not None),
                None,
            )
            if (name and in_v is not None and out_v is not None
                    and any(c.isalpha() for c in name) and len(name) <= 64):
                row = {
                    "input_per_1m_usd": in_v,
                    "output_per_1m_usd": out_v,
                }
                cached_v = next(
                    (_coerce_price(node[k]) for k in (
                        "cached_input",
                        "cachedInput",
                        "cached_input_per_1m",
                        "cached_input_per_1m_usd",
                    ) if k in node and _coerce_price(node[k]) is not None),
                    None,
                )
                if cached_v is not None:
                    row["cached_input_per_1m_usd"] = cached_v
                out[name] = row
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(blob)
    return out


def _extract_from_astro_html(html: str) -> dict[str, dict[str, float]]:
    """Pull model→{input,output} pairs from Astro's hydration markup.

    Pattern observed on developers.openai.com (May 2026):

      [0,"gpt-5.4"], …, [0,"$2.50 / 1M tokens"], …, [0,"$10.00 / 1M tokens"], …

    The ordering inside the row is consistent
    (model → input → cached input → output) for current text models. We
    pluck the first three ``$X / 1M tokens`` matches after each model name,
    within a proximity window that's tight enough to stay inside one row.
    """
    import html as _html
    import re

    decoded = _html.unescape(html)

    MODEL_RE = re.compile(
        r'\[0,"((?:gpt|o[1-9]|codex|davinci|babbage|whisper|tts|dall-e)[A-Za-z0-9.\-]*)"\]'
    )
    RATE_RE = re.compile(
        r'\$\s*(\d+(?:\.\d+)?)\s*/\s*1M\s+(?:input |output |cached |)?tokens',
        re.IGNORECASE,
    )

    out: dict[str, dict[str, float]] = {}
    for m in MODEL_RE.finditer(decoded):
        model = m.group(1)
        if model in out:
            continue
        if "embedding" in model or model.startswith(("tts-", "dall-", "whisper")):
            continue
        window = decoded[m.end() : m.end() + 800]
        rates = RATE_RE.findall(window)
        if len(rates) < 3:
            continue
        try:
            in_rate = float(rates[0])
            cached_rate = float(rates[1])
            out_rate = float(rates[2])
        except ValueError:
            continue
        if in_rate == 0.0 and out_rate == 0.0:
            continue
        out[model] = {
            "input_per_1m_usd": in_rate,
            "cached_input_per_1m_usd": cached_rate,
            "output_per_1m_usd": out_rate,
        }
    return out


def _extract_from_rendered_pricing_text(html: str) -> dict[str, dict[str, float]]:
    """Pull Standard short-context rates from rendered pricing text.

    Current developers.openai.com markup includes rows that flatten to
    strings such as::

      gpt-5.5$5.00$0.50$30.00$10.00$1.00$45.00

    The first trio is short-context ``Input / Cached input / Output``.
    """
    import html as _html
    import re

    decoded = _html.unescape(html)
    text = re.sub(r"<[^>]+>", "\n", decoded)
    text = text.replace("\xa0", " ")

    model = r"(?:gpt|o[1-9]|codex|chatgpt|computer-use)[A-Za-z0-9.\-]*"
    row_re = re.compile(
        rf"(?P<model>{model})"
        r"(?P<prices>(?:\s*(?:\$[\d.]+|-)){3,6})",
        re.IGNORECASE,
    )
    price_re = re.compile(r"\$([\d.]+)|-")

    out: dict[str, dict[str, float]] = {}
    for match in row_re.finditer(text):
        name = match.group("model")
        if _skip_openai_pricing_model(name):
            continue

        cells = [
            None if p.group(0).strip() == "-" else float(p.group(1))
            for p in price_re.finditer(match.group("prices"))
        ]
        if name in out or len(cells) < 3 or cells[0] is None or cells[2] is None:
            continue
        row = {
            "input_per_1m_usd": cells[0],
            "output_per_1m_usd": cells[2],
        }
        if cells[1] is not None:
            row["cached_input_per_1m_usd"] = cells[1]
        out[name] = row
    return out


def _skip_openai_pricing_model(model: str) -> bool:
    name = model.lower()
    return (
        "embedding" in name
        or name.startswith(("tts-", "dall-", "whisper", "sora-"))
    )


# ─────────────────────────────────────────────────────────────────────────────
# Provider: pricepertoken — third-party aggregator with a stable <table>
# ─────────────────────────────────────────────────────────────────────────────

def scrape_pricepertoken(html: str) -> dict[str, dict[str, float]]:
    """Parse pricepertoken.com's OpenAI pricing table.

    The page renders a plain HTML ``<table>`` with stable headers
    ``Model`` / ``Input`` / ``Output`` (and others we ignore). We pull
    column indices from the header row, then walk data rows.

    Returns model names lowercased + spaces→hyphens (``"GPT-5 Nano"``
    → ``"gpt-5-nano"``). The user can rename in
    ``~/.orcho/pricing.local.toml`` if their CLI uses a different alias.
    """
    import html as _html
    import re

    table_match = re.search(r'<table[^>]*>(.*?)</table>', html, re.DOTALL)
    if not table_match:
        raise PricingScrapeError(
            "no ``<table>`` found on pricepertoken page. Layout changed."
        )
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_match.group(1), re.DOTALL)
    if not rows:
        raise PricingScrapeError("table found but no ``<tr>`` rows inside.")

    def _cells(row: str) -> list[str]:
        raw = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', row, re.DOTALL)
        return [_html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in raw]

    headers = _cells(rows[0])
    try:
        model_col = next(
            i for i, h in enumerate(headers) if h.strip().lower() == "model"
        )
        input_col = next(
            i for i, h in enumerate(headers) if h.strip().lower() == "input"
        )
        output_col = next(
            i for i, h in enumerate(headers) if h.strip().lower() == "output"
        )
    except StopIteration:
        raise PricingScrapeError(
            f"table headers {headers!r} don't contain Model/Input/Output. "
            "Layout changed."
        ) from None

    def _parse_dollars(cell: str) -> float | None:
        m = re.search(r'\$\s*(\d+(?:\.\d+)?)', cell)
        if not m:
            return None
        try:
            return float(m.group(1))
        except ValueError:
            return None

    out: dict[str, dict[str, float]] = {}
    for row in rows[1:]:
        cells = _cells(row)
        if len(cells) <= max(model_col, input_col, output_col):
            continue
        name = cells[model_col].strip()
        if not name or name.lower() in {"-", "—", ""}:
            continue
        in_v = _parse_dollars(cells[input_col])
        out_v = _parse_dollars(cells[output_col])
        if in_v is None or out_v is None:
            continue
        canon = re.sub(r"\s+", "-", name.lower())
        out[canon] = {
            "input_per_1m_usd": in_v,
            "output_per_1m_usd": out_v,
        }
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Public dispatcher + validator
# ─────────────────────────────────────────────────────────────────────────────

# Provider registry. Each entry: (url, scraper_fn, provenance_label).
# Adding a third source is a function + a row here.
_PROVIDERS: dict[str, tuple[str, callable, str]] = {
    "openai": (
        "https://developers.openai.com/api/docs/pricing",
        scrape_openai_pricing,
        "developers.openai.com (official)",
    ),
    "pricepertoken": (
        "https://pricepertoken.com/pricing-page/provider/openai",
        scrape_pricepertoken,
        "pricepertoken.com (third-party aggregator — verify against official)",
    ),
}


def known_providers() -> tuple[str, ...]:
    """Names accepted by ``refresh()`` / ``--provider``."""
    return tuple(_PROVIDERS.keys())


def validate_models(models: dict[str, dict[str, float]]) -> list[str]:
    """Sanity-check parsed rates. Returns a list of human-readable
    error strings; empty list = clean. Caller decides whether to write
    or abort.

    Range bound: ``[0.01, 1000]`` per 1M tokens. A scraper glitch that
    yields ``$0.0`` or ``$10000`` for a real model is a parse mistake,
    not a real OpenAI price — refusing to write keeps a 4-orders-of-
    magnitude wrong number out of the user's local file.
    """
    errors: list[str] = []
    if not models:
        errors.append(
            "no models extracted — page structure likely changed."
        )
        return errors
    for model, rates in models.items():
        for half in (
            "input_per_1m_usd",
            "cached_input_per_1m_usd",
            "output_per_1m_usd",
        ):
            if half == "cached_input_per_1m_usd" and half not in rates:
                continue
            v = rates.get(half, 0.0)
            try:
                f = float(v)
            except (TypeError, ValueError):
                errors.append(f"{model}.{half}: non-numeric {v!r}")
                continue
            if not (0.01 <= f <= 1000.0):
                errors.append(f"{model}.{half}: out of range ({f})")
    return errors


def refresh(provider: str) -> RefreshResult:
    """Fetch + parse + validate one provider's pricing page.

    Returns a ``RefreshResult`` on success; raises ``PricingScrapeError``
    on parse failure, validation failure, or unknown provider. Network
    errors propagate as ``urllib`` exceptions — caller catches.
    """
    if provider not in _PROVIDERS:
        raise PricingScrapeError(
            f"unknown provider {provider!r}. Known: "
            f"{', '.join(known_providers())}"
        )
    url, scraper, provenance = _PROVIDERS[provider]
    html = fetch_html(url)
    models = scraper(html)
    errors = validate_models(models)
    if errors:
        raise PricingScrapeError(
            "validation failed:\n  - " + "\n  - ".join(errors)
        )
    return RefreshResult(models=models, url=url, provenance=provenance)
