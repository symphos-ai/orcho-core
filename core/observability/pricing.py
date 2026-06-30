"""core.observability.pricing — OpenAI pricing snapshot + user override.

Codex CLI doesn't compute a counterfactual API cost (the way Claude
Code's stream-json result line surfaces ``total_cost_usd``). To still
show a USD-equivalent for codex-driven phases in ``orcho cost``, we
need to multiply tokens × rate. Two non-negotiables drive the design:

1. **Don't ship speculative numbers.** Public OpenAI rates change; a
   number bundled six months ago is worse than no number. The
   bundled snapshot at ``_config/pricing.openai.snapshot.json`` is
   intentionally **empty** — orcho returns ``None`` (= "we don't
   know") for every model until the user opts in.

2. **User owns the truth.** ``orcho pricing refresh`` scrapes the
   public pricing page, validates the parse, and writes
   ``~/.orcho/pricing.local.toml``. That file wins over the bundled
   snapshot. If the scrape breaks (page structure changed), refresh
   fails loudly with a hint to hand-edit the local file. Either way,
   verifying current rates is the user's responsibility — orcho
   never claims authority on OpenAI pricing.

Public surface:

  ``load_pricing()``           — merged dict {model: PriceEntry}.
  ``estimate_cost_usd(model,
       tokens_in, tokens_out,
       cached_tokens_in=0)``— fresh input × in_rate +
                              cached input × cached_rate + output × out_rate.
                                  ``None`` when model unknown.
  ``estimate_cost_from_total(model, tokens_total)``
                              — codex case: only total available, we
                                 split 50/50 (clearly an estimate; the
                                 caller surfaces a marker).
  ``snapshot_age_days()``     — staleness signal for user warnings.
  ``effective_source(model)`` — "user" / "snapshot" / None — so the
                                 CLI can label numbers by where they
                                 came from.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

# Bundled snapshot lives next to other ``_config/`` JSON. Read at
# module import to keep the lookup fast — pricing changes infrequently
# enough that hot-reloading on every call would be wasted work.
from core.infra.paths import CONFIG_DIR

_SNAPSHOT_PATH = CONFIG_DIR / "pricing.openai.snapshot.json"


def _user_pricing_path() -> Path:
    """``~/.orcho/pricing.local.toml`` — user-controlled overrides.

    Honours ``$ORCHO_PRICING_FILE`` for tests / sandboxed environments
    where touching the home directory is undesirable.
    """
    if env := os.environ.get("ORCHO_PRICING_FILE"):
        return Path(env).expanduser()
    return Path.home() / ".orcho" / "pricing.local.toml"


@dataclass(frozen=True)
class PriceEntry:
    """Per-model rate in $/1M tokens. Both halves required — without
    a split we can't price anything that doesn't have a ``50/50`` caveat
    attached at the call site.
    """
    input_per_1m_usd: float
    output_per_1m_usd: float
    source: str  # "user" | "snapshot"
    cached_input_per_1m_usd: float | None = None


def _load_snapshot() -> dict:
    """Bundled snapshot. Returns empty dict on any read error — the
    snapshot is best-effort, not authoritative."""
    try:
        return json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _load_user_overrides() -> dict:
    """User TOML. Empty dict when the file doesn't exist or is
    malformed — orcho should never crash because the user's pricing
    file got corrupted."""
    p = _user_pricing_path()
    if not p.exists():
        return {}
    try:
        try:
            import tomllib  # py 3.11+
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_pricing() -> dict[str, PriceEntry]:
    """Effective pricing table: user overrides win, bundled snapshot
    fills the rest, missing models stay missing.
    """
    out: dict[str, PriceEntry] = {}
    snapshot = _load_snapshot()
    for model, rates in (snapshot.get("models") or {}).items():
        try:
            out[model] = PriceEntry(
                input_per_1m_usd=float(rates["input_per_1m_usd"]),
                output_per_1m_usd=float(rates["output_per_1m_usd"]),
                source="snapshot",
                cached_input_per_1m_usd=(
                    float(rates["cached_input_per_1m_usd"])
                    if "cached_input_per_1m_usd" in rates else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            continue

    user = _load_user_overrides()
    for model, rates in (user.get("models") or {}).items():
        try:
            out[model] = PriceEntry(
                input_per_1m_usd=float(rates["input_per_1m_usd"]),
                output_per_1m_usd=float(rates["output_per_1m_usd"]),
                source="user",
                cached_input_per_1m_usd=(
                    float(rates["cached_input_per_1m_usd"])
                    if "cached_input_per_1m_usd" in rates else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def effective_source(model: str) -> str | None:
    """``"user"`` / ``"snapshot"`` / ``None``. Used by orcho cost to
    annotate displayed numbers with their origin.
    """
    entry = load_pricing().get(model)
    return entry.source if entry else None


def snapshot_date() -> date | None:
    """Date the bundled snapshot was last refreshed, or ``None``
    when empty (which is the default — orcho ships no rates).
    """
    raw = (_load_snapshot().get("_meta") or {}).get("snapshot_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def user_snapshot_date() -> date | None:
    """Date the user's local pricing file was last refreshed."""
    raw = (_load_user_overrides().get("meta") or {}).get("fetched_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def snapshot_age_days() -> int | None:
    """Days since the **most recent** of (bundled snapshot, user file).
    Returns ``None`` when neither has a date — first-run state.
    """
    candidates = [d for d in (snapshot_date(), user_snapshot_date()) if d]
    if not candidates:
        return None
    return (date.today() - max(candidates)).days


def estimate_cost_usd(
    model: str,
    *,
    tokens_in: int,
    tokens_out: int,
    cached_tokens_in: int = 0,
) -> float | None:
    """Estimate USD cost given the in/out token split. Returns ``None``
    when the model isn't in the pricing table — caller decides what to
    show (typically ``(no $)``).
    """
    entry = load_pricing().get(model)
    if entry is None:
        return None
    cached_in = min(max(0, int(cached_tokens_in or 0)), max(0, tokens_in))
    fresh_in = max(0, tokens_in) - cached_in
    # Backward compatibility for pre-cache pricing.local.toml files. OpenAI
    # currently prices cached text input at 10% of fresh input for GPT-5.x;
    # if the user has not refreshed pricing yet, use that conservative
    # cache-aware estimate instead of billing cached input at the full rate.
    cached_rate = (
        entry.cached_input_per_1m_usd
        if entry.cached_input_per_1m_usd is not None
        else entry.input_per_1m_usd * 0.1
    )
    return (
        fresh_in / 1_000_000.0 * entry.input_per_1m_usd
        + cached_in / 1_000_000.0 * cached_rate
        + max(0, tokens_out) / 1_000_000.0 * entry.output_per_1m_usd
    )


def estimate_cost_from_total(
    model: str,
    tokens_total: int,
) -> float | None:
    """Codex case: only total tokens available, no in/out split.

    Splits 50/50 for the cost calculation. This is **a guess** — the
    caller MUST surface an "estimated" marker so the user knows the
    number isn't measured. Returns ``None`` when the model isn't
    priced.
    """
    half = tokens_total // 2
    return estimate_cost_usd(model, tokens_in=half, tokens_out=tokens_total - half)


def write_user_pricing(
    models: dict[str, dict],
    *,
    source_url: str = "https://developers.openai.com/api/docs/pricing",
    fetched_at: datetime | None = None,
) -> Path:
    """Write the user override file. Used by ``orcho pricing refresh``
    after a successful scrape; manual editors can also call into this
    via the API but typically just edit the TOML by hand.

    Writes a deterministic header + a ``[meta]`` block + per-model
    sections. Does not preserve hand-written comments — refresh is
    destructive (mirrors how ``pip freeze > requirements.txt`` works).
    """
    when = fetched_at or datetime.now(UTC)
    p = _user_pricing_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# orcho local pricing — overrides _config/pricing.openai.snapshot.json.",
        "# Generated by ``orcho pricing refresh`` or hand-edited.",
        "#",
        "# YOUR RESPONSIBILITY: verify these rates against",
        "# https://developers.openai.com/api/docs/pricing whenever the",
        "# numbers matter. Stale rates → wrong cost estimates.",
        "",
        "[meta]",
        f'fetched_at = "{when.replace(microsecond=0).isoformat()}"',
        f'source     = "{source_url}"',
        "",
    ]
    for model in sorted(models):
        rates = models[model]
        in_rate = float(rates.get("input_per_1m_usd", 0.0))
        out_rate = float(rates.get("output_per_1m_usd", 0.0))
        cached_rate = rates.get("cached_input_per_1m_usd")
        lines.append(f'[models."{model}"]')
        lines.append(f"input_per_1m_usd  = {in_rate}")
        if cached_rate is not None:
            lines.append(f"cached_input_per_1m_usd = {float(cached_rate)}")
        lines.append(f"output_per_1m_usd = {out_rate}")
        lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")
    return p
