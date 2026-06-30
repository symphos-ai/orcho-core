"""Terminal stdin helpers shared by interactive CLI prompts.

The single concern here is *paste safety*. A multi-line / multi-paragraph
paste is delivered to a cooked-mode tty as one rapid burst of
newline-separated lines. A prompt that reads a single line captures only the
first line and leaves the rest in the tty buffer, where it truncates the
intended value and then leaks to the shell as commands once the program
exits.

:func:`drain_paste_burst` consumes the whole burst so free-text prompts get
the full multi-line value — including the blank lines between pasted
paragraphs — and nothing leaks. It degrades cleanly to single-line behaviour
when stdin can't be polled (no real ``fileno``, closed fd, piped/CI
transports), so non-interactive callers keep their existing contract.
"""
from __future__ import annotations

import select
import sys
from typing import TextIO


def stdio_interactive() -> bool:
    """True only when **both** stdin and stdout are real TTYs.

    The single interactivity predicate for the whole engine: gates that may
    prompt the operator (commit-delivery, pre-run dirty intake, the
    auto-correction follow-up loop) must fire only at a real terminal, never
    on a piped / CI / MCP transport where a blocking prompt would hang.

    Any uncertainty resolves to ``False`` (non-interactive): a missing or
    overridden ``isatty`` and an ``OSError`` from a closed fd both mean "do
    not prompt". This is the conservative, safe default — a false negative
    skips a prompt, a false positive would block an unattended run forever.
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


# Idle window after the last buffered line before we conclude the paste burst
# has drained. During a burst the next line is already readable, so we never
# actually wait; this timeout is incurred once, after the final line. Kept
# small so a normal single typed line returns with no perceptible delay.
PASTE_DRAIN_TIMEOUT_S = 0.1


def drain_paste_burst(
    first_line: str,
    *,
    stdin: TextIO,
    timeout: float = PASTE_DRAIN_TIMEOUT_S,
) -> str:
    """Return ``first_line`` joined with any lines from the same paste burst.

    ``first_line`` is the line the caller already read (with or without its
    trailing newline — it is normalised away). Continuation lines are read via
    ``stdin.readline()`` while ``select`` reports more buffered input,
    including blank paragraph-separator lines, until the burst drains.

    The result is newline-joined and *not* otherwise stripped; callers apply
    their own ``.strip()``. Degrades to the (newline-stripped) ``first_line``
    when stdin can't be polled: the common single typed-line case and every
    piped / non-tty transport land here.
    """
    lines = [first_line.rstrip("\n")]
    try:
        fd = stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return lines[0]
    while True:
        try:
            ready, _, _ = select.select([fd], [], [], timeout)
        except (OSError, ValueError):
            break
        if not ready:
            break
        nxt = stdin.readline()
        if not nxt:  # EOF / closed stream.
            break
        lines.append(nxt.rstrip("\n"))
    return "\n".join(lines)


__all__ = ["drain_paste_burst", "PASTE_DRAIN_TIMEOUT_S"]
