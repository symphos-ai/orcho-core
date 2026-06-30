"""core/io/ansi.py — ANSI color palette + color-decision policy.

Single source of truth for the terminal color codes used across
``core.io.transcript``, ``core.io.pipeline_block``, ``core.io.journey_prompt``,
and any other presentation module. Kept free of cross-module dependencies
so renderers can import it without dragging the transcript surface.

Two surfaces:

* **Palette** — the :class:`C` constants. Concrete escape codes only;
  no logic.
* **Policy** — :func:`is_color_active`, :func:`paint`, :func:`strip_ansi`,
  and the optional :func:`set_color_enabled` / :func:`get_color_enabled`
  process-level override. The decision flow used by :func:`paint` is:

  1. Explicit ``color=True``/``False`` passed by the caller wins.
  2. Otherwise, if a process-level override is set, use it.
  3. Otherwise, auto-detect via :func:`is_color_active` against the
     supplied (or default ``sys.stdout``) stream.

  Renderers should default to ``color=None`` so they auto-adapt to
  CLI, MCP, pipes, and tests without an explicit threading of the
  flag. Tests can pin a deterministic outcome with explicit ``True`` /
  ``False`` and never need to monkey-patch the environment.
"""
from __future__ import annotations

import os
import re
import sys
from typing import TextIO


class _C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    GREY = "\033[90m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    WHITE = "\033[97m"


C = _C  # public alias for callers that want the same palette


# ── policy ─────────────────────────────────────────────────────────────

# Matches CSI escape sequences (``\x1b[…m``) used for SGR/color codes.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# Process-level override:
#   None  — no override; :func:`paint` falls through to auto-detect.
#   True  — force color on (e.g. CLI ``--force-color`` opt-in).
#   False — force color off (e.g. CLI ``--no-color`` opt-out).
_color_override: bool | None = None


def set_color_enabled(value: bool | None) -> None:
    """Set or clear the process-level color override.

    ``value=True`` / ``False`` overrides auto-detection for every
    subsequent :func:`paint` call that does not pass an explicit
    ``color=`` argument. ``value=None`` clears the override and
    restores auto-detect-by-default behaviour.

    Intended for one-shot CLI startup wiring. Renderers must not
    poke this from inside a request — the override is secondary to
    the explicit ``color=`` argument, never the source of truth.
    """
    global _color_override
    _color_override = value


def get_color_enabled() -> bool | None:
    """Return the current process-level override, or ``None`` if unset."""
    return _color_override


def is_color_active(stream: TextIO | None = None) -> bool:
    """True when ``stream`` (or ``sys.stdout``) is a TTY and NO_COLOR is unset.

    Pure auto-detection: this function does NOT consult the process
    override. Combine via :func:`paint` (or :func:`_resolve_color`)
    when caller wants override-aware resolution.

    Any uncertainty — missing ``isatty``, ``OSError`` from a closed
    descriptor — collapses to ``False`` so non-interactive transports
    see plain text.
    """
    if "NO_COLOR" in os.environ:
        return False
    actual = stream if stream is not None else sys.stdout
    try:
        return bool(actual.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _resolve_color(
    *, color: bool | None, stream: TextIO | None,
) -> bool:
    """Apply the documented three-step color-decision flow."""
    if color is not None:
        return color
    if _color_override is not None:
        return _color_override
    return is_color_active(stream)


def paint(
    text: str,
    *codes: str,
    color: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """Wrap ``text`` in ANSI ``codes`` per the color-decision flow.

    See module docstring for the full resolution order. ``codes`` may
    be empty — that short-circuits to a plain string regardless of
    the color decision, so callers don't need to special-case the
    "nothing to paint" path.

    **Stderr discipline.** When ``color=None`` and no process-level
    override is set, auto-detect consults the supplied ``stream``
    (or ``sys.stdout`` when ``stream`` is omitted). Stderr-bound
    renderers therefore MUST pass ``stream=sys.stderr`` — otherwise
    an ``orcho run > out.log`` invocation (stderr still on TTY,
    stdout piped to a file) would wrongly suppress color on the
    stderr-bound output. The mirror case (stderr piped, stdout on
    TTY) would wrongly emit color into a file. Either failure mode
    is silent; pass the explicit stream.
    """
    if not codes:
        return text
    if not _resolve_color(color=color, stream=stream):
        return text
    return f"{''.join(codes)}{text}{C.RESET}"


def strip_ansi(text: str) -> str:
    """Remove CSI color escape codes from ``text``.

    Useful in tests that pin visible content without locking down the
    palette, and at integration seams where a downstream consumer
    cannot interpret terminal escapes (log files, web UIs that render
    plain text, MCP transports).
    """
    return _ANSI_CSI_RE.sub("", text)


__all__ = [
    "C",
    "_C",
    "get_color_enabled",
    "is_color_active",
    "paint",
    "set_color_enabled",
    "strip_ansi",
]
