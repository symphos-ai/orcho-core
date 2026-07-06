"""
core/io/encoding.py — portable UTF-8 for the standard streams.

Orcho's terminal output uses non-ASCII glyphs (arrows, box drawing, and emoji
such as the ``📄``/``📡`` run-header chips). On native Windows the default
console encoding is a legacy code page (typically cp1252) that cannot encode
those glyphs, so a plain ``print`` raises ``UnicodeEncodeError`` and aborts the
run before any work happens. The same failure occurs under a POSIX ``C``/ASCII
locale.

:func:`ensure_utf8_stdio` reconfigures ``sys.stdout`` / ``sys.stderr`` to UTF-8
when they are not already UTF-8. It is a no-op on hosts that are already UTF-8
(the common macOS/Linux case), idempotent, and safe to call from every CLI
entry point.
"""
from __future__ import annotations

import contextlib
import sys


def ensure_utf8_stdio() -> None:
    """Reconfigure the standard streams to UTF-8 when they are not already.

    Uses ``errors="replace"`` as a belt-and-braces guard so output degrades to a
    replacement character rather than crashing if any stream still refuses a
    code point. Streams that cannot be reconfigured (already detached, or
    swapped for a non-``TextIOWrapper`` object in tests) are left untouched.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if encoding == "utf8":
            continue
        # Stream may not support reconfigure (already detached, or replaced by a
        # plain buffer in a test harness) — leave it as-is in that case.
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


__all__ = ["ensure_utf8_stdio"]
