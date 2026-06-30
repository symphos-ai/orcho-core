"""Read-only assembly of :class:`RunSnapshot` from durable run artifacts.

``load_run_snapshot`` composes existing SDK readers — it never parses
durable files by hand and never writes, normalizes, or migrates anything
on disk:

- :func:`sdk.runs.find_run` resolves the run (propagating
  ``NoWorkspace`` / ``RunNotFound``);
- :func:`sdk.runs.load_meta` / :func:`sdk.runs.load_json_optional` read
  ``meta.json`` tolerantly;
- :func:`sdk.phase_handoff.load_active_phase_handoff` yields the active
  single-project handoff payload;
- :func:`pipeline.cross_project.checkpoint.read_cross_checkpoint` yields
  the cross-run resume-hint checkpoint.

Pending-action precedence: for a cross run the cross checkpoint is
authoritative at the cross level, so checkpoint-derived handoff and gate
forms are resolved before the single-project ``meta.phase_handoff`` form.
A single-project run has no checkpoint file (the reader returns its empty
default), so it falls straight through to the ``meta.phase_handoff``
branch.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.cross_project.checkpoint import read_cross_checkpoint
from sdk.phase_handoff import load_active_phase_handoff
from sdk.run_control.types import PendingOperatorAction, RunSnapshot
from sdk.runs import _CWD_DEFAULT, find_run, load_meta
from sdk.types import PhaseStatus

_AWAITING_PHASE_HANDOFF = "awaiting_phase_handoff"
_AWAITING_GATE = "awaiting_gate_decision"


def load_run_snapshot(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> RunSnapshot:
    """Build a client-neutral :class:`RunSnapshot` for a run.

    Resolves the run via :func:`sdk.runs.find_run` (or the newest run when
    ``run_id`` is ``None``), projects the focal control fields from
    ``meta.json``, enumerates sub-run rows, and resolves at most one
    pending operator action. Purely read-only.

    Raises:
        NoWorkspace / RunNotFound: propagated from ``find_run``.
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    raw_meta = load_meta(ref.run_dir)

    phases_raw = raw_meta.get("phases") if isinstance(raw_meta, dict) else None
    phases = tuple(phases_raw.keys()) if isinstance(phases_raw, dict) else ()

    pending = _resolve_pending_action(
        ref.run_id,
        ref.run_dir,
        raw_meta,
        workspace=workspace,
        runs_dir=runs_dir,
        cwd=cwd,
    )

    return RunSnapshot(
        run_id=ref.run_id,
        run_dir=ref.run_dir,
        status=raw_meta.get("status"),
        task=str(raw_meta.get("task", "")),
        project=raw_meta.get("project"),
        profile=raw_meta.get("profile"),
        phases=phases,
        sub_runs=_collect_sub_runs(ref.run_dir),
        worktree=raw_meta.get("worktree") if isinstance(raw_meta, dict) else None,
        pending_action=pending,
        raw_meta=raw_meta,
    )


def _collect_sub_runs(run_dir: Path) -> tuple[PhaseStatus, ...]:
    """Enumerate visible sub-run directories as ``PhaseStatus`` rows.

    Mirrors :func:`sdk.status.load_status`'s sub-project enumeration:
    every non-hidden sub-directory becomes one row, ``status`` taken from
    its own ``meta.json`` (``None`` when not yet written). No terminal
    layer is touched.
    """
    rows: list[PhaseStatus] = []
    for sd in sorted(
        p for p in run_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    ):
        sub_meta = load_meta(sd)
        rows.append(
            PhaseStatus(name=sd.name, status=sub_meta.get("status") if sub_meta else None)
        )
    return tuple(rows)


def _resolve_pending_action(
    run_id: str,
    run_dir: Path,
    raw_meta: dict[str, Any],
    *,
    workspace: Path | str | None,
    runs_dir: Path | str | None,
    cwd: Path | str | None | object,
) -> PendingOperatorAction | None:
    """Resolve the single pending operator action, or ``None``.

    Precedence (cross checkpoint authoritative at cross level):

    1. cross handoff — ``checkpoint["phase_handoff_pending"]``;
    2. gate — ``checkpoint["pending_gate"]`` or
       ``meta.status == "awaiting_gate_decision"``;
    3. single-project handoff — ``meta.status == "awaiting_phase_handoff"``
       with a ``meta.phase_handoff`` payload.
    """
    checkpoint = read_cross_checkpoint(run_dir)
    meta_status = raw_meta.get("status") if isinstance(raw_meta, dict) else None

    if checkpoint.get("phase_handoff_pending"):
        # The active ``meta.phase_handoff`` payload (persisted for cross
        # pauses too, via ``apply_cross_phase_handoff_pause`` ->
        # ``save_cross_session``) is the only sanctioned source of
        # ``available_actions`` and the handoff ``id``; the checkpoint
        # carries just the dispatch fields.
        active = load_active_phase_handoff(
            run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd,
        )
        return _cross_handoff_action(run_id, checkpoint, active)

    pending_gate = checkpoint.get("pending_gate")
    if isinstance(pending_gate, dict) or meta_status == _AWAITING_GATE:
        return PendingOperatorAction(
            run_id=run_id,
            kind="gate",
            handoff_kind=None,
            available_actions=(),
            raw=dict(pending_gate) if isinstance(pending_gate, dict) else {},
        )

    if meta_status == _AWAITING_PHASE_HANDOFF:
        payload = load_active_phase_handoff(
            run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd,
        )
        if isinstance(payload, dict):
            return _project_handoff_action(run_id, payload)

    return None


def _verbatim_available_actions(payload: dict[str, Any] | None) -> tuple[str, ...]:
    """Return ``available_actions`` verbatim from a handoff payload.

    The runtime-published list is the only sanctioned source of allowed
    handoff verbs; it is never re-derived or reinterpreted here. Returns
    ``()`` when the payload is absent or carries no list.
    """
    if not isinstance(payload, dict):
        return ()
    raw_actions = payload.get("available_actions")
    return tuple(raw_actions) if isinstance(raw_actions, (list, tuple)) else ()


def _project_handoff_action(
    run_id: str, payload: dict[str, Any],
) -> PendingOperatorAction:
    """Build the single-project ``meta.phase_handoff`` pending action.

    The handoff id comes from the payload's ``id`` field (the durable
    contract used by ``sdk.phase_handoff.phase_handoff_decide`` and
    ``sdk.actions``). ``available_actions`` is taken verbatim from the
    runtime-published list — the only sanctioned source of handoff verbs.
    """
    return PendingOperatorAction(
        run_id=run_id,
        kind="phase_handoff",
        handoff_kind=None,
        handoff_id=payload.get("id"),
        phase=payload.get("phase"),
        available_actions=_verbatim_available_actions(payload),
        raw=dict(payload),
    )


def _cross_handoff_action(
    run_id: str, checkpoint: dict[str, Any], active: dict[str, Any] | None,
) -> PendingOperatorAction:
    """Build a cross-run pending action from the checkpoint + active payload.

    ``phase_handoff_kind`` (checkpoint) is the dispatch authority and is
    carried verbatim into ``handoff_kind`` (no inference from the id
    prefix). ``available_actions`` and the handoff ``id`` come from the
    active ``meta.phase_handoff`` payload — the sanctioned source, which
    cross pauses persist in full alongside the checkpoint. The full
    checkpoint is preserved in ``raw`` as a forward-compatible escape
    hatch (``cfa_paused_state``, ``phase_handoff_child_id``, etc. are not
    dropped).
    """
    kind = checkpoint.get("phase_handoff_kind")
    handoff_kind = str(kind) if kind is not None else None
    handoff_id = (
        active.get("id") if isinstance(active, dict) else None
    ) or checkpoint.get("phase_handoff_id")
    project_alias = (
        checkpoint.get("phase_handoff_project_alias")
        if handoff_kind == "project"
        else None
    )
    phase = active.get("phase") if isinstance(active, dict) else None
    return PendingOperatorAction(
        run_id=run_id,
        kind="phase_handoff",
        handoff_kind=handoff_kind,
        handoff_id=handoff_id,
        phase=phase,
        project_alias=project_alias,
        available_actions=_verbatim_available_actions(active),
        raw=dict(checkpoint),
    )
