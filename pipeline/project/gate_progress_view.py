# SPDX-License-Identifier: Apache-2.0
"""Terminal presenter for live verification-gate progress (ADR 0190).

A scheduled gate hook runs the declared checks as blocking subprocesses that can
take minutes. ``gate_repair`` already prints a ``▶ running…`` line before the
call; this module renders the *live* coalesced updates that arrive while the
command runs — command identity, elapsed, last-output age, and a compact tail —
so a multi-minute gate is visibly alive without drowning summary mode in raw
output.

Strictly presentation: it consumes the bounded
:class:`pipeline.verification_progress.GateProgressRecord` the aggregator hands
it and prints one compact line per coalesced update. It is only ever wired under
``PresentationPolicy.TERMINAL`` (``gate_repair`` supplies the presenter only
then); SILENT and MCP-stdio presentations pass no presenter, so nothing reaches
stdout — the durable ``gate.progress`` event is still emitted, which is not
stdout. Color routes through :mod:`core.io.ansi` per the io discipline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.verification_progress import GateProgressRecord, PresenterFn

#: Compact live-tail width. The full output lives in the per-command ``.log``;
#: the live line only needs a glanceable end-of-stream fragment.
_LIVE_TAIL_CHARS = 80


def _fmt_secs(seconds: float) -> str:
    """``95.4`` → ``1m35s``; ``8.1`` → ``8s`` (matches gate_repair's format)."""
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "0s"
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def _compact_tail(record: GateProgressRecord) -> str:
    """A single-line, bounded fragment of the most recent output.

    Prefers stderr (where progress meters usually live), falling back to stdout.
    Newlines are collapsed so the live line never wraps unexpectedly, and the
    fragment is bounded to :data:`_LIVE_TAIL_CHARS`.
    """
    raw = record.stderr_tail or record.stdout_tail
    if not raw:
        return ""
    flat = " ".join(raw.split())
    if len(flat) > _LIVE_TAIL_CHARS:
        flat = flat[-_LIVE_TAIL_CHARS:]
    return flat


def render_gate_progress_line(record: GateProgressRecord) -> str:
    """Render one bounded live-progress line (no color, no trailing newline).

    Factored out for tests: the caller overlays color and prints. The line
    names the command, its elapsed time, whether output has been seen yet, and a
    compact tail — all facts, never a fabricated percentage or health verdict.
    """
    elapsed = _fmt_secs(record.elapsed_s)
    if not record.has_output:
        return f"     {record.name}  ⏱ {elapsed}  · no output yet"
    tail = _compact_tail(record)
    tail_part = f"  · {tail}" if tail else ""
    return f"     {record.name}  ⏱ {elapsed}  · streaming{tail_part}"


def print_gate_progress(record: GateProgressRecord) -> None:
    """Presenter: print one compact, colorized live-progress line, flushed."""
    from core.io.ansi import C, is_color_active, paint

    color = is_color_active()
    print(paint(render_gate_progress_line(record), C.GREY, color=color), flush=True)


def gate_progress_presenter() -> PresenterFn:
    """Return the terminal presenter callback for a gate progress aggregator.

    ``gate_repair`` calls this only for ``PresentationPolicy.TERMINAL`` runs;
    SILENT / MCP-stdio runs pass ``None`` so no unsolicited stdout is produced.
    """
    return print_gate_progress


__all__ = [
    "gate_progress_presenter",
    "print_gate_progress",
    "render_gate_progress_line",
]
