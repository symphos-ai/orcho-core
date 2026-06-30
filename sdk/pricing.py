"""Pricing table — read-only `show` plus the side-effecting `refresh`."""
from __future__ import annotations

from datetime import UTC, datetime

from core.observability import pricing as _pricing, pricing_scrapers as _scrapers
from sdk.errors import PricingFetchError
from sdk.types import PricingEntry, PricingTable, RefreshResult


def show_pricing() -> PricingTable:
    """Return the effective pricing table.

    Read-only. Merges the bundled snapshot with the user override file;
    `PricingEntry.source` records origin per row so the formatter can
    annotate without re-querying.
    """
    raw = _pricing.load_pricing()
    entries = tuple(
        PricingEntry(
            model=model,
            input_per_million=e.input_per_1m_usd,
            output_per_million=e.output_per_1m_usd,
            source=e.source,
        )
        for model, e in sorted(raw.items())
    )
    user = _pricing.user_snapshot_date()
    bundled = _pricing.snapshot_date()
    return PricingTable(
        entries=entries,
        user_snapshot_date=user.isoformat() if user else None,
        bundled_snapshot_date=bundled.isoformat() if bundled else None,
        snapshot_age_days=_pricing.snapshot_age_days(),
        user_path=_user_path(),
        bundled_path=None,
    )


def _user_path():
    """Resolve the user-override path through the existing private helper.

    Calling the private function via the module attribute keeps the
    indirection in one place — tests can monkeypatch it on the
    `core.observability.pricing` module without reaching inside SDK.
    """
    fn = getattr(_pricing, "_user_pricing_path", None)
    return fn() if callable(fn) else None


def refresh_pricing(provider: str = "openai", *, dry_run: bool = False) -> RefreshResult:
    """Refresh the local pricing override file.

    **Side-effecting.** Writes `~/.orcho/pricing.local.toml` (or the
    path indicated by `core.observability.pricing._user_pricing_path`,
    which tests must monkeypatch). Raises `PricingFetchError` for
    any scrape or network failure — embedders that want to surface a
    user-friendly message catch the error and read `.exit_code`.

    `dry_run=True` returns a `RefreshResult` without writing.
    """
    try:
        result = _scrapers.refresh(provider)
    except _scrapers.PricingScrapeError as exc:
        raise PricingFetchError(f"Scrape failed: {exc}") from exc
    except Exception as exc:  # network failure umbrella (URLError, timeout, …)
        raise PricingFetchError(f"Fetch failed: {exc}") from exc

    when = datetime.now(UTC)
    if dry_run:
        return RefreshResult(
            written_path=_user_path() or _pricing._user_pricing_path(),
            snapshot_date=when.replace(microsecond=0).isoformat(),
            models_count=len(result.models),
            source_label=result.provenance,
        )

    target = _pricing.write_user_pricing(
        result.models,
        source_url=result.url,
        fetched_at=when,
    )
    return RefreshResult(
        written_path=target,
        snapshot_date=when.replace(microsecond=0).isoformat(),
        models_count=len(result.models),
        source_label=result.provenance,
    )


__all__ = ["show_pricing", "refresh_pricing"]
