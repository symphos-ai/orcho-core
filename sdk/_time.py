"""Internal time helpers for SDK report APIs."""
from __future__ import annotations

from datetime import datetime, timedelta


def parse_window(window: str | None) -> datetime | None:
    """Parse a relative window like ``"30d"`` / ``"7d"`` / ``"24h"`` / ``"all"``.

    Returns the cutoff `datetime` (anything older is excluded) or `None`
    when the window is `"all"` / unparseable / empty (= no cutoff). The
    contract matches the original CLI helper byte-for-byte so report
    commands keep producing identical output.
    """
    if not window:
        return None
    s = window.strip().lower()
    if s == "all":
        return None
    if s.endswith("d"):
        try:
            days = int(s[:-1])
        except ValueError:
            return None
        return datetime.now() - timedelta(days=days)
    if s.endswith("h"):
        try:
            hours = int(s[:-1])
        except ValueError:
            return None
        return datetime.now() - timedelta(hours=hours)
    return None


def run_ts_to_datetime(run_id: str) -> datetime | None:
    """Parse a run-id timestamp like ``20260502_104135``.

    Strict: the whole string must match ``%Y%m%d_%H%M%S``. Returns
    `None` for any other shape. Tolerates older or third-party-
    generated run IDs that use a different naming scheme — those just
    don't get a parsed timestamp.
    """
    if not run_id:
        return None
    try:
        return datetime.strptime(run_id, "%Y%m%d_%H%M%S")
    except (ValueError, TypeError):
        return None
