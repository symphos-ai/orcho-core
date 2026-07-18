"""core/io/journey_prompt.py — framing helpers for interactive prompts.

Domain layer on top of :mod:`core.io.ansi`: the journey-style prompts
(pre-run intake, post-release delivery gate, resume intent) share a
visual grammar — `═` divider, bold/cyan title, grey help lines, green
``[default]`` chip — and this module is the single home for those
building blocks.

Color primitives (:func:`is_color_active`, :func:`paint`,
:func:`strip_ansi`) live in :mod:`core.io.ansi` and are re-exported
here so existing callers (``commit_delivery``, ``pre_run_dirty``,
``resume_prompt``) don't need to learn a second import path.
"""
from __future__ import annotations

from typing import TextIO

from core.io.ansi import (
    C,
    is_color_active,
    paint,
    strip_ansi,
)

_DIVIDER_CHAR = "═"
_DIVIDER_WIDTH = 68


def bold(text: str, *, color: bool) -> str:
    return paint(text, C.BOLD, color=color)


def grey(text: str, *, color: bool) -> str:
    return paint(text, C.GREY, color=color)


def green_bold(text: str, *, color: bool) -> str:
    return paint(text, C.GREEN, C.BOLD, color=color)


def cyan_bold(text: str, *, color: bool) -> str:
    return paint(text, C.CYAN, C.BOLD, color=color)


def title(text: str, *, color: bool) -> str:
    """Section title — cyan + bold."""
    return cyan_bold(text, color=color)


def help_line(text: str, *, color: bool) -> str:
    """One-line help text under an option — dim/grey."""
    return grey(text, color=color)


def default_chip(*, color: bool) -> str:
    """The green ``[default]`` chip rendered next to the default option."""
    return green_bold("[default]", color=color)


def divider(*, color: bool) -> str:
    """Horizontal separator that frames a journey block."""
    line = _DIVIDER_CHAR * _DIVIDER_WIDTH
    return grey(line, color=color)


def ask_yn(
    prompt: str,
    *,
    default_yes: bool,
    stdin: TextIO,
    stdout: TextIO,
    color: bool,
) -> bool | None:
    """Ask a yes/no question.  Returns None on EOF / Ctrl-C (abort)."""
    hint = "[Y/n]" if default_yes else "[y/N]"
    full_prompt = bold(f"{prompt} {hint} ", color=color)
    stdout.write(full_prompt)
    stdout.flush()
    try:
        line = stdin.readline()
    except KeyboardInterrupt:
        stdout.write("\n")
        return None
    if not line:
        stdout.write("\n")
        return None
    choice = line.strip().lower()
    if not choice:
        return default_yes
    return choice in ("y", "yes")


__all__ = [
    # Domain helpers
    "ask_yn",
    "bold",
    "cyan_bold",
    "default_chip",
    "divider",
    "green_bold",
    "grey",
    "help_line",
    "title",
    # Re-exports from core.io.ansi (compat surface — kept so existing
    # callers don't churn; new code is free to import from ansi.py).
    "is_color_active",
    "paint",
    "strip_ansi",
]
