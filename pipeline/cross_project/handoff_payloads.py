"""Cross-project phase-handoff payload builders + pause persistence.

These helpers own the byte-shape of cross-level handoff payloads (ADR
0038, ADR 0039 cross parity, ADR cross-delivery+CFA-pause Phase A) so
MCP / SDK / UI consumers can route on the same fields they already use
for single-run validate_plan handoffs:

* :func:`build_cross_plan_handoff_payload` — cross_plan rejection pause
  (id prefix ``cross_plan:``, checkpoint ``phase_handoff_kind="plan"``).
* :func:`build_project_phase_handoff_payload` — proxy a child
  single-project handoff through the cross parent (id prefix
  ``project:<alias>:``, checkpoint ``phase_handoff_kind="project"``,
  child id preserved in ``artifacts.child_handoff_id``).
* :func:`build_cfa_handoff_payload` — cross_final_acceptance REJECTED
  pause (id prefix ``cfa:``, checkpoint ``phase_handoff_kind="cfa"``).
  Routed by the cross CLI prompt loop alongside ``cross_plan:`` (both
  are cross-owned, in-process decisions); ``project:`` remains off-band.
* :func:`apply_cross_phase_handoff_pause` — persist any of the three
  payload kinds: mutate the session, mark the cross checkpoint, emit
  the live event, flush ``meta.json`` and best-effort ``metrics.json``.

Handoff id grammars (the suffix is the authoritative round number
post-mortem tooling and resume-meta reload reads):

* ``cross_plan:<round_extras_key>:<round>``
* ``cfa:<round_extras_key>:<round>``
* ``project:<alias>:<child_handoff_id>``

:func:`parse_cross_handoff_round` is the safety-net parser that
recovers the round number when ``resumed_meta`` is not threaded through.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from core.observability.logging import warn
from pipeline.cross_project.checkpoint import write_cross_checkpoint
from pipeline.engine import save_session as save_cross_session

if TYPE_CHECKING:
    from pipeline.cross_project.final_acceptance import (
        CrossFinalAcceptanceResult,
    )

#: Cross-plan loop key for handoff IDs and round-extras lookup, mirroring
#: single-run ``plan_round`` (ADR 0038).
CROSS_PLAN_ROUND_KEY: str = "cross_plan_round"

#: CFA loop key — pair to :data:`CROSS_PLAN_ROUND_KEY` for the
#: cross_final_acceptance pause grammar. Handoff id format is
#: ``cfa:cross_final_acceptance:<round>``. Mirrors the cross_plan
#: shape so MCP / SDK consumers that already route on
#: ``round_extras_key`` for cross_plan handoffs work unchanged.
CFA_ROUND_KEY: str = "cross_final_acceptance"

#: Maximum bytes of last cross-plan output included in the handoff payload.
#: Mirrors the single-run handoff truncation discipline so the persisted
#: ``meta.phase_handoff.last_output`` stays bounded.
_CROSS_HANDOFF_LAST_OUTPUT_MAX: int = 16384


def parse_cross_handoff_round(handoff_id: str, default: int) -> int:
    """Best-effort parse of the round number from a cross handoff id.

    Cross handoff IDs follow the
    ``cross_plan:cross_plan_round:<N>`` grammar; the suffix is the
    authoritative source of the round number that the SDK halt path
    + the cross checkpoint round-trip preserve. Used as the fallback
    when ``resumed_meta`` is not threaded through to
    ``run_cross_pipeline`` (direct/internal callers); the production
    CLI always supplies ``resumed_meta`` via :func:`load_resume_meta`
    and the parser is the safety net.
    """
    try:
        return int(handoff_id.rsplit(":", 1)[-1])
    except (ValueError, AttributeError):
        return default


def build_cross_plan_handoff_payload(
    *,
    round_n: int,
    max_rounds: int,
    plan_review_dict: dict | None,
    plan_output: str,
    can_continue: bool = True,
) -> dict:
    """Build the persisted ``meta.phase_handoff`` payload for a cross_plan
    pause (ADR 0038).

    Shape is byte-identical to single-run (ADR 0031) so MCP / SDK / UI
    consumers that already read it for ``validate_plan`` handoffs work
    unchanged for cross_plan handoffs. The phase prefix
    (``"cross_plan"``) and the ``round_extras_key``
    (``"cross_plan_round"``) let post-mortem tooling route off either
    field.

    ADR 0054: ``can_continue`` is ``False`` when the paused round's plan
    did NOT parse schema-valid (synthetic reject) — there is no valid
    ``cross_plan.json`` for THIS round to dispatch, so ``continue`` is
    narrowed out (``[retry_feedback, halt]``). Continuing would otherwise
    silently dispatch an older round's plan than the one just rejected.
    """
    from pipeline.runtime.roles import PhaseHandoffAction, PhaseHandoffType

    review = plan_review_dict or {}
    verdict_label = review.get("verdict") or "REJECTED"
    # ``continue_with_waiver`` (ADR 0072) is deliberately NOT offered here:
    # it is a single-project-only action whose durable-waiver semantics
    # (operator verdict injected into downstream review gates) have no
    # cross_plan equivalent. cross_plan publishes only continue /
    # retry_feedback / halt; the cross resume dispatchers reject the fourth
    # value loudly. Granular cross-project waiver is out of scope per ADR 0072.
    actions = []
    if can_continue:
        actions.append(PhaseHandoffAction.CONTINUE.value)
    actions.append(PhaseHandoffAction.RETRY_FEEDBACK.value)
    actions.append(PhaseHandoffAction.HALT.value)
    return {
        "id":                f"cross_plan:{CROSS_PLAN_ROUND_KEY}:{round_n}",
        "phase":             "cross_plan",
        "type":              PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT.value,
        "trigger":           "rejected",
        "verdict":           verdict_label,
        "approved":          False,
        "round_extras_key":  CROSS_PLAN_ROUND_KEY,
        "round":             round_n,
        "loop_max_rounds":   max_rounds,
        "available_actions": actions,
        "artifacts":         {
            "short_summary": review.get("short_summary") or "",
            "findings":      list(review.get("findings") or []),
            "risks":         list(review.get("risks") or []),
            "checks":        list(review.get("checks") or []),
        },
        "last_output":       (plan_output or "")[:_CROSS_HANDOFF_LAST_OUTPUT_MAX],
    }


def build_cfa_handoff_payload(
    *,
    round_n: int,
    max_rounds: int,
    cfa_result: CrossFinalAcceptanceResult,
    raw_output: str | None = None,
) -> dict:
    """Build the persisted ``meta.phase_handoff`` payload for a
    cross_final_acceptance REJECTED pause (ADR cross-delivery+CFA-pause
    Phase A).

    Shape is byte-identical to the cross_plan / single-run handoff so
    MCP / SDK / UI consumers route on the existing fields. The phase
    tag is ``"cross_final_acceptance"`` and the
    ``round_extras_key`` is :data:`CFA_ROUND_KEY`; the id prefix
    (``cfa:``) is what the cross CLI dispatch uses to route the
    decision into the new CFA gate instead of the cross_plan
    planning_loop resume path.

    Available actions: **``["continue", "halt"]`` for ALL sources
    until A2c lands.** ``retry_feedback`` is intentionally NOT
    advertised: the gate cannot honor it yet (the feedback-aware
    reviewer re-invoke is A2c), so exposing it would dead-end the
    operator in a ``NotImplementedError``. ``continue`` is the
    operator override (accept the REJECTED bundle on purpose);
    ``halt`` writes terminal halted evidence. When A2c ships the
    feedback-aware reviewer, ``retry_feedback`` is re-added for the
    ``"agent"`` source (the only path where a reviewer actually ran).

    ``artifacts`` carries enough to render the pause without re-
    invoking the reviewer:

    * ``verdict`` — the parsed verdict string (``"REJECTED"`` on the
      pause path; on the rare ``"APPROVED"`` + ``ship_ready=False``
      shape, callers must NOT reach this builder).
    * ``short_summary`` — the model's one-line summary.
    * ``source`` — discriminator (``review`` / ``precondition`` /
      ``parse_error``).
    * ``release_blockers`` — list of blocker dicts from
      ``ParsedRelease.blockers_as_dicts()``. Identical shape to what
      ``session["phases"]["cross_final_acceptance"]`` carries on a
      non-pause REJECT, so consumers reading either field see the
      same data.
    * ``parse_error`` — present only when ``source == "parse_error"``;
      carries the parser's exception text for operator forensics.
    """
    from pipeline.runtime.roles import PhaseHandoffAction, PhaseHandoffType

    parsed = cfa_result.parsed
    source = cfa_result.source

    # Until A2c builds the feedback-aware reviewer, ``retry_feedback``
    # is NOT a user-reachable action for ANY source — the gate would
    # raise NotImplementedError on it. Narrow to continue/halt so the
    # prompt never advertises a dead-end button. (A2c re-adds
    # retry_feedback for source=="agent".)
    actions = [
        PhaseHandoffAction.CONTINUE.value,
        PhaseHandoffAction.HALT.value,
    ]

    artifacts: dict = {
        "verdict": parsed.verdict,
        "short_summary": parsed.short_summary or "",
        "source": source,
        "release_blockers": parsed.blockers_as_dicts(),
    }
    if source == "parse_error" and cfa_result.parse_error:
        artifacts["parse_error"] = cfa_result.parse_error

    last_output_text = raw_output if raw_output is not None else (
        cfa_result.raw_output or ""
    )

    return {
        "id":                f"cfa:{CFA_ROUND_KEY}:{round_n}",
        "phase":             "cross_final_acceptance",
        "type":              PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT.value,
        "trigger":           "rejected",
        "verdict":           parsed.verdict,
        "approved":          False,
        "round_extras_key":  CFA_ROUND_KEY,
        "round":             round_n,
        "loop_max_rounds":   max_rounds,
        "available_actions": actions,
        "artifacts":         artifacts,
        "last_output":       last_output_text[:_CROSS_HANDOFF_LAST_OUTPUT_MAX],
    }


def build_project_phase_handoff_payload(
    *, alias: str, child_payload: dict,
) -> dict:
    """Proxy a child single-project handoff through the cross parent.

    The parent payload reuses every field of ``child_payload`` (so MCP /
    SDK consumers see the same shape) and rewrites only the id to the
    ``project:<alias>:<child_id>`` namespace, stashing the original
    child id under ``artifacts.child_handoff_id`` so resume can route
    the decision back to the child run.
    """
    child_id = child_payload.get("id")
    if not isinstance(child_id, str) or not child_id:
        raise RuntimeError(
            f"Child project {alias!r} paused without a valid handoff id."
        )
    payload = dict(child_payload)
    payload["id"] = f"project:{alias}:{child_id}"
    artifacts = dict(payload.get("artifacts") or {})
    artifacts.setdefault("project_alias", alias)
    artifacts.setdefault("child_handoff_id", child_id)
    payload["artifacts"] = artifacts
    return payload


def apply_cross_phase_handoff_pause(
    *,
    run_dir: Path | None,
    session: dict,
    cross_ckpt: dict,
    payload: dict,
    cross_phase_usage: dict | None = None,
    terminal: bool = True,
) -> None:
    """Persist a cross-level phase-handoff pause.

    Used for all three shapes of cross-level pause:

    * **cross_plan rejection** (ADR 0038) — payload built by
      :func:`build_cross_plan_handoff_payload`. Callers must populate
      ``session["phases"]["cross_plan"]`` (round trace) BEFORE this
      call so the persisted ``meta.json`` and the in-memory session
      carry the same shape. Checkpoint ``phase_handoff_kind`` is
      ``"plan"`` (or omitted on legacy callers — the resume router
      treats absence as ``"plan"``).
    * **child project handoff proxy** (ADR 0039 cross parity) —
      payload built by :func:`build_project_phase_handoff_payload`.
      The child's ``session["phases"]["projects"][<alias>]`` already
      holds the inner trace by the time the parent proxies the pause.
      Caller must stamp ``phase_handoff_kind="project"`` +
      ``phase_handoff_project_alias`` + ``phase_handoff_child_id``
      on the checkpoint before calling this helper.
    * **cross_final_acceptance REJECTED** (ADR cross-delivery+CFA-pause
      Phase A) — payload built by :func:`build_cfa_handoff_payload`.
      Caller must populate ``session["phases"]
      ["cross_final_acceptance"]`` with the reviewer result + per-
      attempt history BEFORE this call. Checkpoint
      ``phase_handoff_kind`` is ``"cfa"``; the resume router (added
      in A2) dispatches off this field, NOT the id prefix, so
      legacy callers that did not stamp it would mis-route into the
      planning loop.

    Steps (identical for all shapes):

    - Sets ``session["status"]="awaiting_phase_handoff"`` and
      ``session["phase_handoff"]=payload``.
    - Marks the cross checkpoint with ``phase_handoff_pending`` and
      ``phase_handoff_id`` so resume can find the active artifact.
      Project-proxy callers additionally stamp
      ``phase_handoff_kind``, ``phase_handoff_project_alias``, and
      ``phase_handoff_child_id`` on the checkpoint before calling
      this helper.
    - Emits ``phase.handoff_requested`` so live observers (MCP watch,
      dashboard) pick up the pause without re-reading meta.
    - Persists ``meta.json`` and the cross checkpoint.
    - Best-effort ``metrics.json`` snapshot mirrors the single-run
      pause discipline (``_apply_phase_handoff_pause`` calls
      ``run._metrics.save`` for the same reason): a subsequent
      ``halt`` decision invokes the SDK halt path which writes
      ``evidence.json``, but the SDK has no access to the cross
      orchestrator's in-memory ``cross_phase_usage`` accumulator, so
      the post-halt run dir would otherwise carry an
      ``evidence.json`` next to a missing ``metrics.json``. Snapshot
      captures the cross_plan + cross_validate_plan token spend on
      the rejected plan up to the pause; for project-proxy pauses
      the same accumulator is captured up to the moment the child
      paused (sub-pipelines that finished earlier are already
      reflected; the paused child's per-alias rollup lands when the
      child resumes and finishes).

    No-ops on ``run_dir is None`` — in-memory dry runs still see the
    session mutation and the checkpoint markers without touching disk.
    """
    from core.observability import events as _events

    session["status"] = "awaiting_phase_handoff"
    session["phase_handoff"] = payload
    cross_ckpt["phase_handoff_pending"] = True
    cross_ckpt["phase_handoff_id"] = payload["id"]

    _events.emit(
        "phase.handoff_requested",
        phase=payload["phase"],
        handoff_type=payload["type"],
        trigger=payload["trigger"],
        round=payload["round"],
        handoff_id=payload["id"],
    )
    # ADR 0047 Phase E — the pause-banner ``warn(...)`` is a terminal
    # courtesy line for the operator; the structural signal (event +
    # session status + checkpoint) above is what UI / MCP / cross-
    # project consume. Under ``terminal=False`` (caller passes
    # ``ctx.terminal`` or its cross-level equivalent) we suppress.
    if terminal:
        warn(
            f"Cross phase handoff requested for {payload['phase']!r} (round "
            f"{payload['round']}/{payload['loop_max_rounds']}): "
            f"trigger={payload['trigger']!r}. Pausing for operator decision."
        )
    if run_dir is None:
        return
    save_cross_session(run_dir, session)
    write_cross_checkpoint(run_dir, cross_ckpt)
    # Best-effort metrics.json snapshot — see the docstring above for
    # why this lands alongside the meta/checkpoint write. Failure to
    # snapshot is non-fatal: the pause invariant
    # (``meta.status=awaiting_phase_handoff`` + ``phase_handoff``
    # payload + checkpoint) is already on disk, and downstream
    # readers tolerate a missing ``metrics.json`` the same way they
    # tolerate it on early-aborted runs.
    if cross_phase_usage:
        try:
            from core.observability.metrics import cross_metrics_dict
            cross_metrics = cross_metrics_dict({}, cross_phase_usage)
            (run_dir / "metrics.json").write_text(
                json.dumps(cross_metrics, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass
