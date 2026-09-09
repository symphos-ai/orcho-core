"""pipeline/project/followup_supersede.py — close the parent once its correction child delivers.

A rejected-FA / ``fix``-parked parent is superseded by the ordinary correction
follow-up that delivers its diff: the parent settles to ``done`` and carries a
durable ``superseded_by_followup`` marker so the delivery gate, diagnosis, and
live status read it as closed rather than as an active correction candidate.

Two producers reach that moment. A live child run reaches it in its own
finalization (``pipeline.project.finalization``). A child parked on a
deferred delivery gate reaches it later, out of band, when the operator
decides the gate through ``sdk.run_control.delivery`` — the child's finalize
already ran with ``status='pending'`` and could not supersede anything. Both
call this one seam; the pure parent-meta mutation stays in
:func:`pipeline.run_state.terminal_outcome.supersede_parent_meta`.
"""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

from pipeline.engine import save_session
from pipeline.run_state.terminal_outcome import supersede_parent_meta

__all__ = ["DELIVERED_STATUSES", "supersede_parent_after_child_delivery"]

# ``commit_delivery.status`` values that mean the child shipped (or explicitly
# retained) the diff; only these close out the parent.
DELIVERED_STATUSES: frozenset[str] = frozenset(
    {"committed", "applied_uncommitted", "skipped"},
)


def supersede_parent_after_child_delivery(
    child_meta: Mapping[str, Any],
    child_run_dir: Path | str | None,
    *,
    child_run_id: str | None = None,
    parent_run_id: str | None = None,
) -> str | None:
    """Supersede the parent of a delivered correction child; return its run id.

    Guards (any miss is a silent no-op, never an exception):

    * ``child_run_dir`` is set and the child is an ordinary correction follow-up
      — ``parent_run_id`` on the meta (or the explicit fallback the live run
      passes from its extras), ``resume_mode='followup'``,
      ``profile='correction'``, and a ``correction_context.md`` in the run dir;
    * the child's ``commit_delivery.status`` is in :data:`DELIVERED_STATUSES`;
    * the parent resolves under the child's runs dir and is genuinely a
      rejected-FA / ``commit_decision_fix`` terminal (idempotent: an already
      superseded parent reads ``done`` and is left alone).

    Returns the parent run id when the parent meta was rewritten, else ``None``.
    """
    if not child_run_dir:
        return None
    run_dir = Path(child_run_dir)
    parent = child_meta.get("parent_run_id")
    if not isinstance(parent, str) or not parent:
        parent = parent_run_id
    if not isinstance(parent, str) or not parent:
        return None
    if (
        child_meta.get("resume_mode") != "followup"
        or child_meta.get("profile") != "correction"
        or not (run_dir / "correction_context.md").is_file()
    ):
        return None
    delivery = child_meta.get("commit_delivery")
    delivery_status = str(delivery.get("status")) if isinstance(delivery, Mapping) else ""
    if delivery_status not in DELIVERED_STATUSES:
        return None

    try:
        from pipeline.control.resume_context import (
            is_terminal_commit_decision_fix,
            is_terminal_final_acceptance_rejected,
        )
        from sdk.runs import find_run, load_meta

        ref = find_run(parent, runs_dir=run_dir.parent, cwd=None)
        parent_meta = load_meta(ref.run_dir)
    except Exception:  # noqa: BLE001 — a cross-run reconcile must never break the caller
        return None
    if not isinstance(parent_meta, MutableMapping):
        return None
    if not (
        is_terminal_final_acceptance_rejected(parent_meta)
        or is_terminal_commit_decision_fix(parent_meta)
    ):
        return None

    supersede_parent_meta(
        parent_meta,
        child_run_id=child_run_id or run_dir.name,
        child_status=str(child_meta.get("status") or "done"),
        delivery_status=delivery_status,
    )
    try:
        save_session(ref.run_dir, parent_meta)
    except Exception:  # noqa: BLE001 — a failed parent write must not break the caller
        return None
    return parent
