"""
core/metrics.py — Pipeline run metrics (tokens, time, rounds).

Design:
  * Zero required external dependencies (stdlib by default).
  * No required pricing — token/time metrics work without dollar accounting.
  * Token estimation: tiktoken when installed for text-only local estimates;
    len(utf-8 bytes) // 4 fallback otherwise. Exactness is only claimed for
    OpenAI/Codex-like model ids.
  * MetricsCollector accumulates per-phase data during a run.
  * metrics.json written next to meta.json in output_dir.

Output format (metrics.json):
  {
    "total_tokens_in":  12400,
    "total_tokens_out": 28600,
    "total_tokens":     41000,
    "total_duration_s": 142.3,
    "total_rounds":     2,
    "total_retries":    1,
    "phases": {
      "plan":   {"tokens_in": 3200, "tokens_out": 5100, "duration_s": 12.3, "model": "..."},
      "build":  {"tokens_in": 8500, "tokens_out": 15000, "duration_s": 28.1, "model": "..."},
      "review": {"tokens_in": 700,  "tokens_out": 1200, "duration_s": 5.4, "model": "..."}
    },
    "phase_attempts": [
      {"phase": "plan", "attempt": 1, "tokens_in": 1000, ...},
      {"phase": "plan", "attempt": 2, "tokens_in": 2200, ...}
    ]
  }

Usage:
    from core.observability.metrics import MetricsCollector

    m = MetricsCollector()
    m.record_phase("plan",  prompt="...", output="...", duration_s=12.3, model="claude-opus-4-7")
    m.record_phase("build", prompt="...", output="...", duration_s=28.1, model="claude-sonnet-4-6")
    m.add_round()
    m.save(output_dir)
    print(m.summary_line())  # "Tokens: 41,000 (in=12,400 out=28,600) | Time: 142.3s | Rounds: 2"
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.infra import config
from core.observability.accounting_display import (
    format_cost_reference,
    format_cost_reference_summary,
)

# ─────────────────────────────────────────────────────────────────────────────
# Token estimation
# ─────────────────────────────────────────────────────────────────────────────

_BYTE_HEURISTIC_SOURCE = "byte_heuristic"
_TIKTOKEN_FALLBACK_ENCODING = "o200k_base"
_ACCOUNTING_KEYS = {
    "cost_estimated",
    "cost_usd",
    "cost_usd_equivalent",
    "total_cost_usd_equivalent",
}


def accounting_enabled() -> bool:
    """Return true when dollar-denominated accounting is explicitly enabled."""
    return config.accounting_enabled()


def scrub_accounting_fields(value: Any) -> Any:
    """Return *value* without dollar-denominated accounting fields."""
    if isinstance(value, dict):
        return {
            key: scrub_accounting_fields(item)
            for key, item in value.items()
            if key not in _ACCOUNTING_KEYS
        }
    if isinstance(value, list):
        return [scrub_accounting_fields(item) for item in value]
    return value


def _merge_subtask_usage_record(
    prior: dict[str, Any], new: dict[str, Any],
) -> dict[str, Any]:
    """Fold two per-subtask usage records for the same ``subtask_id`` into one.

    Used by :meth:`MetricsCollector.record_subtask_usage` so a partial
    ``implement_retry`` resume (which re-emits only the rerun subtasks)
    ACCUMULATES onto the already-persisted record instead of replacing the
    whole phase list and dropping the untouched subtasks. The fold mirrors how
    ``phase_attempts`` accumulate across resume, so ``sum(subtasks)`` keeps
    reconciling with the cumulative ``phases.implement`` rollup.

    Schema-agnostic by field TYPE, so it does not couple this module to the
    subtask record shape owned by ``subtask_dag``:

    * numeric (``int``/``float``, not ``bool``) → summed
      (``tokens_*``/``total_tokens``/``tool_calls``/``duration_s``/
      ``cost_usd_equivalent``/``invocations`` accumulate across attempts);
    * ``bool`` → logical AND (``tokens_exact`` stays true only if every
      contributing attempt was exact);
    * anything else (``runtime``/``model``/``state``/``declared_files``) →
      latest value wins (the rerun's final ``state`` is authoritative).

    ``subtask_id`` is never altered. A key present in only one record is kept
    as-is.
    """
    out = dict(prior)
    for key, value in new.items():
        if key == "subtask_id":
            continue
        existing = out.get(key)
        if isinstance(value, bool) and isinstance(existing, bool):
            out[key] = existing and value
        elif (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and isinstance(existing, (int, float)) and not isinstance(existing, bool)
        ):
            out[key] = existing + value
        else:
            out[key] = value
    return out


@dataclass(frozen=True)
class TokenEstimate:
    """Token-count estimate plus the counter that produced it.

    ``exact`` means exact for the local tokenizer input, not provider usage
    truth for a full structured request. Runtime usage remains authoritative.
    """

    tokens: int
    source: str
    exact: bool = False


def estimate_tokens(text: str) -> int:
    """Backward-compatible byte-heuristic estimate.

    Uses 4 bytes-per-token heuristic (OpenAI/Anthropic standard rule of thumb).
    Accurate only as a coarse fallback. Returns 0 for empty input.
    """
    return estimate_tokens_with_source(text).tokens


def estimate_model_tokens(text: str, *, model: str = "") -> int:
    """Estimate tokens for *text*, preferring a model tokenizer when available."""
    return estimate_tokens_with_source(text, model=model).tokens


def estimate_tokens_with_source(text: str, *, model: str = "") -> TokenEstimate:
    """Estimate token count and report the source used.

    Use optional ``tiktoken`` for text-only local estimates when installed.
    If ``tiktoken.encoding_for_model(model)`` resolves directly for an
    OpenAI/Codex-like model id, mark the estimate exact for the local
    tokenizer input. Unknown OpenAI/Codex-like ids fall back to
    ``o200k_base`` with ``exact=False``. Claude/Gemini/unknown models also
    use ``tiktoken`` as a better heuristic than bytes/4, but stay
    ``exact=False`` because the provider tokenizer differs.
    """
    if not text:
        return TokenEstimate(0, _BYTE_HEURISTIC_SOURCE)

    encoded = _estimate_with_tiktoken(text, model)
    if encoded is not None:
        return encoded

    return TokenEstimate(
        max(1, len(text.encode("utf-8")) // 4),
        _BYTE_HEURISTIC_SOURCE,
    )


def _is_openai_like_model(model: str) -> bool:
    """Return true for model ids that are plausibly OpenAI/Codex-tokenized."""
    m = (model or "").strip().lower()
    if not m:
        return False
    if m.startswith(("gpt-", "chatgpt-", "text-", "davinci", "curie", "babbage", "ada")):
        return True
    if m.startswith(("o1", "o2", "o3", "o4")):
        return True
    return "codex" in m and not m.startswith("claude")


def _estimate_with_tiktoken(text: str, model: str) -> TokenEstimate | None:
    try:
        encoding, source, direct = _tiktoken_encoding(model)
        exact = direct and _is_openai_like_model(model)
        if not direct and _is_openai_like_model(model):
            source = f"{source}:fallback"
        elif not exact:
            source = f"{source}:heuristic"
        return TokenEstimate(max(1, len(encoding.encode(text))), source, exact=exact)
    except Exception:
        # ``tiktoken`` may lazily fetch encoding blobs on first use. Token
        # estimation must stay offline/best-effort, so any tokenizer failure
        # falls back to the byte heuristic.
        return None


@lru_cache(maxsize=32)
def _tiktoken_encoding(model: str) -> tuple[Any, str, bool]:
    import tiktoken  # type: ignore[import-not-found]

    try:
        encoding = tiktoken.encoding_for_model(model)
        source = f"tiktoken:{getattr(encoding, 'name', model)}"
        direct = True
    except KeyError:
        encoding = tiktoken.get_encoding(_TIKTOKEN_FALLBACK_ENCODING)
        source = f"tiktoken:{_TIKTOKEN_FALLBACK_ENCODING}"
        direct = False
    return encoding, source, direct


def _clear_tokenizer_cache_for_tests() -> None:
    """Clear cached optional-tokenizer state for monkeypatched tests."""
    _tiktoken_encoding.cache_clear()


# ─────────────────────────────────────────────────────────────────────────────
# Per-phase record
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PhaseMetrics:
    """Metrics for a single pipeline phase.

    ``cost_usd_equivalent`` is a dollar-denominated cost reference, not a
    billing receipt. When ``cost_estimated`` is false, the value came from the
    underlying runtime/endpoint (for example ``total_cost_usd`` in stream-json).
    When the runtime reports tokens but not cost, Orcho may fill this from the
    local pricing table and mark ``cost_estimated=True``.
    """

    phase: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_unknown: int = 0
    tokens_in_cache_read: int = 0
    tokens_in_cache_create: int = 0
    duration_s: float = 0.0
    attempt: int = 1
    tool_calls: int = 0
    retries: int = 0
    cost_usd_equivalent: float | None = None
    cost_estimated: bool = False
    # True when ``tokens_in/out`` came from API headers (Claude) or the
    # CLI's own usage trailer (Codex's ``tokens used``). False when we
    # fell back to ``estimate_tokens()`` on prompt/output (best-effort
    # heuristic). Surfaced in CLI output so a reader can tell measured
    # numbers from estimates without having to know which CLIs report
    # what.
    tokens_exact: bool = False

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out + self.tokens_unknown

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "model":      self.model,
            "tokens_in":  self.tokens_in,
            "tokens_out": self.tokens_out,
            "total_tokens": self.total_tokens,
            "duration_s": round(self.duration_s, 3),
        }
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tokens_unknown:
            d["tokens_unknown"] = self.tokens_unknown
        if self.tokens_in_cache_read:
            d["tokens_in_cache_read"] = self.tokens_in_cache_read
        if self.tokens_in_cache_create:
            d["tokens_in_cache_create"] = self.tokens_in_cache_create
        if self.retries:
            d["retries"] = self.retries
        if accounting_enabled() and self.cost_usd_equivalent is not None:
            d["cost_usd_equivalent"] = round(self.cost_usd_equivalent, 4)
            d["cost_estimated"] = self.cost_estimated
        # Always emit ``tokens_exact`` so consumers (orcho cost, dashboards)
        # can disambiguate measured from estimated without re-deriving
        # the rule. False is the conservative default for legacy entries
        # written before this field existed.
        d["tokens_exact"] = self.tokens_exact
        return d

    def as_attempt_dict(self) -> dict[str, Any]:
        """Return this record as a per-attempt metrics entry."""
        return {
            "phase": self.phase,
            "attempt": self.attempt,
            **self.as_dict(),
        }


def _resolve_phase_cost_usd_equivalent(
    *,
    cost_usd: float | None,
    model: str,
    tokens_in: int,
    tokens_out: int,
    tokens_unknown: int,
    tokens_in_cache_read: int,
    tokens_exact: bool,
) -> tuple[float | None, bool]:
    """Return ``(api_equivalent_cost, estimated)`` for a phase.

    Provider-reported cost remains authoritative. When accounting is enabled
    and the provider reported exact token usage but no cost, estimate the
    cost reference from the local pricing table. Heuristic token counts
    intentionally stay unpriced so the DONE summary never turns byte estimates
    into dollar-looking facts.
    """
    if not accounting_enabled():
        return None, False
    if cost_usd is not None:
        return float(cost_usd), False
    if not tokens_exact or not model:
        return None, False

    total_tokens = max(0, tokens_in) + max(0, tokens_out) + max(0, tokens_unknown)
    if total_tokens <= 0:
        return None, False

    from core.observability import pricing

    if tokens_in or tokens_out:
        estimated = pricing.estimate_cost_usd(
            model,
            tokens_in=max(0, tokens_in),
            cached_tokens_in=max(0, tokens_in_cache_read),
            # Unknown provider usage is usually reasoning/cache/other usage.
            # Count it on the output side rather than dropping it.
            tokens_out=max(0, tokens_out) + max(0, tokens_unknown),
        )
    else:
        estimated = pricing.estimate_cost_from_total(model, total_tokens)
    if estimated is None:
        return None, False
    return float(estimated), True


# ─────────────────────────────────────────────────────────────────────────────
# MetricsCollector
# ─────────────────────────────────────────────────────────────────────────────

class MetricsCollector:
    """Accumulates per-phase metrics during a pipeline run.

    Args:
        default_model: Fallback model name when record_phase() is called
                       without an explicit model argument.
    """

    def __init__(
        self,
        default_model: str = "",
        *,
        plan_model: str = "",
        implement_model: str = "",
        review_model: str = "",
    ) -> None:
        # default_model is used as fallback when record_phase() has no model arg.
        # Per-phase models are stored for use in summary/export.
        self._phases: list[PhaseMetrics] = []
        self._default_model = default_model or implement_model or plan_model or review_model
        self._plan_model = plan_model
        self._implement_model = implement_model
        self._review_model = review_model
        self._rounds: int = 0
        self._total_retries: int = 0
        # Additive, observe-only per-subtask usage breakdown, keyed by phase
        # (e.g. ``"implement"``). Each value is the durable record list the
        # subtask_dag path stamped on its phase log. These records EXPLAIN a
        # phase total; they are never folded into ``total_*`` / ``phases`` (no
        # double counting). Absent for whole_plan / non-subtask runs.
        self._subtask_usage: dict[str, list[dict[str, Any]]] = {}
        # Additive, observe-only handoff-advice usage attribution. A flat dict
        # of primitive token/cost fields the UPPER layer derives from durable
        # advice artifacts (the advisor runs outside the FSM phase loop, so its
        # usage is NOT a pipeline phase). Surfaced under the additive
        # ``handoff_advice`` key in :meth:`as_dict`; NEVER folded into
        # ``total_*`` / ``phases`` — the run totals stay authoritative and the
        # advice usage never double-counts. Empty until the upper layer records
        # it (whole_plan / no-advice runs keep the historical shape). ``core``
        # stays unaware of where the numbers came from — it only sees primitives.
        self._advice_usage: dict[str, Any] = {}

    # ── Recording ─────────────────────────────────────────────────────────────

    def record_phase(
        self,
        phase: str,
        *,
        prompt: str = "",
        output: str = "",
        duration_s: float = 0.0,
        model: str = "",
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        tokens_total: int | None = None,
        attempt: int | None = None,
        tool_calls: int = 0,
        retries: int = 0,
        cost_usd: float | None = None,
        tokens_exact: bool | None = None,
        reconcile_total: bool = False,
        tokens_in_cache_read: int | None = None,
        tokens_in_cache_create: int | None = None,
    ) -> PhaseMetrics:
        """Record metrics for a completed pipeline phase.

        Args:
            phase:        Phase name ("plan", "build", "review", "fix", …).
            prompt:       Input text — used for estimation when ``tokens_in``
                          / ``tokens_total`` aren't provided.
            output:       Output text — same role for ``tokens_out``.
            duration_s:   Wall-clock seconds for this phase.
            model:        Model name (falls back to default_model if empty).
            tokens_in:    Exact input-token count from API headers
                          (Claude). ``None`` triggers ``estimate_tokens``
                          on ``prompt``.
            tokens_out:   Exact output-token count from API headers
                          (Claude). ``None`` triggers ``estimate_tokens``
                          on ``output``.
            tokens_total: Total token count when the provider doesn't
                          split in/out (codex CLI prints a single
                          ``tokens used`` trailer). When this is set and
                          ``tokens_in/out`` are None, we record the total
                          as ``tokens_unknown`` while leaving
                          ``tokens_in/out`` at zero, so summaries don't
                          mislabel aggregate provider usage as output.
            attempt:      1-based phase attempt. Defaults to the next
                          occurrence of this phase name.
            tool_calls:   Built-in/MCP tool calls observed during this
                          phase invocation. Zero when unknown or none.
            retries:      Retry attempts in this phase.
            cost_usd:     Runtime-reported cost reference (for example
                          ``total_cost_usd``). ``None`` when not reported.
            tokens_in_cache_read: Provider cache-read input token subset.
                          Used for cache-aware cost estimates when
                          the provider does not report native cost.
            tokens_in_cache_create: Provider cache-create input token subset.
                          Stored for reporting; priced as normal input unless
                          the provider reports native cost.
            tokens_exact: Optional override for the derived ``exact`` flag.
                          When ``None`` the flag is inferred as before. Useful
                          when the caller feeds an estimated number (e.g. a
                          wire estimate) as ``tokens_total`` / ``tokens_out``
                          but wants the record explicitly marked NOT exact —
                          pass ``False`` to override the otherwise-True
                          total-only branch.
            reconcile_total: When ``True`` and ``tokens_total`` is given
                          alongside at least one split side, treat the provider
                          total as authoritative: known provider split sides
                          are kept verbatim, a missing side is taken as ``0``
                          (never estimated from text), and the remainder up to
                          ``tokens_total`` is folded into ``tokens_unknown`` so
                          ``total_tokens == tokens_total`` (Codex/Gemini report
                          a normalized total that can exceed/replace the split,
                          e.g. reasoning tokens or a partial split). Default
                          ``False`` keeps the historical behavior — the missing
                          side is estimated and the total is ignored when a
                          split is present.
        """
        resolved_model = model or self._default_model
        # Track whether the token count is measured (came from a CLI /
        # API source) or estimated (estimate_tokens fallback on text
        # length). orcho cost surfaces ``~`` for the estimated case so
        # a reader can see at a glance which numbers to trust.
        if tokens_in is None and tokens_out is None and tokens_total is not None:
            in_tok  = 0
            out_tok = 0
            unknown_tok = int(tokens_total)
            exact = True
        elif reconcile_total and tokens_total is not None:
            # Provider total is authoritative. Keep the known provider split
            # side(s) verbatim and treat a missing side as 0 — never estimate
            # it from prompt/output, which would inflate ``total_tokens`` past
            # the provider number. The remainder up to ``tokens_total`` lands
            # in ``tokens_unknown`` so ``total_tokens == tokens_total``.
            in_tok  = tokens_in if tokens_in is not None else 0
            out_tok = tokens_out if tokens_out is not None else 0
            unknown_tok = max(0, int(tokens_total) - in_tok - out_tok)
            exact = True
        elif tokens_in is not None or tokens_out is not None:
            in_tok = (
                tokens_in
                if tokens_in is not None
                else estimate_model_tokens(prompt, model=resolved_model)
            )
            out_tok = (
                tokens_out
                if tokens_out is not None
                else estimate_model_tokens(output, model=resolved_model)
            )
            unknown_tok = 0
            # If at least one side came from the caller, treat as exact —
            # the caller had API usage in hand.
            exact = True
        else:
            in_tok = estimate_model_tokens(prompt, model=resolved_model)
            out_tok = estimate_model_tokens(output, model=resolved_model)
            unknown_tok = 0
            exact = False

        if tokens_exact is not None:
            exact = tokens_exact
        cache_read_tok = max(0, int(tokens_in_cache_read or 0))
        cache_create_tok = max(0, int(tokens_in_cache_create or 0))

        resolved_cost_usd, cost_estimated = _resolve_phase_cost_usd_equivalent(
            cost_usd=cost_usd,
            model=resolved_model,
            tokens_in=in_tok,
            tokens_out=out_tok,
            tokens_unknown=unknown_tok,
            tokens_in_cache_read=cache_read_tok,
            tokens_exact=exact,
        )

        pm = PhaseMetrics(
            phase=phase,
            model=resolved_model,
            tokens_in=in_tok,
            tokens_out=out_tok,
            tokens_unknown=unknown_tok,
            tokens_in_cache_read=cache_read_tok,
            tokens_in_cache_create=cache_create_tok,
            duration_s=duration_s,
            attempt=attempt or self._next_attempt_for(phase),
            tool_calls=max(0, int(tool_calls or 0)),
            retries=retries,
            cost_usd_equivalent=resolved_cost_usd,
            cost_estimated=cost_estimated,
            tokens_exact=exact,
        )
        self._phases.append(pm)
        self._total_retries += retries
        return pm

    def record_subtask_usage(
        self, phase: str, records: list[dict[str, Any]],
    ) -> None:
        """Merge an observe-only per-subtask usage breakdown into ``phase``.

        Merge — NOT replace — by ``subtask_id``: a record for a subtask_id
        already present (e.g. rehydrated by :meth:`load_from_disk` after a
        handoff pause) is ACCUMULATED via :func:`_merge_subtask_usage_record`,
        and ids absent from ``records`` are preserved. This is what makes a
        partial ``implement_retry`` resume — where ``subtask_dag`` re-emits
        only the rerun subtasks — keep the previously-persisted subtasks and
        stay reconciled with the cumulative ``phases.implement`` rollup
        (which likewise accumulates the pre-pause + retry attempts). New ids
        are appended in arrival order after the existing ones. Records are
        copied so later caller mutation cannot bleed into the snapshot.
        Surfaced under the additive ``subtasks`` key in :meth:`as_dict`;
        never folded into ``total_*`` or ``phases``.
        """
        if not phase or not records:
            return
        existing = self._subtask_usage.get(phase, [])
        by_id: dict[Any, dict[str, Any]] = {}
        order: list[Any] = []
        for rec in existing:
            sid = rec.get("subtask_id")
            by_id[sid] = dict(rec)
            order.append(sid)
        for rec in records:
            sid = rec.get("subtask_id")
            if sid in by_id:
                by_id[sid] = _merge_subtask_usage_record(by_id[sid], rec)
            else:
                by_id[sid] = dict(rec)
                order.append(sid)
        self._subtask_usage[phase] = [by_id[sid] for sid in order]

    def record_advice_usage(self, usage: Mapping[str, Any]) -> None:
        """Record observe-only handoff-advice usage attribution.

        Mirrors :meth:`record_subtask_usage`: the value is surfaced under the
        additive ``handoff_advice`` key in :meth:`as_dict` and is NEVER folded
        into ``total_*`` / ``phases`` — the run totals stay authoritative, so
        advice usage never double-counts.

        REPLACE semantics (not merge): the caller (the upper ``pipeline.project``
        layer) re-derives the full advice-usage aggregate from the durable advice
        artifacts on each call, so the latest call holds the complete picture.
        Accepts only a mapping of primitive token/cost fields — non-mapping,
        empty, or non-primitive entries are ignored — so ``core`` never learns
        anything about ``pipeline.project``. Usage unavailable → record nothing
        (the slot stays absent rather than emitting a misleading zero).
        """
        if not isinstance(usage, Mapping) or not usage:
            return
        cleaned: dict[str, Any] = {}
        for key, value in usage.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float, str)):
                cleaned[str(key)] = value
        if cleaned:
            self._advice_usage = cleaned

    def add_round(self, count: int = 1) -> None:
        """Increment fix-round counter."""
        self._rounds += count

    # ── Aggregates ────────────────────────────────────────────────────────────

    @property
    def total_tokens_in(self) -> int:
        return sum(p.tokens_in for p in self._phases)

    @property
    def total_tokens_out(self) -> int:
        return sum(p.tokens_out for p in self._phases)

    @property
    def total_tokens_unknown(self) -> int:
        return sum(p.tokens_unknown for p in self._phases)

    @property
    def total_tokens(self) -> int:
        return (
            self.total_tokens_in
            + self.total_tokens_out
            + self.total_tokens_unknown
        )

    @property
    def total_duration_s(self) -> float:
        return sum(p.duration_s for p in self._phases)

    @property
    def total_cost_usd_equivalent(self) -> float:
        """Sum of per-phase cost-reference values.

        Native provider costs and local pricing-table estimates both
        contribute. Phases with no provider cost and no known model price
        contribute 0.
        """
        return sum(p.cost_usd_equivalent or 0.0 for p in self._phases)

    @property
    def total_cost_estimated(self) -> bool:
        """Return true when any included cost reference is estimated."""
        return any(
            p.cost_usd_equivalent is not None and p.cost_estimated
            for p in self._phases
        )

    @property
    def total_rounds(self) -> int:
        return self._rounds

    @property
    def total_retries(self) -> int:
        return self._total_retries

    @property
    def phases(self) -> list[PhaseMetrics]:
        return list(self._phases)

    def _next_attempt_for(self, phase: str) -> int:
        return 1 + sum(p.phase == phase for p in self._phases)

    # ── Serialization ─────────────────────────────────────────────────────────

    def as_dict(self) -> dict[str, Any]:
        """Return the full metrics dict matching metrics.json schema."""
        d: dict[str, Any] = {
            "total_tokens_in":  self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "total_tokens_unknown": self.total_tokens_unknown,
            "total_tokens":     self.total_tokens,
            "total_duration_s": round(self.total_duration_s, 3),
            "phases": self._phase_rollup_dict(),
            "phase_attempts": [
                p.as_attempt_dict() for p in self._phases
            ],
        }
        if self._rounds:
            d["total_rounds"] = self._rounds
        if self._total_retries:
            d["total_retries"] = self._total_retries
        # Cost reference (native runtime cost or local pricing-table
        # estimate). Only surfaced when at least one phase has a cost —
        # otherwise users would see ``$0.00`` and assume the call was free,
        # which is the opposite of what the field means.
        if accounting_enabled() and any(
            p.cost_usd_equivalent is not None for p in self._phases
        ):
            d["total_cost_usd_equivalent"] = round(self.total_cost_usd_equivalent, 4)
            d["cost_estimated"] = self.total_cost_estimated
        # Additive per-subtask breakdown. Only present when records exist, so
        # whole_plan / non-subtask runs keep the historical shape. Cost fields
        # are gated by the same accounting switch as ``total_cost_*`` above —
        # when accounting is off, ``scrub_accounting_fields`` strips
        # ``cost_usd_equivalent`` / ``cost_estimated`` from each record.
        if self._subtask_usage:
            subtasks: dict[str, Any] = {
                phase: [dict(r) for r in records]
                for phase, records in self._subtask_usage.items()
            }
            if not accounting_enabled():
                subtasks = scrub_accounting_fields(subtasks)
            d["subtasks"] = subtasks
        # Additive, observe-only handoff-advice usage attribution. Present only
        # when the upper layer recorded it; NEVER part of ``total_*``. Cost is
        # gated by the same accounting switch as the other dollar fields — when
        # accounting is off, ``scrub_accounting_fields`` strips the cost field
        # so a heuristic / un-priced run never shows a dollar-looking number.
        if self._advice_usage:
            advice = dict(self._advice_usage)
            if not accounting_enabled():
                advice = scrub_accounting_fields(advice)
            d["handoff_advice"] = advice
        return d

    def _phase_rollup_dict(self) -> dict[str, Any]:
        """Return per-phase rollups without losing repeated attempts."""
        out: dict[str, Any] = {}
        for p in self._phases:
            current = out.get(p.phase)
            if current is None:
                row = p.as_dict()
                row["attempts"] = 1
                out[p.phase] = row
                continue

            current["tokens_in"] += p.tokens_in
            current["tokens_out"] += p.tokens_out
            current["total_tokens"] += p.total_tokens
            current["duration_s"] = round(
                float(current.get("duration_s") or 0.0) + p.duration_s,
                3,
            )
            current["attempts"] = int(current.get("attempts") or 1) + 1
            if p.tokens_unknown:
                current["tokens_unknown"] = (
                    int(current.get("tokens_unknown") or 0)
                    + p.tokens_unknown
                )
            if p.tokens_in_cache_read:
                current["tokens_in_cache_read"] = (
                    int(current.get("tokens_in_cache_read") or 0)
                    + p.tokens_in_cache_read
                )
            if p.tokens_in_cache_create:
                current["tokens_in_cache_create"] = (
                    int(current.get("tokens_in_cache_create") or 0)
                    + p.tokens_in_cache_create
                )
            if p.tool_calls:
                current["tool_calls"] = (
                    int(current.get("tool_calls") or 0)
                    + p.tool_calls
                )
            if p.retries:
                current["retries"] = (
                    int(current.get("retries") or 0)
                    + p.retries
                )
            if accounting_enabled() and p.cost_usd_equivalent is not None:
                current["cost_usd_equivalent"] = round(
                    float(current.get("cost_usd_equivalent") or 0.0)
                    + p.cost_usd_equivalent,
                    4,
                )
                current["cost_estimated"] = (
                    bool(current.get("cost_estimated")) or p.cost_estimated
                )
            current["tokens_exact"] = (
                bool(current.get("tokens_exact")) and p.tokens_exact
            )
            if current.get("model") != p.model:
                current["model"] = "mixed"
        return out

    def save(self, output_dir: Path | str) -> Path:
        """Write metrics.json to output_dir. Creates dir if needed."""
        d = Path(output_dir)
        d.mkdir(parents=True, exist_ok=True)
        f = d / "metrics.json"
        f.write_text(
            json.dumps(self.as_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return f

    def load_from_disk(self, path: Path | str) -> int:
        """Rehydrate ``_phases`` / ``_rounds`` / ``_total_retries`` from a
        previously-saved ``metrics.json``. Intended for resume after a
        pipeline subprocess restart, where the fresh subprocess starts
        with an empty accumulator and would otherwise overwrite the
        prior subprocess's metrics on finalize (losing every attempt
        recorded before the pause).

        Idempotent against re-call only when the source file does not
        change — caller is expected to invoke this exactly once during
        resume init.

        Returns the number of ``PhaseMetrics`` entries appended. ``0``
        means the file was missing, unreadable, malformed, or contained
        an empty ``phase_attempts`` list — none of those raise; the
        collector is left in whatever state it was in before the call.
        """
        p = Path(path)
        if not p.is_file():
            return 0
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        if not isinstance(data, dict):
            return 0
        attempts = data.get("phase_attempts")
        if not isinstance(attempts, list):
            return 0
        loaded = 0
        for entry in attempts:
            if not isinstance(entry, dict):
                continue
            phase = entry.get("phase")
            if not isinstance(phase, str) or not phase:
                continue
            # Defensive int/float coercion: a malformed source field
            # must not crash resume init. Unknown / wrong-typed values
            # default to the PhaseMetrics field defaults.
            def _i(v: Any, default: int = 0) -> int:
                return int(v) if isinstance(v, int) and not isinstance(v, bool) else default
            def _f(v: Any, default: float = 0.0) -> float:
                if isinstance(v, bool):
                    return default
                return float(v) if isinstance(v, (int, float)) else default
            cost = entry.get("cost_usd_equivalent") if accounting_enabled() else None
            self._phases.append(PhaseMetrics(
                phase=phase,
                model=str(entry.get("model", "")),
                tokens_in=_i(entry.get("tokens_in")),
                tokens_out=_i(entry.get("tokens_out")),
                tokens_unknown=_i(entry.get("tokens_unknown")),
                tokens_in_cache_read=_i(entry.get("tokens_in_cache_read")),
                tokens_in_cache_create=_i(entry.get("tokens_in_cache_create")),
                duration_s=_f(entry.get("duration_s")),
                attempt=_i(entry.get("attempt"), default=1) or 1,
                tool_calls=_i(entry.get("tool_calls")),
                retries=_i(entry.get("retries")),
                cost_usd_equivalent=(
                    _f(cost) if isinstance(cost, (int, float))
                    and not isinstance(cost, bool) else None
                ),
                cost_estimated=bool(entry.get("cost_estimated")),
                tokens_exact=bool(entry.get("tokens_exact")),
            ))
            self._total_retries += _i(entry.get("retries"))
            loaded += 1
        # ``total_rounds`` is omitted from the saved dict when 0
        # (``_rounds`` was never incremented). Treat absence as 0
        # rather than as an error.
        rounds = data.get("total_rounds")
        if isinstance(rounds, int) and not isinstance(rounds, bool):
            self._rounds += rounds
        # Rehydrate the additive per-subtask breakdown. Critical for resume:
        # ``handoff.py`` saves ``metrics.json`` on a pause, and the resume
        # subprocess starts with an empty accumulator. Without this, the final
        # save after resume would drop the breakdown captured pre-pause. Purely
        # defensive — only ``dict[str, list[dict]]`` shapes survive; any other
        # value is silently ignored so a malformed file never crashes resume.
        raw_subtasks = data.get("subtasks")
        if isinstance(raw_subtasks, dict):
            for phase, recs in raw_subtasks.items():
                if not isinstance(phase, str) or not phase:
                    continue
                if not isinstance(recs, list):
                    continue
                clean = [dict(r) for r in recs if isinstance(r, dict)]
                if clean:
                    self._subtask_usage[phase] = clean
        # Rehydrate the additive, observe-only handoff-advice usage slot so a
        # save-after-resume keeps it even if no further phase-end re-derives it.
        # Only primitive token/cost fields survive; any other shape is ignored.
        raw_advice = data.get("handoff_advice")
        if isinstance(raw_advice, dict):
            advice: dict[str, Any] = {}
            for key, value in raw_advice.items():
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float, str)):
                    advice[str(key)] = value
            if advice:
                self._advice_usage = advice
        return loaded

    # ── Human-readable summaries ──────────────────────────────────────────────

    def summary_line(self) -> str:
        """One-liner for CLI output at the end of a run."""
        parts = [
            f"Tokens: {self.total_tokens:,} "
            f"({self._summary_token_split()})",
            f"Time: {self.total_duration_s:.1f}s",
        ]
        if accounting_enabled() and any(
            p.cost_usd_equivalent is not None for p in self._phases
        ):
            parts.append(
                format_cost_reference_summary(
                    self.total_cost_usd_equivalent,
                    estimated=self.total_cost_estimated,
                )
            )
        if self._rounds:
            parts.append(f"Rounds: {self._rounds}")
        if self._total_retries:
            parts.append(f"Retries: {self._total_retries}")
        return " | ".join(parts)

    def _summary_token_split(self) -> str:
        fields = [
            f"in={self.total_tokens_in:,}",
            f"out={self.total_tokens_out:,}",
        ]
        if self.total_tokens_unknown:
            fields.append(f"unknown={self.total_tokens_unknown:,}")
        return " ".join(fields)

    def summary_table(self) -> str:
        """Multi-line table for --show-metrics display."""
        if not self._phases:
            return "No phases recorded."

        lines = [
            f"{'Phase':<12} {'Model':<26} {'In':>8} {'Out':>8} "
            f"{'Unknown':>8} {'Total':>8} {'Time':>8} {'Tools':>7} "
            f"{'Retries':>7}",
            "-" * 99,
        ]
        for p in self._phases:
            retries = str(p.retries) if p.retries else "-"
            lines.append(
                f"{p.phase:<12} {p.model:<26} "
                f"{p.tokens_in:>8,} {p.tokens_out:>8,} "
                f"{p.tokens_unknown:>8,} {p.total_tokens:>8,} "
                f"{p.duration_s:>7.1f}s {p.tool_calls or '-':>7} "
                f"{retries:>7}"
            )
        lines.append("-" * 99)
        retries_total = str(self._total_retries) if self._total_retries else "-"
        total_tool_calls = sum(p.tool_calls for p in self._phases)
        lines.append(
            f"{'TOTAL':<12} {'':<26} "
            f"{self.total_tokens_in:>8,} {self.total_tokens_out:>8,} "
            f"{self.total_tokens_unknown:>8,} {self.total_tokens:>8,} "
            f"{self.total_duration_s:>7.1f}s {total_tool_calls or '-':>7} "
            f"{retries_total:>7}"
        )
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Cross-project rollup
# ─────────────────────────────────────────────────────────────────────────────

def cross_summary_table(
    per_project: dict[str, dict[str, Any]],
    cross_phases: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Render the aggregate metrics table for a cross run.

    Two sections + a grand total:
      Sub-pipelines       rows from ``per_project`` (one per alias)
      Cross-level phases  rows from ``cross_phases`` (cross_hypothesis,
                          cross_plan, cross_validate_plan, contract_check)
      TOTAL               sum of both — the full token usage

    ``cross_phases`` is None-safe; either side may be empty. ``Calls``
    column replaces the legacy ``Rounds`` column so cross-level rollups
    (multi-call: hypothesis attempts ×2, contract_check per-alias) fit
    the same slot.
    """
    cross_phases = cross_phases or {}
    if not per_project and not cross_phases:
        return "No metrics recorded."

    def _sub(m: dict) -> tuple[int, int, int, float, int, float | None, bool]:
        tin   = int(m.get("total_tokens_in")  or 0)
        tout  = int(m.get("total_tokens_out") or 0)
        ttot  = int(m.get("total_tokens")     or (tin + tout))
        tdur  = float(m.get("total_duration_s") or 0.0)
        trnd  = int(m.get("total_rounds")     or 0)
        tcost = m.get("total_cost_usd_equivalent")
        return (
            tin,
            tout,
            ttot,
            tdur,
            trnd,
            (float(tcost) if tcost is not None else None),
            bool(m.get("cost_estimated")),
        )

    def _cross(m: dict) -> tuple[int, int, int, float, int, float | None, bool]:
        tin  = int(m.get("tokens_in")  or 0)
        tout = int(m.get("tokens_out") or 0)
        ttot = int(m.get("total_tokens") or (tin + tout))
        tdur = float(m.get("duration_s") or 0.0)
        calls = int(m.get("calls") or 0)
        tcost = m.get("cost_usd_equivalent")
        return (
            tin,
            tout,
            ttot,
            tdur,
            calls,
            (float(tcost) if tcost is not None else None),
            bool(m.get("cost_estimated")),
        )

    sub_rows = [(alias, *_sub(m or {})) for alias, m in per_project.items()]
    cross_rows = [(name, *_cross(m or {})) for name, m in cross_phases.items()]

    use_accounting = accounting_enabled()
    any_cost = use_accounting and (
        any(r[6] is not None for r in sub_rows)
        or any(r[6] is not None for r in cross_rows)
    )

    header = f"{'Phase':<24} {'In':>10} {'Out':>10} {'Total':>10} {'Time':>9} {'Calls':>7}"
    if any_cost:
        header += f" {'Cost ref':>24}"
    width = max(len(header), 70)
    lines = [header, "-" * width]

    sum_in = sum_out = sum_tot = 0
    sum_dur = 0.0
    sum_cost = 0.0

    def _emit(label: str, tin: int, tout: int, ttot: int,
              tdur: float, count: int, tcost: float | None,
              cost_estimated: bool) -> None:
        nonlocal sum_in, sum_out, sum_tot, sum_dur, sum_cost
        count_s = str(count) if count else "-"
        line = (
            f"{label:<24} {tin:>10,} {tout:>10,} {ttot:>10,} "
            f"{tdur:>8.1f}s {count_s:>7}"
        )
        if any_cost:
            cost_s = (
                format_cost_reference(tcost, estimated=cost_estimated)
                if tcost is not None
                else "—"
            )
            line += f" {cost_s:>24}"
        lines.append(line)
        sum_in  += tin
        sum_out += tout
        sum_tot += ttot
        sum_dur += tdur
        if use_accounting and tcost is not None:
            sum_cost += tcost

    if sub_rows:
        lines.append("Sub-pipelines:")
        for r in sub_rows:
            _emit(f"  [{r[0]}]", *r[1:])
    if cross_rows:
        lines.append("Cross-level phases:")
        for r in cross_rows:
            _emit(f"  {r[0]}", *r[1:])

    lines.append("-" * width)
    total_line = (
        f"{'TOTAL':<24} {sum_in:>10,} {sum_out:>10,} {sum_tot:>10,} "
        f"{sum_dur:>8.1f}s {'-':>7}"
    )
    if any_cost:
        total_estimated = any(r[7] for r in sub_rows) or any(r[7] for r in cross_rows)
        total_line += f" {format_cost_reference(sum_cost, estimated=total_estimated):>24}"
    lines.append(total_line)
    return "\n".join(lines)


