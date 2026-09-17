# SPDX-License-Identifier: Apache-2.0
"""Artifact-only public projection of live verification-gate progress (ADR 0190).

A read-only projection over ``<run_dir>/events.jsonl`` that answers one
question for a watcher (CLI, ``orcho-mcp`` live-status): *is a gate command
running right now, and what has it produced so far?* It reads the durable
``gate.progress`` stream the producer coalesces
(:mod:`pipeline.verification_progress`) and pairs it with the settled
``gate.end`` boundary by ``invocation_id``.

Contract:

* Returns the **latest invocation** only — a rerun of the same command in a
  repair loop is a distinct ``invocation_id`` and supersedes the earlier one.
* Returns ``None`` when the run has **no** ``gate.progress`` events (historical
  runs stay readable), when the latest invocation has **settled** (a matching
  ``gate.end`` exists), or when the **run is terminal** (a ``run.end`` exists).
  It never advertises a finished command as still running.
* Purely observational: it reports facts (timestamps, byte counters, bounded
  tails). It never re-parses the log, never fabricates a percentage, and never
  asserts process health.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from core.observability.event_kinds import EventKind
from core.observability.events import read_all
from sdk.runs import _CWD_DEFAULT, find_run


@dataclass(frozen=True, slots=True)
class GateProgressSnapshot:
    """The live state of the currently-running gate command, or nothing.

    All fields are facts projected from the durable event stream. ``exit_code``
    and ``outcome`` are populated only for a settled execution — and
    :func:`read_active_gate_progress` never returns a settled snapshot, so for
    every value it returns ``execution_state`` is ``"running"`` and those two
    are ``None``. The fields exist because they are part of the load-bearing
    wire contract shared with ``orcho-mcp``.
    """

    command: str
    hook: str
    phase: str
    invocation_id: str
    started_at: str
    observed_at: str
    elapsed_s: float
    last_output_at: str | None
    stdout_tail: str
    stderr_tail: str
    has_output: bool
    execution_state: Literal["running", "settled"]
    exit_code: int | None
    outcome: str | None


def read_active_gate_progress(
    run_id: str | None = None,
    *,
    workspace: str | None = None,
    runs_dir: str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> GateProgressSnapshot | None:
    """Project the active gate's live progress for ``run_id`` (latest invocation).

    Read-only over ``events.jsonl`` (``find_run`` + ``read_all``). Returns
    ``None`` for a run with no progress events, a settled latest invocation, or
    a terminal run — see the module docstring. Raises ``NoWorkspace`` /
    ``RunNotFound`` through :func:`sdk.runs.find_run`.
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    events = read_all(ref.run_dir)
    if not events:
        return None

    progress = [e for e in events if e.kind == EventKind.GATE_PROGRESS]
    if not progress:
        return None  # historical run without progress data — stays readable.

    latest = progress[-1]  # read_all yields seq order; newest progress wins.
    invocation_id = str(latest.payload.get("invocation_id") or "")
    if not invocation_id:
        return None

    # Never advertise a finished command as running: a terminal run, or a
    # settled boundary for this invocation, means there is no active gate.
    if any(e.kind == EventKind.RUN_END and e.seq > latest.seq for e in events):
        return None
    if any(
        e.kind == EventKind.GATE_END
        and str(e.payload.get("invocation_id") or "") == invocation_id
        for e in events
    ):
        return None

    inv_events = [
        e for e in progress
        if str(e.payload.get("invocation_id") or "") == invocation_id
    ]
    newest = inv_events[-1]
    payload = newest.payload

    started_at = ""
    for e in events:
        if (
            e.kind == EventKind.GATE_START
            and str(e.payload.get("invocation_id") or "") == invocation_id
        ):
            started_at = e.ts
            break
    if not started_at:
        started_at = inv_events[0].ts

    return GateProgressSnapshot(
        command=str(payload.get("name") or ""),
        hook=str(payload.get("hook") or ""),
        phase=str(newest.phase or ""),
        invocation_id=invocation_id,
        started_at=started_at,
        observed_at=newest.ts,
        elapsed_s=_as_float(payload.get("elapsed_s")),
        last_output_at=payload.get("last_output_at"),
        stdout_tail=str(payload.get("stdout_tail") or ""),
        stderr_tail=str(payload.get("stderr_tail") or ""),
        has_output=bool(payload.get("has_output", False)),
        execution_state="running",
        exit_code=None,
        outcome=None,
    )


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


__all__ = ["GateProgressSnapshot", "read_active_gate_progress"]
