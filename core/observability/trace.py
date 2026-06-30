"""
core/trace.py — Lightweight verbose/trace output system.

Design: module-level flag set once at CLI startup.
No parameter threading — call vtrace() / vtimed() anywhere.

Usage:
    # At CLI entrypoint:
    from core.observability.trace import enable_trace
    if args.verbose:
        enable_trace()

    # Anywhere in the codebase:
    from core.observability.trace import vtrace, vtimed
    vtrace("provider", f"{type(provider).__name__} selected")
    with vtimed("PLAN"):
        result = claude.invoke(prompt, cwd)
"""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager

from core.io.ansi import C, paint

# ── State ─────────────────────────────────────────────────────────────────────

_enabled: bool = False


def enable_trace(enabled: bool = True) -> None:
    """Enable or disable trace output globally."""
    global _enabled
    _enabled = enabled


def is_enabled() -> bool:
    return _enabled


# ── Core output ───────────────────────────────────────────────────────────────

def vtrace(category: str, message: str, *, extra: str = "") -> None:
    """Print a trace line if verbose mode is enabled.

    Format:  [TRACE][category]  message  (extra)

    Trace output goes to stderr; ``paint(stream=sys.stderr)`` makes
    auto-detect consult stderr's TTY status (see Terminal color
    discipline rule in orcho-core/CLAUDE.md).
    """
    if not _enabled:
        return
    extra_part = (
        f"  {paint(f'({extra})', C.DIM, stream=sys.stderr)}" if extra else ""
    )
    print(
        f"{paint('[TRACE]', C.DIM, stream=sys.stderr)}"
        f"{paint(f'[{category}]', C.CYAN, stream=sys.stderr)}"
        f"  {message}{extra_part}",
        file=sys.stderr,
        flush=True,
    )


def vdump(category: str, label: str, text: str, *, max_chars: int = 200) -> None:
    """Dump a text preview (prompt / response) under trace."""
    if not _enabled:
        return
    preview = text.replace("\n", "↵ ")[:max_chars]
    ellipsis = "…" if len(text) > max_chars else ""
    vtrace(
        category,
        f"{label}: {len(text)} chars → "
        f"{paint(f'{preview}{ellipsis}', C.DIM, stream=sys.stderr)}",
    )


@contextmanager
def vtimed(category: str, label: str = ""):
    """Context manager that traces entry + exit with elapsed time.

    Usage:
        with vtimed("implement", "Claude code phase"):
            output = claude.invoke(prompt, cwd, mutates_artifacts=True)
    """
    if not _enabled:
        yield
        return

    tag = f"{category}{'/' + label if label else ''}"
    t0 = time.perf_counter()
    vtrace(tag, "→ start")
    try:
        yield
    finally:
        elapsed = time.perf_counter() - t0
        vtrace(tag, "← done", extra=f"{elapsed:.3f}s")
