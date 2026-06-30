"""
core/observability/events.py — JSONL event-store for live pipeline progress.

Single source of truth for pipeline progress. Replaces the historical trio of
parallel channels (progress.log, output.log, subprocess stdout) for both CLI
watcher and web dashboard. Old log files keep being written for backward
compat in [core/observability/logging.py](core/observability/logging.py) and
[agents/stream.py](agents/stream.py), but events.jsonl is authoritative.

Public surface:
    Event          — frozen dataclass for one event
    init_event_store(run_dir)            — open events.jsonl, reset seq
    emit(kind, **payload)                — thread-safe append
    set_phase(phase)                     — update active phase tag
    current_run_dir()                    — Path | None of active store
    read_all(run_dir)                    — list[Event] for replay
    tail(run_dir, since_seq=0, poll=0.3) — generator yielding new events

Schema (one line per event, JSON):
    {"seq": int, "ts": ISO-8601, "kind": str, "phase": str|None, "payload": {...}}

Threading:
    A single module-level RLock guards the writer. Multiple producer threads
    (orchestrator main thread, agents.stream callback thread) can call emit()
    concurrently. The reader (tail) does NOT take the lock — it only re-reads
    the file, which is append-only, so partial-line reads are handled by
    skipping the in-progress line until the next poll.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Module state ─────────────────────────────────────────────────────────────
# These are intentionally module-level globals: a pipeline run is a process-
# wide singleton (one orchestrator per python -m cli.orcho invocation). Web
# dashboard runs each pipeline as a fresh subprocess, so isolation is
# guaranteed at the process boundary.
_lock: threading.RLock = threading.RLock()
_path: Path | None = None
_seq:  int = 0
_phase: str | None = None
# Phase 7.10-followup: canonical phase context for machine consumers
# (UI / MCP / decision-provenance graph). ``_phase`` is the
# human-readable display string (UPPERCASE ``PLAN`` / ``VALIDATE_PLAN`` /
# …); ``_phase_key`` is the lowercase canonical handler-registry key
# (``plan`` / ``validate_plan`` / …); ``_round`` is the 1-based loop
# round / attempt counter (singleton phases stay at 1). All three are
# attached to every event payload automatically by ``emit()`` so
# graph-edge builders don't need to track phase boundaries themselves.
_phase_key: str | None = None
_round: int | None = None
# Human-readable banner title for the active phase (e.g. ``CONTRACT CHECK
# — Codex reviews cross-project consistency``). Render-only: never emitted
# into event payloads. The transcript renderer reads it via
# ``current_phase_header`` to synthesise a section title above runtime
# invocations that fire without an immediately-preceding banner.
_phase_title: str | None = None

# Optional in-process fan-out hook for the replay-first event hub
# (ADR 0048). When set, ``emit()`` calls this AFTER the durable write
# completes, inside the lock so seq ordering is preserved. The hook
# is sync (`put_nowait` semantics); it must not perform I/O or
# block. Exceptions are caught — the durable file write is the
# source of truth and never depends on the hook succeeding.
#
# Wired by ``core.observability.event_hub`` on first import via
# ``set_publish_hook`` below. When the hub module is never
# imported, the hook stays ``None`` and ``emit()`` runs unchanged.
from collections.abc import Callable  # noqa: E402  # forward use only

_publish_hook: Callable[[Path, Event], None] | None = None


# ── Public dataclass ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Event:
    """One pipeline event. Immutable, JSON-serializable."""
    seq:     int
    ts:      str           # ISO-8601 with milliseconds, local time
    kind:    str
    phase:   str | None
    payload: dict


# ── Lifecycle ────────────────────────────────────────────────────────────────
def init_event_store(run_dir: Path | None, *, resume: bool = False) -> Path | None:
    """Initialize event-store for a pipeline run.

    Creates ``run_dir/events.jsonl``. By default the file is truncated and
    ``seq`` resets to 0 — fresh runs start clean.

    When ``resume=True`` and an existing events.jsonl is found in run_dir,
    the file is preserved and ``seq`` continues from the last recorded
    event. New events from the resumed run append to the same stream so
    consumers (dashboard reducer, orcho-watch) see one continuous timeline
    spanning the original run and the resumed continuation. This matters
    for audit trails — validate_plan gate Approve resumes that previously
    clobbered ``validate_plan.verdict`` events now keep them.

    Pass None for ``run_dir`` to disable the store (standalone CLI
    invocations without --output-dir). Subsequent emit() calls become
    no-ops.

    Returns the events.jsonl path (or None when disabled).
    """
    global _path, _seq, _phase
    with _lock:
        if run_dir is None:
            _path = None
            _seq = 0
            _phase = None
            return None
        run_dir.mkdir(parents=True, exist_ok=True)
        _path = run_dir / "events.jsonl"
        _phase = None

        if resume and _path.exists() and _path.stat().st_size > 0:
            # Continue the existing stream. Walk the file once to find the
            # highest seq, so the next emit() picks up where the parent
            # run left off (no duplicate seq numbers).
            last_seq = 0
            try:
                for line in _path.read_text(encoding="utf-8",
                                            errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    s = int(d.get("seq", 0))
                    if s > last_seq:
                        last_seq = s
            except OSError:
                last_seq = 0
            _seq = last_seq
        else:
            _seq = 0
            _path.write_text("", encoding="utf-8")
        return _path


def current_run_dir() -> Path | None:
    """Return the run dir backing the active store, or None."""
    with _lock:
        return _path.parent if _path else None


def set_publish_hook(
    hook: Callable[[Path, Event], None] | None,
) -> None:
    """Install (or clear) the in-process fan-out hook for ADR 0048.

    Called once by ``core.observability.event_hub`` at module import
    so :func:`emit` can deliver each event to active subscribers in
    the same process. Passing ``None`` clears the hook — useful in
    test teardown to leave the events module in a pristine state.

    The hook signature is ``(run_dir: Path, event: Event) -> None``.
    It runs inside the events module's lock after the durable write
    succeeds; implementations MUST be sync, non-blocking, and never
    raise (raises are swallowed by :func:`emit` to keep the durable
    path independent of the hub).
    """
    global _publish_hook
    with _lock:
        _publish_hook = hook


# ── Phase tracking ───────────────────────────────────────────────────────────
def set_phase(phase: str | None) -> None:
    """Update the phase tag attached to subsequent events.

    Call this on phase.start; phase.end can pass None or set the next phase.
    Reading the phase is a quick lock acquisition — emit() does it
    automatically so callers normally don't read it directly.
    """
    global _phase
    with _lock:
        _phase = phase


def set_phase_context(
    *,
    phase: str | None = None,
    phase_key: str | None = None,
    round: int | None = None,
    title: str | None = None,
) -> None:
    """Set the full phase context (display string + canonical key + round).

    Companion to :func:`set_phase` for machine consumers — every event
    emitted while this context is active carries ``phase_key`` and
    ``round`` in its payload so a UI / decision-provenance graph can
    group events by canonical handler key and loop iteration without
    re-deriving from the display string.

    ``title`` is the human banner label for the phase; it is render-only
    (never emitted into payloads) and surfaced via
    :func:`current_phase_header` so the transcript can guarantee a section
    title above every runtime invocation.

    Pass ``None`` to any field to clear it. Typical callsite: phase.start
    in the orchestrator sets all three; phase.end resets them.
    """
    global _phase, _phase_key, _round, _phase_title
    with _lock:
        if phase is not None or phase == "":  # explicit empty means "set to empty"
            _phase = phase
        _phase_key = phase_key
        _round = round
        _phase_title = title


def clear_phase_context() -> None:
    """Reset all phase-context fields. Companion to ``set_phase_context``."""
    global _phase, _phase_key, _round, _phase_title
    with _lock:
        _phase = None
        _phase_key = None
        _round = None
        _phase_title = None


def current_phase() -> str | None:
    """Return the active phase display string, or ``None`` when unset.

    Machine-readable companion to :func:`current_phase_header` (which is
    render-only). Runtime adapters stamp this onto the bounded
    ``StalledCommand`` carriers they build so a stall diagnostic names the
    phase it occurred in even before the event store injects ``phase`` at
    emit time.
    """
    with _lock:
        return _phase


def current_phase_header() -> tuple[str, str] | None:
    """Return ``(phase_display, title)`` for the active phase, or ``None``.

    Render-layer helper: :func:`core.io.transcript.render_agent_invocation`
    uses this to synthesise a section header above a runtime invocation
    when the phase fired multiple invocations under a single banner (e.g.
    the cross contract-check loop iterating projects). The title falls
    back to the display string when no banner label was recorded.
    """
    with _lock:
        if _phase is None:
            return None
        return (_phase, _phase_title or _phase)


# ── Emit ─────────────────────────────────────────────────────────────────────
def emit(kind: str, **payload: Any) -> None:
    """Append one event to the store. No-op if init_event_store was never
    called or was called with None.

    Thread-safe: multiple producers serialize on the module lock. The write
    is one ``json.dumps + "\\n"`` chunk + flush, so readers tailing the file
    never see partial JSON lines (POSIX append on small writes is atomic
    enough for our line size; we still flush + os.fsync-light via flush
    only — no fsync for performance).
    """
    global _seq
    with _lock:
        if _path is None:
            return
        _seq += 1
        # Inject canonical phase context (Phase 7.10-followup) so every
        # event payload carries ``phase_key`` and ``round`` for machine
        # consumers, alongside the human ``phase`` display string at the
        # top-level Event field. Caller-supplied values in ``payload``
        # win (per-call override is sometimes useful).
        merged_payload: dict[str, Any] = {}
        if _phase_key is not None:
            merged_payload["phase_key"] = _phase_key
        if _round is not None:
            merged_payload["round"] = _round
        merged_payload.update(payload)
        evt = Event(
            seq=_seq,
            ts=_now_iso(),
            kind=kind,
            phase=_phase,
            payload=_clean_payload(merged_payload),
        )
        line = json.dumps(asdict(evt), ensure_ascii=False) + "\n"
        # Open-append-close on every write keeps the file safe across
        # multiple python processes (cross orchestrator forks per project)
        # and avoids holding a long-lived FH that may break under EINTR /
        # log-rotation. The lock keeps lines from interleaving.
        with _path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
        # ADR 0048 D2: in-process fan-out for active subscribers, AFTER
        # the durable write completes. Hook is None when the hub module
        # was never imported — zero overhead in that case. Sync only;
        # never blocks. Exceptions never propagate (durable file is the
        # source of truth; the hub is best-effort acceleration).
        if _publish_hook is not None:
            with contextlib.suppress(Exception):
                _publish_hook(_path.parent, evt)


def append_event(
    run_dir: Path,
    kind: str,
    payload: dict | None = None,
    *,
    phase: str | None = None,
) -> int:
    """Append an event to ``<run_dir>/events.jsonl`` from any process.

    Public API (P2.5). Unlike :func:`emit`, this writes to a specific run's
    event store independent of the caller's own process state — useful when
    an external supervisor (orcho-mcp) needs to record events into a run
    after the pipeline subprocess has exited (e.g. ``run.orphaned``,
    synthetic ``run.end``).

    Reads ``max(seq)`` from the existing file to assign the next sequence
    number, then appends one JSON line under an exclusive ``fcntl.flock``
    lock so concurrent appenders can't interleave. POSIX-only locking; on
    Windows the lock is a no-op (acceptable for v1 since the supervisor
    targets POSIX).

    Args:
        run_dir: directory containing (or to contain) ``events.jsonl``.
        kind: event kind (e.g. ``"run.orphaned"``, ``"qa.decided"``).
        payload: arbitrary key/value dict; cleaned via the same rules
            as :func:`emit`.
        phase: optional phase tag.

    Returns:
        The seq number assigned to the appended event.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    events_path = run_dir / "events.jsonl"

    # Best-effort POSIX file lock; on Windows we fall through without locking
    # (rare race, acceptable for v1 supervisor which is POSIX-only anyway).
    try:
        import fcntl  # type: ignore[import-not-found]
        _has_flock = True
    except ImportError:
        fcntl = None  # type: ignore[assignment]
        _has_flock = False

    # ``a+`` opens for append + read; the read cursor starts at 0 so we
    # can scan the whole file for max seq before writing.
    with events_path.open("a+", encoding="utf-8") as f:
        if _has_flock:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            max_seq = 0
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    seq = int(json.loads(line).get("seq", 0))
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
                if seq > max_seq:
                    max_seq = seq

            next_seq = max_seq + 1
            evt = Event(
                seq=next_seq,
                ts=_now_iso(),
                kind=kind,
                phase=phase,
                payload=_clean_payload(payload or {}),
            )
            f.write(json.dumps(asdict(evt), ensure_ascii=False) + "\n")
            f.flush()
            return next_seq
        finally:
            if _has_flock:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _now_iso() -> str:
    """ISO-8601 with milliseconds, local time. e.g. 2026-05-04T13:10:01.123."""
    now = datetime.now()
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def _clean_payload(payload: dict) -> dict:
    """Drop keys with None values to keep events.jsonl compact. Truncate
    very long string fields to a hard cap (16 KiB) so a single huge tool
    input can't blow up the file. Long values get a "_truncated" marker.
    """
    cleaned: dict = {}
    for k, v in payload.items():
        if v is None:
            continue
        if isinstance(v, str) and len(v) > 16384:
            cleaned[k] = v[:16384]
            cleaned[f"_{k}_truncated"] = len(v)
        else:
            cleaned[k] = v
    return cleaned


