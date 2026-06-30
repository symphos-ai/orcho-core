"""
core/logging.py — Banners and the task-progress.log writer.

Extracted from orchestrator.py.

Public surface:
    set_progress_log(output_dir, session_ts="")
    log_phase(phase, title, status="START", outcome="")
    banner(phase, title, color=C.CYAN)
    success(text)
    warn(text)
    preview(label, text, color=C.WHITE, n=700)

Terminal colors live in :mod:`core.io.ansi` — import :data:`C` from
there directly.

The module keeps a private module-level ``_progress_log`` Path so the same
log file is reused across all phases of a single pipeline run. Tests can
inspect it directly via ``core.logging._progress_log``.
"""

from datetime import datetime
from pathlib import Path
from typing import Literal

from core.io.ansi import C, paint
from core.observability import events as _events

OutputMode = Literal["summary", "live", "debug"]
OUTPUT_MODES: tuple[OutputMode, ...] = ("summary", "live", "debug")


# ── Progress log ─────────────────────────────────────────────────────────────
_progress_log: Path | None = None


def set_progress_log(output_dir: Path | None, session_ts: str = "") -> None:
    """Call once after output_dir (= runs/{ts}/) is known. All log_phase() calls write here.
    The session timestamp is already encoded in the directory name, so the file
    is always named ``progress.log`` (session_ts arg retained for compat but ignored)."""
    global _progress_log
    _progress_log = output_dir / "progress.log" if output_dir else None


def log_phase(
    phase: str,
    title: str,
    status: str = "START",
    outcome: str = "",
    *,
    phase_kind: str | None = None,
    attempt: int = 1,
    phase_key: str | None = None,
    round: int | None = None,
) -> None:
    """Append one line to task-progress.log (no-op if no progress log set)
    AND mirror the event into the JSONL event-store (no-op if store not init).

    Parameters:
        phase       — human-readable phase id ("PLAN", "REVIEW_CHANGES", "DONE", ...).
                      Used as the legacy `phase` string field on the Event and
                      as the label in progress.log lines.
        title       — short description.
        status      — "START" or "END" / "DONE" (anything non-START maps to phase.end).
        outcome     — short outcome string for the END line.
        phase_kind  — canonical kind from `core.observability.phases` (PLAN,
                      VALIDATE_PLAN, IMPLEMENT, …). Required for events the
                      dashboard groups by attempt. Pass None for non-canonical
                      milestones (banners like "DONE" / "PLAN COMPLETE" /
                      "CONTRACT_CHECK").
        attempt     — 1-based attempt index. Singletons stay at 1; loops
                      (plan→validate_plan, build→review→fix) increment per
                      round.

    progress.log is kept for backward compat with external tooling; the
    event-store is the canonical source of truth for new readers
    (orcho-watch, dashboard).
    """
    # Mirror to event-store first (cheap, thread-safe, never raises). Status
    # values in the wild: "START" (banner default), "END" (explicit),
    # "DONE" (CLI banner). Map all non-START to phase.end so consumers have
    # a binary start/end model regardless of legacy phrasing.
    # Phase 7.10-followup: ``phase_key`` (lowercase canonical handler
    # key) + ``round`` (loop counter) are attached to the events module
    # context so every emit() while this phase is active carries them.
    # Falls back to legacy behaviour (``phase`` display string +
    # ``attempt`` in payload) when callers don't pass the new fields.
    effective_round = round if round is not None else attempt
    if status == "START":
        _events.set_phase_context(
            phase=phase, phase_key=phase_key, round=effective_round,
            title=title,
        )
        _events.emit("phase.start", title=title,
                     phase_kind=phase_kind, attempt=attempt)
    else:
        _events.emit("phase.end", title=title, outcome=outcome or status,
                     phase_kind=phase_kind, attempt=attempt)
        _events.clear_phase_context()

    if not _progress_log:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    suffix = f"  → {outcome}" if outcome else ""
    line = f"[{ts}]  {status:<6}  [{phase}]  {title}{suffix}\n"
    _progress_log.parent.mkdir(parents=True, exist_ok=True)
    with _progress_log.open("a", encoding="utf-8") as f:
        f.write(line)


