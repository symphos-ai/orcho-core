"""
pipeline/project/handoff_waiver.py — waiver lifecycle helpers (ADR 0073).

A ``phase_handoff_waiver`` is the durable record that a rejected/incomplete
phase verdict was accepted by an authority (an operator on resume, or — under
the implement substance-repair fallback with ``on_exhausted='auto_waiver'`` —
the pipeline itself). Downstream review gates read it and do not reopen the
waived findings.

This module owns the two waiver-state operations the implement auto-waiver path
needs, kept out of the already-large ``pipeline/project/handoff.py``:

* :func:`apply_waiver_to_state` — write the waiver onto ``state.extras`` (the
  in-process source of truth). It REQUIRES a non-empty ``waiver_text``: a
  waiver with no rationale is meaningless and is rejected.
* :func:`sync_waiver_to_session` — durably mirror the ``state.extras`` waiver
  onto ``run.session`` so a fresh-process resume (MCP / Web) rehydrates it.
  Conflict-aware: re-syncing the same payload is a no-op; a *different* waiver
  already on the session for is a conflict and raises rather than silently
  overwriting the audit record.

``decided_by`` is applier-set provenance (``operator`` or ``auto:on_exhausted``)
recorded directly on the waiver payload here. It deliberately does NOT travel
through the decision-artifact bridge (:mod:`pipeline.control.handoff_decisions`)
and is NOT part of the decision-artifact idempotency comparison — the artifact
wire-format stays unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

#: ``state.extras`` / ``session`` key holding the active waiver payload.
WAIVER_KEY = "phase_handoff_waiver"


def apply_waiver_to_state(
    state: Any,
    *,
    handoff_id: str,
    phase: str,
    waiver_text: str,
    decided_by: str,
    note: str | None = None,
    decided_at: str | None = None,
    findings: Any = None,
    critique: str = "",
) -> dict[str, Any]:
    """Write a ``phase_handoff_waiver`` onto ``state.extras`` and return it.

    ``waiver_text`` is mandatory and must be non-empty — it records *why* the
    rejected/incomplete findings are accepted; an empty rationale raises
    ``ValueError``. ``decided_by`` is the applier-set provenance string
    (``operator`` for a human resume decision, ``auto:on_exhausted`` for the
    automatic implement fallback). ``decided_at`` defaults to the current UTC
    timestamp when not supplied.

    The payload mirrors the shape persisted by the operator
    ``continue_with_waiver`` path (``handoff_id`` / ``phase`` / ``waiver_text``
    / ``note`` / ``decided_at`` / ``findings`` / ``critique``) plus
    ``decided_by``, so the same downstream review gates read it unchanged.
    """
    if not isinstance(waiver_text, str) or not waiver_text.strip():
        raise ValueError(
            "apply_waiver_to_state: waiver_text must be a non-empty string — "
            "a waiver must record why the rejected/incomplete findings are "
            "accepted."
        )
    waiver = {
        "handoff_id":  handoff_id,
        "phase":       phase,
        "waiver_text": waiver_text,
        "note":        note,
        "decided_at":  decided_at
        or datetime.now(UTC).isoformat(timespec="seconds"),
        "findings":    findings,
        "critique":    critique,
        "decided_by":  decided_by,
    }
    state.extras[WAIVER_KEY] = waiver
    return waiver


def sync_waiver_to_session(run: Any) -> None:
    """Durably mirror the ``state.extras`` waiver onto ``run.session``.

    Idempotent and conflict-aware:

    * no waiver on ``state.extras`` → no-op;
    * the session already holds an *equal* waiver → no-op (re-sync is safe);
    * the session holds a *different* waiver → :class:`RuntimeError`, because
      overwriting a distinct persisted waiver would corrupt the audit record.

    Mirrors the in-memory ``run.session`` payload only (the same mirror the
    operator ``continue_with_waiver`` path performs); persisting the session to
    ``meta.json`` is the caller's existing phase-end / resume responsibility.
    """
    waiver = run.state.extras.get(WAIVER_KEY)
    if waiver is None:
        return
    existing = run.session.get(WAIVER_KEY)
    if existing is not None and existing != waiver:
        raise RuntimeError(
            "sync_waiver_to_session: run.session already holds a different "
            f"{WAIVER_KEY} (handoff {existing.get('handoff_id')!r}); refusing "
            f"to overwrite with handoff {waiver.get('handoff_id')!r}. A "
            "conflicting waiver indicates inconsistent decision state."
        )
    run.session[WAIVER_KEY] = waiver
