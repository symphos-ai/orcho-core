"""Read-only classification + invariants for cross-run snapshots (Stage 7).

The single-project consistency checker (:mod:`pipeline.run_state.consistency`)
compares the event-derived projection against ``meta.json`` for one run. A
cross run adds a second durable surface — ``cross_checkpoint.json`` — plus a
fan-out of child runs under ``run_dir/<alias>/``. This module is the
cross-level analogue: it folds those durable artifacts — ``meta.json``,
``cross_checkpoint.json``, the ``phase_handoff_decisions/`` artifacts, and the
child ``meta.json`` rows — into one structured snapshot and diagnoses the
cross-specific invariants between them.

It is strictly **read-only**: it never starts a provider / subprocess / child
run, never writes, and never repairs. ``repair_hint`` text describes what a
future repair *would* do; this module does none of it.

Package discipline (the load-bearing constraint): this module lives in
``pipeline.run_state`` and must NOT import ``pipeline.cross_project`` — not
even the checkpoint reader. The cross checkpoint is read here by path
(``run_dir/cross_checkpoint.json``) and tolerantly (missing / corrupt file →
empty default), exactly mirroring :func:`pipeline.run_state.consistency._read_meta`.
That keeps ``run_state`` a leaf that depends at most on its own modules.

``phase_handoff_kind`` is treated as the dispatch authority everywhere here.
The id prefix (``cross_plan:`` / ``project:`` / ``cfa:``) is informational —
the kind/id agreement check (:func:`validate_cross_run_state` invariant 4)
flags a *mismatch* but the snapshot never *infers* kind from the prefix.

Severity convention (mirrors :mod:`pipeline.run_state.consistency`):

- ``"error"`` — a contradiction that breaks resume reasoning (a checkpoint
  marked ``phase_handoff_pending`` with no active payload to resolve).
- ``"warning"`` — a recoverable desync a repair could heal (a terminal run
  carrying a stale handoff, kind/id disagreement, incomplete project marker).
- ``"info"`` — reserved; no current cross invariant is purely informational.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline.run_state.cross_parent import CrossParentState, reduce_cross_parent_state
from pipeline.run_state.cross_parent_disk import load_cross_parent_facts
from pipeline.run_state.status_vocab import TERMINAL_CROSS_STATUSES
from pipeline.run_state.types import RunStateIssue

_CROSS_CHECKPOINT_FILE = "cross_checkpoint.json"
_DECISIONS_DIRNAME = "phase_handoff_decisions"

# Informational id-prefix per dispatch kind. Used ONLY to flag a kind/id
# disagreement; resume dispatch routes on the kind, never on the prefix.
_KIND_ID_PREFIX = {
    "plan": "cross_plan:",
    "project": "project:",
    "cfa": "cfa:",
}


@dataclass(frozen=True, slots=True)
class CrossRunStateSnapshot:
    """Immutable read-only fold of a cross run's durable artifacts.

    Built by :func:`classify_cross_run_state` from ``meta.json``,
    ``cross_checkpoint.json``, and the child ``meta.json`` rows. Carries no
    behaviour and starts nothing — purely the projected state the invariant
    checks read.

    Fields:

    - ``meta_status`` — top-level ``meta.json`` ``status`` (``None`` when
      absent / non-string).
    - ``active_handoff`` — the active ``meta.phase_handoff`` payload dict, or
      ``None`` when no active handoff is recorded.
    - ``active_handoff_id`` — the ``id`` of ``active_handoff`` when present.
    - ``checkpoint_pending`` — checkpoint ``phase_handoff_pending`` flag.
    - ``checkpoint_kind`` — checkpoint ``phase_handoff_kind`` (dispatch
      authority).
    - ``checkpoint_id`` — checkpoint ``phase_handoff_id`` (informational id).
    - ``checkpoint_project_alias`` — checkpoint ``phase_handoff_project_alias``
      (set when kind == ``"project"``).
    - ``checkpoint_child_id`` — checkpoint ``phase_handoff_child_id`` (set when
      kind == ``"project"``).
    - ``cfa_paused_state`` — checkpoint ``cfa_paused_state`` (set when
      kind == ``"cfa"`` and pending).
    - ``pending_gate`` — checkpoint ``pending_gate`` payload, or ``None``.
    - ``sub_status`` — checkpoint ``sub_status`` map (alias → status string).
    - ``child_statuses`` — per-alias child-run ``meta.status`` read from
      ``run_dir/<alias>/meta.json`` for each alias in ``sub_status`` whose
      child meta exists (``None`` value when the child meta has no status).
    - ``decisions`` — recorded ``phase_handoff_decisions/*.json`` artifacts as
      ``{"action", "handoff_id"}`` rows (tolerant: a missing directory / bad
      file contributes nothing). Lets a reader tell a *pending* handoff with
      no decision from one that already has a recorded operator decision.
    """

    meta_status: str | None = None
    active_handoff: dict[str, Any] | None = None
    active_handoff_id: str | None = None
    checkpoint_pending: bool = False
    checkpoint_kind: str | None = None
    checkpoint_id: str | None = None
    checkpoint_project_alias: str | None = None
    checkpoint_child_id: str | None = None
    cfa_paused_state: dict[str, Any] | None = None
    pending_gate: dict[str, Any] | None = None
    sub_status: dict[str, Any] = field(default_factory=dict)
    child_statuses: dict[str, str | None] = field(default_factory=dict)
    decisions: tuple[dict[str, Any], ...] = ()
    #: Canonical reduction of exact declared durable facts.  Legacy fields above
    #: remain a repair-facing compatibility projection only.
    canonical_state: CrossParentState | None = None


def classify_cross_run_state(run_dir: Path | str) -> CrossRunStateSnapshot:
    """Fold a cross run's durable artifacts into a read-only snapshot.

    Reads ``meta.json``, ``cross_checkpoint.json``, the
    ``phase_handoff_decisions/`` artifacts, and the child ``meta.json`` rows
    tolerantly (missing / corrupt → empty default). Starts no provider,
    subprocess, or child run. Returns a :class:`CrossRunStateSnapshot`.
    """
    path = Path(run_dir)
    meta = _read_json_dict(path / "meta.json")
    checkpoint = _read_json_dict(path / _CROSS_CHECKPOINT_FILE)

    meta_status = meta.get("status")
    active_handoff = meta.get("phase_handoff")
    active_handoff = active_handoff if isinstance(active_handoff, dict) else None
    active_handoff_id = active_handoff.get("id") if isinstance(active_handoff, dict) else None

    cfa_paused_state = checkpoint.get("cfa_paused_state")
    pending_gate = checkpoint.get("pending_gate")
    sub_status = checkpoint.get("sub_status")
    sub_status = sub_status if isinstance(sub_status, dict) else {}

    canonical_state = reduce_cross_parent_state(load_cross_parent_facts(path))
    return CrossRunStateSnapshot(
        meta_status=meta_status if isinstance(meta_status, str) else None,
        active_handoff=active_handoff,
        active_handoff_id=(active_handoff_id if isinstance(active_handoff_id, str) else None),
        checkpoint_pending=bool(checkpoint.get("phase_handoff_pending")),
        checkpoint_kind=_opt_str(checkpoint.get("phase_handoff_kind")),
        checkpoint_id=_opt_str(checkpoint.get("phase_handoff_id")),
        checkpoint_project_alias=_opt_str(checkpoint.get("phase_handoff_project_alias")),
        checkpoint_child_id=_opt_str(checkpoint.get("phase_handoff_child_id")),
        cfa_paused_state=(cfa_paused_state if isinstance(cfa_paused_state, dict) else None),
        pending_gate=pending_gate if isinstance(pending_gate, dict) else None,
        sub_status=dict(sub_status),
        child_statuses=_read_child_statuses(path, sub_status),
        decisions=_read_decisions(path),
        canonical_state=canonical_state,
    )


def validate_cross_run_state(run_dir: Path | str) -> tuple[RunStateIssue, ...]:
    """Diagnose cross-run snapshot inconsistencies (read-only).

    Classifies ``run_dir`` via :func:`classify_cross_run_state` and returns a
    tuple of :class:`RunStateIssue` for the cross-specific invariants. Never
    writes, repairs, or starts anything. Severity follows the module
    convention (``error`` breaks resume reasoning; ``warning`` is a
    recoverable desync).
    """
    snap = classify_cross_run_state(run_dir)
    issues: list[RunStateIssue] = []

    # The canonical reducer owns contradiction precedence.  Keep legacy issue
    # text below for repair compatibility, but surface each canonical violation
    # first as a stable read-only diagnostic.
    if snap.canonical_state is not None and snap.canonical_state.children:
        for violation in snap.canonical_state.violations:
            suffix = f" for child {violation.alias!r}" if violation.alias else ""
            issues.append(
                RunStateIssue(
                    code=f"cross_parent_{violation.code}",
                    severity="error",
                    message=f"canonical cross-parent state violation {violation.code}{suffix}",
                    repair_hint="reconcile the durable parent, child, and checkpoint facts before resume",
                )
            )

    # 1. Terminal cross run carrying a stale active handoff — INCLUDING
    #    ``failed``. A cross pause short-circuits the run all the way to a
    #    final terminal, so any payload left at a cross terminal is stale.
    if snap.active_handoff is not None and snap.meta_status in TERMINAL_CROSS_STATUSES:
        issues.append(
            RunStateIssue(
                code="cross_terminal_with_stale_handoff",
                severity="warning",
                message=(
                    f"meta.status is {snap.meta_status!r} (terminal) but "
                    "meta.phase_handoff still carries an active handoff "
                    f"({snap.active_handoff_id!r})"
                ),
                repair_hint=(
                    "clear the stale meta.phase_handoff payload via the "
                    "cross-safe terminal helper; a terminal cross run has no "
                    "active handoff to decide"
                ),
            )
        )

    # 2. Checkpoint claims a pending handoff but no active payload exists to
    #    resolve — resume would surface a pending action with no actions.
    if snap.checkpoint_pending and snap.active_handoff is None:
        issues.append(
            RunStateIssue(
                code="checkpoint_pending_without_active_handoff",
                severity="error",
                message=(
                    "cross_checkpoint.phase_handoff_pending is set but "
                    "meta.phase_handoff carries no active payload to resolve"
                ),
                repair_hint=(
                    "either re-persist the active handoff payload or clear "
                    "the pending checkpoint markers so resume has no torn "
                    "pending action"
                ),
            )
        )

    # 3. An active handoff payload exists but the checkpoint never marked it
    #    pending — a recoverable desync (resume keys on the checkpoint).
    if snap.active_handoff is not None and not snap.checkpoint_pending:
        issues.append(
            RunStateIssue(
                code="active_handoff_without_checkpoint_pending",
                severity="warning",
                message=(
                    "meta.phase_handoff carries an active handoff "
                    f"({snap.active_handoff_id!r}) but "
                    "cross_checkpoint.phase_handoff_pending is not set"
                ),
                repair_hint=(
                    "reconcile the checkpoint: set phase_handoff_pending (with "
                    "kind/id) or clear the stale meta.phase_handoff payload"
                ),
            )
        )

    # 4. kind/id disagreement. The kind is authoritative; the id prefix is
    #    informational, but a mismatch signals a torn or hand-edited
    #    checkpoint. Never used to infer kind — only to flag disagreement.
    expected_prefix = _KIND_ID_PREFIX.get(snap.checkpoint_kind or "")
    if (
        expected_prefix is not None
        and snap.checkpoint_id is not None
        and not snap.checkpoint_id.startswith(expected_prefix)
    ):
        issues.append(
            RunStateIssue(
                code="checkpoint_kind_id_mismatch",
                severity="warning",
                message=(
                    f"phase_handoff_kind is {snap.checkpoint_kind!r} (expects "
                    f"id prefix {expected_prefix!r}) but phase_handoff_id is "
                    f"{snap.checkpoint_id!r}"
                ),
                repair_hint=(
                    "the kind is the dispatch authority; correct the id so it "
                    "agrees with the kind (resume never routes on the prefix)"
                ),
            )
        )

    # 5. kind == 'project' requires a complete project marker: the alias must
    #    appear in sub_status and the child id must be non-empty.
    if snap.checkpoint_kind == "project":
        alias = snap.checkpoint_project_alias
        if alias is None or alias not in snap.sub_status or not snap.checkpoint_child_id:
            issues.append(
                RunStateIssue(
                    code="project_handoff_marker_incomplete",
                    severity="warning",
                    message=(
                        "phase_handoff_kind is 'project' but the project "
                        "marker is incomplete: "
                        f"alias={alias!r} (in sub_status: "
                        f"{alias in snap.sub_status if alias else False}), "
                        f"child_id={snap.checkpoint_child_id!r}"
                    ),
                    repair_hint=(
                        "a project handoff must name an alias present in "
                        "sub_status and a non-empty child id; reconcile the "
                        "checkpoint markers"
                    ),
                )
            )

    # 6. kind == 'cfa' and pending requires the persisted CFA paused state so
    #    resume can re-enter the CFA gate.
    if snap.checkpoint_kind == "cfa" and snap.checkpoint_pending and snap.cfa_paused_state is None:
        issues.append(
            RunStateIssue(
                code="cfa_pending_without_paused_state",
                severity="warning",
                message=(
                    "phase_handoff_kind is 'cfa' and pending but "
                    "cross_checkpoint.cfa_paused_state is absent"
                ),
                repair_hint=(
                    "a CFA pause must persist cfa_paused_state (verdict, "
                    "findings_count, summary, source) for resume to re-enter "
                    "the CFA gate"
                ),
            )
        )

    # 7. A pending gate and a pending phase handoff cannot both be active —
    #    resume resolves exactly one pending operator action.
    if isinstance(snap.pending_gate, dict) and snap.checkpoint_pending:
        issues.append(
            RunStateIssue(
                code="pending_gate_and_handoff_active",
                severity="warning",
                message=(
                    "cross_checkpoint carries both a pending_gate and a "
                    "pending phase handoff; resume resolves only one pending "
                    "action"
                ),
                repair_hint=(
                    "clear whichever pending marker no longer applies so a "
                    "single pending operator action remains"
                ),
            )
        )

    return tuple(issues)


def _read_child_statuses(
    run_dir: Path,
    sub_status: dict[str, Any],
) -> dict[str, str | None]:
    """Read child-run ``meta.status`` for each alias under ``run_dir/<alias>``.

    Only aliases whose child ``meta.json`` exists contribute a row; a missing
    child meta is simply omitted (the cross run may have failed before
    dispatching that child). Value is the child status or ``None`` when the
    child meta carries no status.
    """
    out: dict[str, str | None] = {}
    for alias in sub_status:
        if not isinstance(alias, str):
            continue
        child_meta_file = run_dir / alias / "meta.json"
        if not child_meta_file.is_file():
            continue
        child_meta = _read_json_dict(child_meta_file)
        status = child_meta.get("status")
        out[alias] = status if isinstance(status, str) else None
    return out


def _read_decisions(run_dir: Path) -> tuple[dict[str, Any], ...]:
    """Read ``phase_handoff_decisions/*.json`` tolerantly (mirrors consistency).

    Returns one ``{"action", "handoff_id"}`` row per readable artifact, in
    sorted filename order. A missing directory or an unreadable / malformed /
    non-object file is skipped — a single bad artifact never breaks the scan.
    """
    decisions_dir = run_dir / _DECISIONS_DIRNAME
    if not decisions_dir.is_dir():
        return ()
    out: list[dict[str, Any]] = []
    for entry in sorted(decisions_dir.iterdir()):
        if not (entry.is_file() and entry.suffix == ".json"):
            continue
        try:
            raw = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        out.append({"action": raw.get("action"), "handoff_id": raw.get("handoff_id")})
    return tuple(out)


def _read_json_dict(path: Path) -> dict[str, Any]:
    """Read a JSON object tolerantly; return ``{}`` when absent / malformed."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _opt_str(value: Any) -> str | None:
    """Return ``value`` when it is a non-empty ``str``, else ``None``."""
    return value if isinstance(value, str) and value else None


__all__ = [
    "CrossRunStateSnapshot",
    "classify_cross_run_state",
    "validate_cross_run_state",
]