def cross_metrics_dict(
    per_project: dict[str, dict[str, Any]],
    cross_phases: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a top-level cross-run ``metrics.json`` dict.

    Aggregates two sources:

      * ``per_project`` — sub-pipeline ``metrics.json`` dicts, one per
        alias. These flow through the standard :class:`MetricsCollector`
        machinery already.
      * ``cross_phases`` — cross-level invokes (``cross_hypothesis``,
        ``cross_plan``, ``cross_validate_plan``, ``contract_check``).
        Captured inline in the cross orchestrator from each runtime's
        ``last_*`` attrs; not routed through a MetricsCollector.

    Both sources surface as entries in ``phases`` and contribute to the
    ``total_*`` rollups so readers (``orcho metrics``, ``orcho evidence``,
    dashboard, MCP) see the full token usage — not just sub-pipelines.

    Surface still matches :meth:`MetricsCollector.as_dict` (top-level
    ``total_tokens_in`` / ``total_tokens_out`` / ``total_tokens`` /
    ``total_duration_s`` plus a ``phases`` dict) so consumers don't
    need a cross/single branch.
    """
    tin = tout = ttot = 0
    tdur = 0.0
    trnd = 0
    tcost = 0.0
    any_cost = False
    phases: dict[str, Any] = {}
    for alias, m in per_project.items():
        m = m or {}
        a_in   = int(m.get("total_tokens_in")  or 0)
        a_out  = int(m.get("total_tokens_out") or 0)
        a_tot  = int(m.get("total_tokens")     or (a_in + a_out))
        a_dur  = float(m.get("total_duration_s") or 0.0)
        a_rnd  = int(m.get("total_rounds")     or 0)
        a_cost = m.get("total_cost_usd_equivalent") if accounting_enabled() else None
        tin  += a_in
        tout += a_out
        ttot += a_tot
        tdur += a_dur
        trnd += a_rnd
        if a_cost is not None:
            any_cost = True
            tcost += float(a_cost)
        phase_entry: dict[str, Any] = {
            "tokens_in":  a_in,
            "tokens_out": a_out,
            "total_tokens": a_tot,
            "duration_s": round(a_dur, 3),
            "kind":       "sub_pipeline",
        }
        if a_rnd:
            phase_entry["rounds"] = a_rnd
        if a_cost is not None:
            phase_entry["cost_usd_equivalent"] = round(float(a_cost), 4)
        phases[alias] = phase_entry

    cross_phases = cross_phases or {}
    for name, m in cross_phases.items():
        m = m or {}
        p_in  = int(m.get("tokens_in")  or 0)
        p_out = int(m.get("tokens_out") or 0)
        p_tot = int(m.get("total_tokens") or (p_in + p_out))
        p_dur = float(m.get("duration_s") or 0.0)
        p_cost = m.get("cost_usd_equivalent") if accounting_enabled() else None
        tin  += p_in
        tout += p_out
        ttot += p_tot
        tdur += p_dur
        if p_cost is not None:
            any_cost = True
            tcost += float(p_cost)
        cp_entry: dict[str, Any] = {
            "tokens_in":  p_in,
            "tokens_out": p_out,
            "total_tokens": p_tot,
            "duration_s": round(p_dur, 3),
            "kind":       "cross_level",
        }
        if "calls" in m:
            cp_entry["calls"] = int(m["calls"])
        if p_cost is not None:
            cp_entry["cost_usd_equivalent"] = round(float(p_cost), 4)
        phases[name] = cp_entry

    d: dict[str, Any] = {
        "total_tokens_in":  tin,
        "total_tokens_out": tout,
        "total_tokens":     ttot,
        "total_duration_s": round(tdur, 3),
        "phases":           phases,
        "cross_aggregation": {
            "sub_pipelines": list(per_project.keys()),
            "cross_phases":  list(cross_phases.keys()),
        },
    }
    if trnd:
        d["total_rounds"] = trnd
    if accounting_enabled() and any_cost:
        d["total_cost_usd_equivalent"] = round(tcost, 4)
    return d


def cross_summary_line(
    per_project: dict[str, dict[str, Any]],
    cross_phases: dict[str, dict[str, Any]] | None = None,
) -> str:
    """One-line aggregate — mirrors :meth:`MetricsCollector.summary_line`.

    Sums sub-pipelines + cross-level phases so the figure matches the
    grand TOTAL row in :func:`cross_summary_table`.
    """
    cross_phases = cross_phases or {}
    tin = tout = ttot = 0
    tdur = 0.0
    tcost = 0.0
    any_cost = False
    any_cost_estimated = False
    use_accounting = accounting_enabled()
    for m in per_project.values():
        m = m or {}
        tin  += int(m.get("total_tokens_in")  or 0)
        tout += int(m.get("total_tokens_out") or 0)
        ttot += int(m.get("total_tokens")     or 0)
        tdur += float(m.get("total_duration_s") or 0.0)
        c = m.get("total_cost_usd_equivalent") if use_accounting else None
        if c is not None:
            any_cost = True
            tcost += float(c)
            any_cost_estimated = any_cost_estimated or bool(m.get("cost_estimated"))
    for m in cross_phases.values():
        m = m or {}
        tin  += int(m.get("tokens_in")  or 0)
        tout += int(m.get("tokens_out") or 0)
        ttot += int(m.get("total_tokens") or 0)
        tdur += float(m.get("duration_s") or 0.0)
        c = m.get("cost_usd_equivalent") if use_accounting else None
        if c is not None:
            any_cost = True
            tcost += float(c)
            any_cost_estimated = any_cost_estimated or bool(m.get("cost_estimated"))
    parts = [
        f"Tokens: {ttot:,} (in={tin:,} out={tout:,})",
        f"Time: {tdur:.1f}s",
    ]
    if any_cost:
        parts.append(format_cost_reference_summary(tcost, estimated=any_cost_estimated))
    return " | ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Historical run loader  (`ma metrics --last N`)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RunSummary:
    """Summary of a single historical pipeline run from the runs/ directory."""

    run_id: str
    project: str
    task: str
    total_tokens: int
    total_duration_s: float
    rounds: int
    timestamp: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id":           self.run_id,
            "project":          self.project,
            "task":             self.task[:80],
            "total_tokens":     self.total_tokens,
            "total_duration_s": round(self.total_duration_s, 3),
            "rounds":           self.rounds,
            "timestamp":        self.timestamp,
        }


def load_historical_runs(
    runs_dir: Path | str,
    last_n: int = 10,
) -> list[RunSummary]:
    """Scan runs_dir for metrics.json + meta.json pairs, return RunSummary list.

    Returns at most last_n entries, sorted newest-first by run_id.
    Silently skips malformed or incomplete run dirs.
    """
    runs_dir = Path(runs_dir)
    if not runs_dir.exists():
        return []

    summaries: list[RunSummary] = []

    for run_dir in sorted(runs_dir.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue

        metrics_f = run_dir / "metrics.json"
        meta_f    = run_dir / "meta.json"
        if not (metrics_f.exists() and meta_f.exists()):
            continue

        try:
            metrics = json.loads(metrics_f.read_text(encoding="utf-8"))
            meta    = json.loads(meta_f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        # Single-project runs store ``project`` as a single path; cross-
        # project runs store ``projects`` as ``{alias: path}``. Render the
        # latter as ``cross[alias1,alias2]`` so ``orcho metrics`` doesn't
        # report ``?`` for every cross run.
        project_label = meta.get("project")
        if not project_label:
            projects_map = meta.get("projects")
            if isinstance(projects_map, dict) and projects_map:
                project_label = f"cross[{','.join(projects_map.keys())}]"
            else:
                project_label = "?"

        summaries.append(RunSummary(
            run_id          = run_dir.name,
            project         = project_label,
            task            = meta.get("task", "?"),
            total_tokens    = metrics.get("total_tokens", 0),
            total_duration_s= metrics.get("total_duration_s", 0.0),
            rounds          = metrics.get("total_rounds", 0),
            timestamp       = meta.get("timestamp", run_dir.name),
        ))

        if len(summaries) >= last_n:
            break

    return summaries


def format_history_table(summaries: list[RunSummary]) -> str:
    """Format RunSummary list as a console table for `ma metrics --last N`."""
    if not summaries:
        return "No historical runs found."

    lines = [
        f"{'Run ID':<20} {'Project':<22} {'Tokens':>8} {'Time':>8} {'Rnd':>4}  Task",
        "-" * 82,
    ]
    for s in summaries:
        project    = Path(s.project).name if s.project else "?"
        task_short = s.task[:32] + "…" if len(s.task) > 32 else s.task
        lines.append(
            f"{s.run_id:<20} {project:<22} "
            f"{s.total_tokens:>8,} {s.total_duration_s:>7.1f}s {s.rounds:>4}  "
            f"{task_short}"
        )
    return "\n".join(lines)