def banner(
    phase: str,
    title: str,
    color: str = C.CYAN,
    *,
    phase_kind: str | None = None,
    attempt: int = 1,
    status: str | None = None,
    phase_key: str | None = None,
    round: int | None = None,
) -> None:
    """Emit a phase boundary on stdout and mirror to ``progress.log`` /
    the event-store.

    The visible banner is rendered by :func:`core.io.transcript.render_phase_header`
    so the CLI run-transcript stays consistent across phases. ``status``
    is optional and surfaced as a status chip next to the title (e.g.
    ``running`` / ``approved`` / ``halted``).

    ``phase_key`` (lowercase canonical handler key) and ``round`` (loop
    counter) flow into the event-store context so every subsequent
    event payload carries them — see :func:`log_phase` for the contract.
    """
    from core.io.transcript import render_phase_header
    print(render_phase_header(phase, title, status=status, color=color))
    log_phase(
        phase, title, phase_kind=phase_kind, attempt=attempt,
        phase_key=phase_key, round=round,
    )


def success(text: str) -> None:
    print(paint(f"  ✓ {text}", C.GREEN, C.BOLD))


def warn(text: str) -> None:
    print(paint(f"  ⚠ {text}", C.YELLOW, C.BOLD))


# Module-wide verbose toggle. ``preview()`` honours this when its caller
# leaves the default ``n``; explicit ``n=`` overrides still win. Set via
# ``set_verbose(True)`` from ``run_pipeline.main()`` when ``--verbose`` /
# ``-v`` is on so every phase preview prints in full instead of truncating
# at 700 chars.
_verbose: bool = False


def set_verbose(flag: bool) -> None:
    """Enable / disable the module-wide preview-untruncated mode."""
    global _verbose
    _verbose = bool(flag)


def get_verbose() -> bool:
    """Return the module-wide verbose flag.

    True iff ``--output debug`` / ``--verbose`` is active. Consumers
    outside this module read it through this accessor instead of
    touching ``_verbose`` directly.
    """
    return _verbose


def normalize_output_mode(mode: str | None) -> OutputMode:
    """Return a canonical run-output mode or raise ``ValueError``."""
    if mode is None:
        return "summary"
    if mode not in OUTPUT_MODES:
        choices = ", ".join(OUTPUT_MODES)
        raise ValueError(f"invalid output mode {mode!r}; expected one of: {choices}")
    return mode  # type: ignore[return-value]


# Run-output mode state. Set by ``apply_output_mode``; read by
# observability surfaces (live card, future surfaces) that need to
# branch on the active transcript verbosity without subscribing to
# events. Defaults to ``"summary"`` so a test that imports a
# rendering helper without first applying a mode behaves as if no
# user opted in to extra output.
_output_mode: OutputMode = "summary"


def get_output_mode() -> OutputMode:
    """Return the active run-output mode.

    Surfaces gated on live / debug verbosity (the M14.4.4 live card,
    future surfaces) read this instead of guessing from
    :func:`get_verbose` or :func:`agents.get_stdout_echo`, which
    overlap but mean different things (debug is a superset of live;
    stdout echo also turns on in live mode but is the wrong
    coupling for "should I render a structured card").
    """
    return _output_mode


def apply_output_mode(mode: str | None) -> OutputMode:
    """Apply the run transcript mode to trace, previews, and agent echo.

    ``summary`` keeps stdout to phase banners and structured outcome blocks.
    ``live`` adds the formatted live agent transcript.
    ``debug`` is monotonic over ``live``: it also enables stderr trace lines
    and untruncated previews.
    """
    global _output_mode
    output_mode = normalize_output_mode(mode)
    from agents import set_stdout_echo
    from core.observability import trace

    trace.enable_trace(output_mode == "debug")
    set_stdout_echo(output_mode in {"live", "debug"})
    set_verbose(output_mode == "debug")
    _output_mode = output_mode
    return output_mode


def preview_heading(label: str, color: str = C.WHITE) -> None:
    """Print only the colored preview heading.

    Used when the logical label should appear before a long-running action,
    while the body is printed after the action completes.
    """
    print(f"\n{paint(f'{label}:', color)}")


def preview_body(text: str, n: int | None = 700) -> None:
    """Print preview body text using the same truncation rules as preview()."""
    if _verbose:
        n = None
    if not n or len(text) <= n:
        print(text)
    else:
        print(text[:n] + "…")


def preview(label: str, text: str, color: str = C.WHITE, n: int | None = 700) -> None:
    """Print a labeled, color-coded preview of ``text``.

    ``n`` caps the printed length and appends ``…`` when the text was
    truncated. Pass ``n=None`` (or anything falsy) for no cap. When the
    module-wide ``_verbose`` flag is on, the cap is dropped regardless —
    ``--verbose`` / ``-v`` runs always see full output.
    """
    preview_heading(label, color)
    preview_body(text, n)