# ── Read (replay) ────────────────────────────────────────────────────────────
def read_all(run_dir: Path) -> list[Event]:
    """Read full event-store from disk. Returns events in seq order.

    Tolerates partial last lines (writer crashed mid-write) — they're
    silently skipped. Any other JSON error is logged to stderr and the
    line is skipped.
    """
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    events: list[Event] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            events.append(Event(
                seq=int(d.get("seq", 0)),
                ts=str(d.get("ts", "")),
                kind=str(d.get("kind", "")),
                phase=d.get("phase"),
                payload=dict(d.get("payload") or {}),
            ))
        except (json.JSONDecodeError, ValueError, TypeError):
            # Partial last line or corrupt entry — skip silently, don't
            # taint the replay with synthetic placeholders.
            continue
    return events


# ── Tail (live) ──────────────────────────────────────────────────────────────
def tail(
    run_dir: Path,
    since_seq: int = 0,
    poll: float = 0.3,
    stop_predicate=None,
) -> Iterator[Event]:
    """Yield events as they appear in events.jsonl.

    Args:
        run_dir:        Directory containing events.jsonl. Must exist.
        since_seq:      Only yield events with seq > since_seq. Set to the
                        last-seen seq when reconnecting.
        poll:           Seconds between disk re-reads when at EOF.
        stop_predicate: Optional callable() -> bool. When True, stop
                        polling and exit. Called between polls.

    Returns:
        Iterator[Event]. Caller can break out at any time.

    Notes:
        - We re-read the entire file each poll and skip already-seen seq.
          For typical run sizes (≤ 10k events) this is negligible. If the
          file grows huge, switch to ``f.seek(_offset)`` tail.
        - A trailing partial line (writer mid-flush) is detected by
          json.loads failing — we skip it and re-try next poll.
    """
    path = run_dir / "events.jsonl"
    last_seq = since_seq
    while True:
        if path.exists():
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                seq = int(d.get("seq", 0))
                if seq <= last_seq:
                    continue
                last_seq = seq
                yield Event(
                    seq=seq,
                    ts=str(d.get("ts", "")),
                    kind=str(d.get("kind", "")),
                    phase=d.get("phase"),
                    payload=dict(d.get("payload") or {}),
                )
        if stop_predicate and stop_predicate():
            return
        time.sleep(poll)
