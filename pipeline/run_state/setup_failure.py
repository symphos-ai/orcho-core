# SPDX-License-Identifier: Apache-2.0
"""Provider-neutral terminal-state projection for setup/preflight failures.

A run can die *before any pipeline phase runs* — worktree bootstrap blows up,
a preflight check aborts, or the launching supervisor reaps an abnormal exit
that bypassed the pipeline's own ``run.end`` writer. In every one of those
shapes the durable record is honest but *thin*: ``meta.json`` may carry a
terminal ``status`` / ``halt_reason`` (or nothing at all when a SIGKILL beat
the in-process writers), yet there is no phase attempt, no ``run.end`` error
event, and no ``meta.failure`` for the existing SDK projections to surface.
The result reads as a silent failure: ``status`` says the run is over but the
errors slice is empty and nothing tells the operator what to look at.

This module is the **status/halt-reason merge rule** plus a **typed synthesis**
of that missing actionable error, both built only from durable files the run
already writes — ``meta.json``, ``events.jsonl`` (passed in), an optional
launcher state file, and a runtime log. It introduces no new artifact format.

Two responsibilities, kept pure (no clock, no SDK import):

* **Merge rule.** :func:`merged_status` / :func:`merged_halt_reason` reconcile
  the pipeline-owned ``meta`` fields with an optional launcher state file. The
  rule is *explicit and idempotent* with the companion launcher integration
  (its ``merged_status_from_meta`` / ``merged_halt_reason_from_meta``): a
  non-empty, non-``running`` ``meta.status`` always wins; the launcher state is
  consulted *only* when ``meta.status`` is empty / missing / ``running``; and
  the ``failed`` + negative-exit-code → ``interrupted`` remap lives *only* in
  the launcher branch. Keeping one implementation of the rule on the core side
  means both surfaces resolve the same status for the same run dir.
* **Synthesis gate.** :func:`detect_setup_preflight_failure` emits a typed
  setup/preflight error record **only** when nothing already represents the
  terminal cause (no phase attempts, no ``meta.failure``, no ``run.end``
  error/halt event, no active phase handoff) *and* the merged terminal state
  says the run failed. This keeps the existing provider-access (ADR 0101) and
  stalled-command (ADR 0103) projections byte-identical — the synthetic error
  never fires when a richer terminal cause is already on record.

The optional launcher state file (``mcp_supervisor.json``) is a provider-neutral
file contract written by whichever launcher supervises the run process; this
module reads it as plain JSON and never imports the launcher package. Any
embedder that supervises run processes can write the same file to feed the merge
rule. See ``docs/adr/0104-setup-preflight-terminal-state-projection.md``.

Package discipline: like the rest of ``run_state`` this module imports nothing
from runtime / resume / finalization / SDK paths. Its only IO is reading the
optional launcher state file; ``meta`` and ``events`` are passed in by callers
that already did that read once.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.run_state.status_vocab import FAILURE_TERMINAL_STATUSES

#: Launcher-written run-process state file (provider-neutral file contract).
#: Optional: absent for runs launched without a supervising launcher.
_LAUNCHER_STATE_FILENAME = "mcp_supervisor.json"

#: Conventional runtime log every run writes; the synthesized error points an
#: operator here for the failing setup/preflight command.
_RUNTIME_LOG_FILENAME = "runner.log"

#: Stable ``kind`` discriminator for a synthesized setup/preflight failure,
#: parallel to ``provider_access`` / ``stalled_command``. Read by the SDK
#: errors/next-actions projections.
SETUP_FAILURE_KIND = "setup_failed"

# ── Status / halt-reason merge rule ────────────────────────────────────────


def merged_status(meta: dict[str, Any], run_dir: Path) -> str | None:
    """Resolve the run's status from ``meta`` and the optional launcher state.

    Explicit rule, idempotent with the companion launcher integration's
    ``merged_status_from_meta``:

    1. ``meta.status`` is a non-empty ``str`` other than ``running`` → return it
       verbatim. Terminal ``meta`` **wins**; the launcher state is not consulted.
    2. else :func:`supervisor_terminal_status` if it is not ``None``.
    3. else ``meta.status`` if it is a non-empty ``str`` (the trivial
       ``running`` value, surfaced as a stable "still running" answer).
    4. else ``None`` (neither side has a status — callers treat as unknown).

    ``meta.status`` is coerced defensively: a non-string value (a crash mid-write
    can leave one) is treated as absent for the trivial-check, and only a string
    is ever returned.
    """
    meta_status = meta.get("status")
    if isinstance(meta_status, str) and meta_status not in ("", "running"):
        return meta_status
    sup_status = supervisor_terminal_status(run_dir)
    if sup_status is not None:
        return sup_status
    if isinstance(meta_status, str) and meta_status:
        return meta_status
    return None


def merged_halt_reason(meta: dict[str, Any], run_dir: Path) -> str | None:
    """Resolve the run's ``halt_reason`` from ``meta`` then the launcher state.

    The pipeline owns ``meta.halt_reason`` (set on the declared halt / failed /
    interrupted paths). When that field is absent — typically because a signal
    bypassed every in-process writer — the launcher's reap-time taxonomy
    (``signal:<NAME>`` / ``abnormal_exit:<rc>`` / ``interrupted_orphan`` /
    ``orphaned_no_supervisor``) fills in. Returns ``None`` when neither side
    has a reason.
    """
    meta_reason = meta.get("halt_reason")
    if isinstance(meta_reason, str) and meta_reason:
        return meta_reason
    return supervisor_halt_reason(run_dir)


def supervisor_terminal_status(run_dir: Path) -> str | None:
    """Return the launcher state file's terminal status, if any.

    Reads the optional launcher state file and returns:

    * its ``status`` string when terminal (``done`` / ``failed`` /
      ``interrupted`` / ``awaiting_phase_handoff`` / ``awaiting_gate_decision``
      / ``orphaned`` …), with the signal-induced ``failed`` + negative
      ``exit_code`` case remapped to ``interrupted`` so the wire vocabulary
      matches the run lifecycle (a negative exit code is a signal death, which
      the lifecycle names ``interrupted``; a positive code stays ``failed``);
    * ``None`` when the file is absent, unreadable, or holds a non-terminal
      status (``""`` / ``running`` — the launcher itself believes the run is
      still alive).

    This remap lives *only here*, in the launcher branch — never in the
    ``meta`` branch of :func:`merged_status` — so terminal ``meta`` is never
    rewritten.
    """
    state = _read_launcher_state(run_dir)
    if state is None:
        return None
    sup_status = state.get("status")
    if sup_status in (None, "", "running"):
        return None
    if sup_status == "failed":
        rc = state.get("exit_code")
        if isinstance(rc, int) and rc < 0:
            return "interrupted"
    return sup_status


def supervisor_halt_reason(run_dir: Path) -> str | None:
    """Return the launcher state file's ``halt_reason`` string, if any.

    Mirrors :func:`supervisor_terminal_status` so consumers can surface a
    reason for the signal / orphan / abnormal-exit cases that bypassed the
    pipeline's own writers. Returns ``None`` when the file is absent,
    unreadable, or carries no non-empty ``halt_reason``.
    """
    state = _read_launcher_state(run_dir)
    if state is None:
        return None
    reason = state.get("halt_reason")
    if isinstance(reason, str) and reason:
        return reason
    return None


# ── Setup/preflight failure synthesis ──────────────────────────────────────


def detect_setup_preflight_failure(
    meta: dict[str, Any],
    run_dir: Path,
    events: list[Any] | None,
) -> dict[str, Any] | None:
    """Synthesize a typed setup/preflight failure record, or ``None``.

    Returns a record **only** when *all* of the following hold:

    * **No phase attempts.** ``meta.phases`` is empty/absent and there is no
      ``release_summary`` — the run never reached a pipeline phase. Guards
      against swallowing a genuine in-phase failure.
    * **No terminal cause already on record.** No ``meta.failure`` (a
      provider-access / stalled-command failure already owns the surface), no
      active ``meta.phase_handoff`` (an operator handoff is pending), and no
      ``run.end`` event that the collector would itself surface as a breadcrumb
      — one carrying an ``error`` (``run_failed``) or a ``halted`` status
      (``run_halted``). A *bare* failed/interrupted ``run.end`` with neither
      produces no breadcrumb, so it does not count as an existing terminal cause
      and the synthesis still fires (see :func:`_has_terminal_run_end`).
    * **Merged terminal state says the run failed.** :func:`merged_status`
      resolves to ``failed`` / ``halted`` / ``interrupted`` (not ``done``,
      ``cancelled``, ``running``, or unknown).
    * **A concrete setup/preflight signal is present.** The death is attributable
      to a *setup/preflight* cause — a failed worktree-bootstrap record, or a
      launcher that reaped an abnormal exit — rather than a *deliberate* terminal
      whose reason the pipeline already wrote to ``meta.halt_reason`` (a clean
      operator/gate halt such as ``plan_rejected`` or ``phase_handoff_halt``).
      Without this the synthesis would over-fire on any meta-only ``halted`` run
      and bury a benign, already-explained halt under a synthetic error. See
      :func:`_has_setup_preflight_signal`.

    The record names the actionable cause — the merged ``halt_reason`` (e.g.
    ``worktree_bootstrap_failed``, ``signal:<NAME>``, ``abnormal_exit:<rc>``),
    the worktree-bootstrap error when present, and a pointer to the runtime log
    — so the errors slice is never empty for a silent setup/preflight death.

    Returns a plain dict with keys ``kind`` / ``message`` / ``at`` /
    ``halt_reason`` / ``runtime_log_hint``. Pure: reads only ``meta``, the
    passed ``events``, and the optional launcher state file under ``run_dir``.
    """
    # Gate 1: the run must not have reached any pipeline phase.
    if meta.get("phases") or meta.get("release_summary"):
        return None

    # Gate 2: no richer terminal cause may already be on record.
    if meta.get("failure") is not None:
        return None
    if meta.get("phase_handoff") is not None:
        return None
    if _has_terminal_run_end(events):
        return None

    # Gate 3: the merged terminal state must say the run failed.
    status = merged_status(meta, run_dir)
    if status not in FAILURE_TERMINAL_STATUSES:
        return None

    # Gate 4: the failure must carry a concrete setup/preflight signal — not a
    # clean, already-explained operator/gate halt (e.g. ``plan_rejected``).
    if not _has_setup_preflight_signal(meta, run_dir):
        return None

    halt_reason = merged_halt_reason(meta, run_dir)
    return {
        "kind": SETUP_FAILURE_KIND,
        "message": _build_setup_failure_message(meta, halt_reason),
        "at": _terminal_timestamp(meta),
        "halt_reason": halt_reason,
        "runtime_log_hint": _RUNTIME_LOG_FILENAME,
    }


def _build_setup_failure_message(
    meta: dict[str, Any], halt_reason: str | None,
) -> str:
    """Compose a human-readable, actionable setup/preflight failure message.

    Names the merged ``halt_reason`` and the worktree-bootstrap error (when the
    durable ``meta.worktree_bootstrap`` record carries one), and always points
    the operator at the runtime log for the failing setup command.
    """
    parts = [
        "Run terminated during setup/preflight before any pipeline phase ran",
    ]
    if halt_reason:
        parts.append(f"reason: {halt_reason}")
    bootstrap_error = _worktree_bootstrap_error(meta)
    if bootstrap_error:
        parts.append(f"worktree bootstrap error: {bootstrap_error}")
    parts.append(f"inspect {_RUNTIME_LOG_FILENAME} for the failing setup command")
    return "; ".join(parts) + "."


def _has_setup_preflight_signal(meta: dict[str, Any], run_dir: Path) -> bool:
    """True when the terminal failure is attributable to setup/preflight.

    A meta-only ``halted`` / ``failed`` run is *not enough* on its own — a clean
    operator/gate halt (``plan_rejected``, ``phase_handoff_halt``) also leaves a
    terminal ``meta`` with a ``halt_reason`` and empty ``phases``, and that
    reason is already surfaced. The synthesis must fire only when there is a
    concrete *setup/preflight* signal:

    * a **failed worktree-bootstrap** record (``meta.worktree_bootstrap.error``)
      — bootstrap blew up before any phase ran; or
    * a **launcher-reaped abnormal exit** — either the launcher recorded an
      abnormal-exit ``halt_reason`` (``signal:<NAME>`` / ``abnormal_exit:<rc>`` /
      orphan taxonomy), or the launcher *drove* the terminal status because the
      pipeline never wrote one (``meta.status`` empty / missing / ``running``).

    Both acceptance shapes (worktree bootstrap failure; launcher abnormal exit)
    match; a clean meta halt with no bootstrap record and no launcher state does
    not.
    """
    if _worktree_bootstrap_error(meta) is not None:
        return True
    if supervisor_halt_reason(run_dir) is not None:
        return True
    meta_status = meta.get("status")
    meta_status_trivial = (
        not isinstance(meta_status, str) or meta_status in ("", "running")
    )
    return meta_status_trivial and supervisor_terminal_status(run_dir) is not None


def _worktree_bootstrap_error(meta: dict[str, Any]) -> str | None:
    """Return ``meta.worktree_bootstrap.error`` when the record failed.

    The isolation setup writes ``{"status": "failed", "error": <str>}`` into
    ``meta.worktree_bootstrap`` when a bootstrap step blows up. Returns the
    error string only for that failed shape; ``None`` otherwise.
    """
    bootstrap = meta.get("worktree_bootstrap")
    if not isinstance(bootstrap, dict):
        return None
    error = bootstrap.get("error")
    if isinstance(error, str) and error:
        return error
    return None


def _terminal_timestamp(meta: dict[str, Any]) -> str | None:
    """Best durable terminal timestamp from ``meta`` (``halted``/``interrupted``).

    Reads the already-persisted ``halted_at`` / ``interrupted_at`` stamp — no
    clock is consulted. Returns ``None`` when the thin meta carries neither.
    """
    for key in ("halted_at", "interrupted_at"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _has_terminal_run_end(events: list[Any] | None) -> bool:
    """True when ``events`` carries a ``run.end`` that already surfaces an error.

    This must stay aligned with what
    :func:`pipeline.evidence.collector._build_errors` actually turns into an
    ``errors`` breadcrumb from a ``run.end`` event: a ``run_failed`` record when
    the payload carries an ``error``, and a ``run_halted`` record when the
    payload ``status`` is ``halted``. Only those two shapes mean a richer
    terminal cause is *already on record*, so only those may suppress the
    setup/preflight synthesis.

    A *bare* ``run.end`` with ``status='failed'`` / ``'interrupted'`` /
    ``'cancelled'`` and no ``error`` produces **no** breadcrumb in the collector;
    treating it as terminal here would suppress the synthesis and leave the
    errors slice empty for exactly the silent setup/preflight death this module
    exists to surface. So such events do *not* count as terminal. Tolerant of
    both :class:`Event` objects (``.kind`` / ``.payload``) and plain
    ``{"kind", "payload"}`` dicts.
    """
    if not events:
        return False
    for evt in events:
        kind, payload = _event_kind_payload(evt)
        if kind != "run.end":
            continue
        if not isinstance(payload, dict):
            # A run.end with no readable payload still marks a finalize boundary.
            return True
        # Mirror collector._build_errors: only ``error`` (→ run_failed) or a
        # ``halted`` status (→ run_halted) yields an actual breadcrumb.
        if payload.get("error"):
            return True
        if payload.get("status") == "halted":
            return True
    return False


def _event_kind_payload(evt: Any) -> tuple[Any, Any]:
    """Extract ``(kind, payload)`` from an Event object or a plain dict."""
    if isinstance(evt, dict):
        return evt.get("kind"), evt.get("payload")
    return getattr(evt, "kind", None), getattr(evt, "payload", None)


def _read_launcher_state(run_dir: Path) -> dict[str, Any] | None:
    """Load the optional launcher state file once; ``None`` on any read error.

    Provider-neutral file contract: plain JSON written by whichever launcher
    supervises the run process. Absent / unreadable / non-dict → ``None``.
    """
    state_path = run_dir / _LAUNCHER_STATE_FILENAME
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


__all__ = [
    "SETUP_FAILURE_KIND",
    "detect_setup_preflight_failure",
    "merged_halt_reason",
    "merged_status",
    "supervisor_halt_reason",
    "supervisor_terminal_status",
]
