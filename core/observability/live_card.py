"""M14.4.4 — Per-call live card for ``--output live`` / ``debug`` modes.

Renders one 4-line card per agent invocation summarising what the
user actually needs to see while a run is happening:

* Which phase / round finished, how long, what it cost.
* How much of the provider-side input came from cache (cache effectiveness is
  the most volatile signal — it tells the user instantly when the
  M10.5 cache-first layout stops paying off).
* How big the response was.
* How full the active model's context window is (M14.4 runtime
  pressure, surfaced with the M14.4.1 ``runtime_reported`` source
  when Claude exposes it).

The card is deliberately readable English, not a pipe-separated
shorthand. Example on a normal call::

    ✓ plan · round 1 · 1.4s · $0.012
        Provider input  8.3k tokens (91% cached, ~$0.18 saved)
        Response  620 tokens
        Live context    12k / 1M (1% full)

Anomaly markers stay inline so the structure is stable and the
eye sees the deviation without re-parsing layout::

    ✓ review · round 2 · 2.1s · $0.018
        Provider input  4.1k tokens (8% cached — prefix changed)
        Response  320 tokens
        Live context    ⚠ 870k / 1M (87% — approaching limit)

What this module does *not* do:

* It does not query the runtime. All inputs come from the
  ``_session_aware_invoke`` writer-side state at end-of-call —
  agent attributes + ``state.phase_log[trace_slot]`` siblings.
* It does not change any persisted shape. The card is print-only;
  ``session.json`` / ``evidence.json`` / ``metrics.json`` stay as
  they were.
* It does not encode policy. ``Context ⚠ approaching limit`` is a
  visual marker for the user, not a trigger. M14.4+ automatic
  compaction (when it lands) reads
  :mod:`pipeline.observability.context_pressure` records directly,
  not these print lines.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "LiveCardData",
    "format_live_card",
]

# Threshold at which the context line gets the ⚠ marker + the
# "approaching limit" hint. 0.80 matches the value users typically
# see on the underlying providers' own warning surfaces (Claude
# Code's progress bar starts colouring at similar fill ratios).
_CONTEXT_WARN_RATIO = 0.80

# Threshold below which the cache hit annotation gets the
# "prefix changed" suffix. Cache hit rates above this are the
# expected steady-state for cache-first runs; sharp drops below
# signal a prefix invariant was broken (typically a new dynamic
# part landed in the leading prefix region).
_CACHE_LOW_RATIO = 0.50

# Coverage = (cache_read + cache_creation) / tokens_in. When the provider
# exposes the three-way split, this is the honest "prefix changed" signal:
# below it, a large fraction of the prompt is genuinely fresh (uncacheable),
# meaning the cacheable prefix was invalidated. A cold/priming call keeps
# coverage near 1.0 (it writes the prefix into cache) even when the read
# ratio is low, so it must NOT be flagged as a prefix break.
_CACHE_COVERAGE_LOW_RATIO = 0.90

# Approximate API-equivalent rate the cache-read side bills at,
# expressed as a fraction of the fresh-prompt rate. Claude charges
# cache_read at ~10% of input rate, so the savings fraction is
# ~90% of what the cached portion would have cost at the fresh
# rate. The figure is intentionally approximate — the card says
# "~$X saved", not an exact number.
_CACHE_SAVINGS_FRACTION = 0.9

_CONTEXT_LABEL_WIDTH = 16


def _format_token_count(n: int) -> str:
    """Render ``8300 -> '8.3k'``, ``1_000_000 -> '1.0M'``, ``620 -> '620'``.

    Mirrors :func:`pipeline.observability.context_pressure._format_token_count`
    so live-card output and the summary line use the same
    abbreviations. Kept private to each module deliberately —
    cross-module reuse would couple two surfaces whose output
    shapes evolve independently.
    """
    if n < 1_000:
        return str(n)
    for divisor, suffix in (
        (1_000_000_000, "G"),
        (1_000_000, "M"),
        (1_000, "k"),
    ):
        if n >= divisor:
            value = n / divisor
            return f"{value:.1f}{suffix}"
    return str(n)


def _format_savings(saved_tokens: int, cost_per_token_usd: float) -> str:
    """Render a savings estimate like ``"~$0.18 saved"`` or ``""``.

    Returns empty string when the saved amount rounds to less than
    one cent — not worth surfacing, the noise hurts readability.
    The cost-per-token estimate is derived from the call's
    ``cost_usd / tokens_in`` and applied to the cached portion at
    the cache-savings fraction. Approximate by design.
    """
    if cost_per_token_usd <= 0 or saved_tokens <= 0:
        return ""
    saved_usd = saved_tokens * cost_per_token_usd * _CACHE_SAVINGS_FRACTION
    if saved_usd < 0.005:
        return ""
    return f", ~${saved_usd:.2f} saved"


@dataclass(frozen=True)
class LiveCardData:
    """All inputs the formatter needs for one card.

    Built at the end of :func:`pipeline.phases.builtin._session_aware_invoke`
    from agent attributes + the four sibling trace records
    (``prompt_render`` / ``context_growth`` / ``context_clearing``
    / ``context_pressure``) that the same writer just stamped.

    Fields are all optional except ``phase`` + ``duration_s`` —
    the formatter degrades each line gracefully when the
    corresponding signal is missing rather than printing
    ``Provider input    None tokens``.
    """

    phase: str
    duration_s: float
    round: int | None = None
    cost_usd: float | None = None
    cost_estimated: bool = False
    model: str = ""
    tokens_in: int | None = None
    tokens_out: int | None = None
    reasoning_output_tokens: int | None = None
    tokens_total: int | None = None
    prompt_tokens: int | None = None
    tool_calls: int = 0
    provider_calls: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    context_used_tokens: int | None = None
    context_window_tokens: int | None = None
    context_source: str = "unknown"
    # ``halt_reason`` flips the leading glyph to ✗ and replaces the
    # detail block. Used on parse_error / authentication_failed /
    # any other terminal-fail path.
    halt_reason: str | None = None


def format_live_card(d: LiveCardData) -> str:
    """Render the 4-line card for one agent call.

    The first line is the always-rendered header. The provider-input /
    Response / Context lines render only when meaningful data
    exists for them, so a phase that returned without any token
    accounting (rare — mock provider edge cases, halt before
    invoke) shows just the header.

    On halt, the header glyph flips to ✗ and the halt reason
    appears in the header itself; the detail lines are suppressed
    because the call did not produce them.
    """
    glyph = "✗" if d.halt_reason else "✓"
    header_parts = [f"{glyph} {d.phase}"]
    if d.round is not None:
        header_parts.append(f"round {d.round}")
    header_parts.append(f"{d.duration_s:.1f}s")
    if d.cost_usd is not None and d.cost_usd > 0:
        marker = "~$" if d.cost_estimated else "$"
        header_parts.append(f"{marker}{d.cost_usd:.3f}")
    if d.halt_reason:
        header_parts.append(f"halted — {d.halt_reason}")
    header = " · ".join(header_parts)

    if d.halt_reason:
        # Skip the detail block — the call did not produce numeric
        # observables in a meaningful state.
        return header

    lines = [header]

    # ── Orcho wire prompt line ──────────────────────────────────────────
    if d.prompt_tokens is not None and d.prompt_tokens > 0:
        lines.append(
            f"    Orcho prompt   {_format_token_count(d.prompt_tokens)} tokens",
        )

    # ── Provider input line ──────────────────────────────────────────────
    prompt_line = _prompt_line(d)
    if prompt_line is not None:
        lines.append(prompt_line)

    # ── Runtime overhead line ────────────────────────────────────────────
    # Make the gap between the Orcho-assembled prompt (parts shown in the
    # Composition block) and what the provider actually received explicit.
    # The difference is the runtime's own injection (agent system prompt +
    # tool-definition schemas) that Orcho neither builds nor controls — it
    # would otherwise be a silent delta between two footer numbers.
    overhead_line = _runtime_overhead_line(d)
    if overhead_line is not None:
        lines.append(overhead_line)

    # ── Response line ────────────────────────────────────────────────────
    if d.tokens_out is not None and d.tokens_out > 0:
        response = f"    Response  {_format_token_count(d.tokens_out)} tokens"
        if (
            d.reasoning_output_tokens is not None
            and d.reasoning_output_tokens > 0
        ):
            response += (
                f" ({_format_token_count(d.reasoning_output_tokens)} "
                "reasoning)"
            )
        lines.append(response)

    # ── Activity line ────────────────────────────────────────────────────
    activity_line = _activity_line(d)
    if activity_line is not None:
        lines.append(activity_line)

    # ── Context line ─────────────────────────────────────────────────────
    context_line = _context_line(d)
    if context_line is not None:
        lines.append(context_line)

    return "\n".join(lines)


def _prompt_line(d: LiveCardData) -> str | None:
    """Render the provider-input detail line, or ``None`` when there is
    no token signal worth surfacing.

    The cache annotation only appears when ``tokens_in`` and
    ``cache_read_tokens`` are known — otherwise we cannot compute the
    percentage honestly.

    Provider input splits into three parts: ``cache_read`` (hit, billed
    cheap), ``cache_creation`` (written into cache THIS turn — a cold/
    priming call, not a miss; the next turn reads it back), and ``fresh``
    (genuinely uncacheable). A low ``cache_read`` % alone does NOT mean the
    prefix broke — a first/cold call writes most of the prompt into the
    cache and shows a low read %. The honest "prefix changed" signal is
    low *coverage* (read+creation): when a large fraction is ``fresh``, the
    cacheable prefix really was invalidated. When ``cache_creation`` is
    unknown (codex/gemini expose only the cached count) we fall back to the
    read-ratio heuristic.
    """
    if not d.tokens_in or d.tokens_in <= 0:
        if d.tokens_total is not None and d.tokens_total > 0:
            return (
                f"    Provider total  "
                f"{_format_token_count(d.tokens_total)} tokens "
                "(split unknown)"
            )
        return None
    label = f"    Provider input  {_format_token_count(d.tokens_in)} tokens"
    if d.cache_read_tokens is None or d.tokens_in <= 0:
        return label
    read_pct = d.cache_read_tokens / d.tokens_in
    pct_int = int(round(read_pct * 100))
    # Savings estimate when we know the call cost — approximate by
    # design; the card never claims exact billing.
    saved_str = ""
    if d.cost_usd is not None and d.cost_usd > 0:
        cost_per_tok = d.cost_usd / d.tokens_in
        saved_str = _format_savings(d.cache_read_tokens, cost_per_tok)

    creation = d.cache_creation_tokens
    if creation is not None and creation >= 0:
        # Three-way split available — distinguish priming from a real break.
        coverage = (d.cache_read_tokens + creation) / d.tokens_in
        if coverage < _CACHE_COVERAGE_LOW_RATIO:
            # Large fresh remainder → the cacheable prefix really changed.
            annotation = f"({pct_int}% cached — prefix changed)"
        elif read_pct < _CACHE_LOW_RATIO:
            # Prefix intact but mostly written this turn → cold / priming.
            prime_pct = int(round(creation / d.tokens_in * 100))
            annotation = f"({pct_int}% cached, {prime_pct}% priming)"
        else:
            annotation = f"({pct_int}% cached{saved_str})"
        return f"{label} {annotation}"

    # No creation signal (codex/gemini): fall back to the read-ratio
    # heuristic — a low read % is the best "prefix changed" proxy we have.
    if read_pct < _CACHE_LOW_RATIO:
        annotation = f"({pct_int}% cached — prefix changed)"
    else:
        annotation = f"({pct_int}% cached{saved_str})"
    return f"{label} {annotation}"


def _runtime_overhead_line(d: LiveCardData) -> str | None:
    """Render the runtime-injected overhead between Orcho's prompt and the
    provider's actual input, or ``None`` when it can't be computed honestly.

    ``prompt_tokens`` is Orcho's own estimate of the wire prompt it
    assembled (the parts in the Composition block). ``tokens_in`` is what
    the provider reported receiving. The positive difference is the
    runtime's own injection — the agent CLI's system prompt plus
    tool-definition schemas — which Orcho does not build and cannot see in
    the Composition manifest. Surfacing it keeps the manifest honest:
    Orcho-owned parts are only a fraction of the real wire prompt.

    Skipped when either number is missing, or when the gap is not clearly
    positive (estimator noise / providers that already fold the system
    prompt into a number Orcho can't separate). A small slack avoids
    flagging rounding jitter as overhead.
    """
    if d.prompt_tokens is None or d.prompt_tokens <= 0:
        return None
    if d.tokens_in is None or d.tokens_in <= 0:
        return None
    overhead = d.tokens_in - d.prompt_tokens
    # Require the gap to be a meaningful fraction of the orcho prompt before
    # surfacing it — tiny deltas are estimator noise, not runtime injection.
    if overhead < max(500, d.prompt_tokens // 10):
        return None
    pct = int(round(overhead / d.tokens_in * 100))
    return (
        f"    Runtime overhead  {_format_token_count(overhead)} tokens "
        f"({pct}% of input — agent system prompt + tools, not Orcho-built)"
    )


def _activity_line(d: LiveCardData) -> str | None:
    """Render tool/provider-call multiplicity when observed."""
    parts: list[str] = []
    if d.tool_calls:
        noun = "call" if d.tool_calls == 1 else "calls"
        parts.append(f"tools={d.tool_calls} {noun}")
    if d.provider_calls:
        noun = "call" if d.provider_calls == 1 else "calls"
        parts.append(f"provider={d.provider_calls} {noun}")
    if not parts:
        return None
    return f"    Activity  {', '.join(parts)}"


def _context_line(d: LiveCardData) -> str | None:
    """Render the Context detail line, or ``None`` when there is
    no signal worth surfacing.

    Three honest shapes mirror :func:`pipeline.observability
    .context_pressure.format_context_summary`:

    * window + used → ``12k / 1M (1% full)`` (with ⚠ over 80%)
    * used only    → ``12.3k used``
    * window only / both unknown → suppressed
    """
    used = d.context_used_tokens
    window = d.context_window_tokens
    label = _context_label(d.context_source)
    if used is None and window is None:
        return None
    if isinstance(used, int) and isinstance(window, int) and window > 0:
        ratio = used / window
        pct = int(round(ratio * 100))
        if ratio >= _CONTEXT_WARN_RATIO:
            return (
                f"    {label:<{_CONTEXT_LABEL_WIDTH}}"
                f"⚠ {_format_token_count(used)} / "
                f"{_format_token_count(window)} ({pct}% — approaching limit)"
            )
        return (
            f"    {label:<{_CONTEXT_LABEL_WIDTH}}"
            f"{_format_token_count(used)} / "
            f"{_format_token_count(window)} ({pct}% full)"
        )
    if isinstance(used, int) and used > 0:
        return (
            f"    {label:<{_CONTEXT_LABEL_WIDTH}}"
            f"{_format_token_count(used)} used"
        )
    return None


def _context_label(source: str) -> str:
    """Name the provenance of the context reading in the live card."""
    if source == "runtime_reported":
        return "Live context"
    if source == "orcho_estimated":
        return "Orcho est."
    if source == "config_static":
        return "Config ctx"
    return "Context"
