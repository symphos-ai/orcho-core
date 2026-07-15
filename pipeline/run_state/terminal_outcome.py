# SPDX-License-Identifier: Apache-2.0
"""Single terminal-outcome reducer for a project run (ADR 0115 slice 3b-1).

`finalize_project_run` settles a run to its terminal shape in two load-bearing
moments: a **pre-delivery** status decision (from ``state.halt`` + the work-kind
profile) and a **post-delivery** no-diff final-acceptance reconcile (after the
diff has been captured and delivery has run). Before this module each moment
open-coded its own ``session["status"] = …`` writes, so the status-flip and the
verdict read lived in `finalization.py` next to the orchestration body.

This module is the one place that turns run/session facts into a terminal patch.
It takes plain facts as arguments and emits the terminal status **exclusively**
through the :mod:`pipeline.run_state.terminal` writer primitives
(:func:`mark_run_done` / :func:`mark_run_halted` /
:func:`mark_run_awaiting_review`) — it never writes ``status`` directly for any
branch. Any release verdict is read only through the
:mod:`pipeline.run_state.release_verdict` derived detectors below, so no
open-coded ``== "APPROVED"`` survives. The reducer owns its own *display*
markers (the nested ``halt`` compat block, ``no_op_outcome``,
``no_change_outcome``, ``rejected_outcome``, ``delivery_override``) and writes
those dict shapes directly — only the ``status`` / ``halt_reason`` fields ever
travel through ``terminal.py``.

Pure in-place mutation: like the rest of ``run_state`` this module does no file
IO, emits no events, and touches no checkpoint — the caller owns persistence and
the single ``run.end`` boundary. It depends only on its sibling ``run_state``
modules (``terminal`` + ``release_verdict``), never on a runtime / resume path.
"""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.run_state.release_verdict import (
    is_approved,
    is_rejected,
    is_release_blocked,
)
from pipeline.run_state.terminal import (
    evict_transient_settle_keys,
    mark_run_awaiting_review,
    mark_run_done,
    mark_run_halted,
)

#: Work-kind profiles whose terminal is a plan-only artifact for human review
#: rather than a delivered diff. Their clean tail pauses at
#: ``awaiting_human_review`` (unless the run already resumed through an approved
#: phase-handoff decision, in which case it settles straight to ``done``).
_PLAN_ONLY_PROFILES: frozenset[str] = frozenset({"planning", "research"})


def resolve_terminal_outcome(
    session: dict[str, Any],
    *,
    state_halt: bool,
    halt_reason: str,
    current_phase: str,
    profile_name: str,
    phase_handoff_override: bool,
) -> None:
    """Settle the **pre-delivery** terminal status onto ``session``.

    Three mutually-exclusive branches, each routing the status write through a
    ``terminal.py`` primitive:

    * ``state_halt`` → :func:`mark_run_halted` with the run's ``halt_reason``,
      plus the nested ``halt`` compat block (``{reason, phase}``) that
      downstream consumers read via ``halt.phase``. The top-level
      ``halt_reason`` mirrors the SDK halt path so every ``state.halt``-driven
      termination records a non-null reason instead of hiding it under
      ``halt.reason``.
    * a plan-only profile (``planning`` / ``research``) with no
      ``phase_handoff_override`` → :func:`mark_run_awaiting_review`: the
      plan-only work kind pauses for human review of its artifact. When the run
      already resumed through an approved phase-handoff decision the override is
      set, so it falls through to ``done`` instead.
    * otherwise → :func:`mark_run_done`.

    A subsequent ``_run_commit_delivery`` / :func:`apply_no_diff_terminal` may
    flip the status again; this only writes the pre-delivery shape.
    """
    if state_halt:
        mark_run_halted(session, halt_reason=halt_reason)
        # The nested ``halt`` block stays for backwards compatibility with
        # consumers that already read ``halt.phase``; it is phase-specific
        # compat state the generic helper does not own.
        session["halt"] = {"reason": halt_reason, "phase": current_phase}
    elif profile_name in _PLAN_ONLY_PROFILES and not phase_handoff_override:
        mark_run_awaiting_review(session)
    else:
        mark_run_done(session)


def apply_no_diff_terminal(
    session: dict[str, Any], *, diff_path: Path | None,
) -> None:
    """Reconcile the **post-delivery** no-diff final-acceptance outcome.

    A verify-only run whose review skipped (no uncommitted target) reaches
    finalization with ``status='done'`` and no captured diff. Only then does
    this reducer translate the recorded final-acceptance verdict into a terminal
    outcome:

    * rejected-without-diff → :func:`mark_run_halted` with
      ``halt_reason='final_acceptance_no_diff'`` plus the ``no_op_outcome``
      display marker (treating a green DONE for a refused run with nothing to
      ship as misleading).
    * approved-without-diff → the ``no_change_outcome`` display marker; status
      stays ``done`` (an honest verification-only pass that produced no diff).

    No-op when a diff was captured (``diff_path is not None``) or the
    pre-delivery / delivery status is not ``done`` — those runs already carry a
    real terminal the reducer must not overwrite.
    """
    if diff_path is not None:
        return
    if session.get("status") != "done":
        return

    base_outcome = {
        "phase": "final_acceptance",
        "review_target": "uncommitted",
        "diff": "none",
    }

    if _final_acceptance_rejected_without_diff(session):
        mark_run_halted(session, halt_reason="final_acceptance_no_diff")
        session["no_op_outcome"] = {
            **base_outcome,
            "reason": "final_acceptance_no_diff",
            "status": "halted",
            "message": (
                "Final acceptance rejected the run, but review_changes skipped "
                "because there was no uncommitted diff to review or deliver."
            ),
        }
        return

    if _final_acceptance_approved_without_diff(session):
        session["no_change_outcome"] = {
            **base_outcome,
            "reason": "verification_no_changes",
            "status": "done",
            "message": (
                "Final acceptance approved a verification-only run that "
                "produced no file changes to review or deliver."
            ),
        }


# ── no-diff verdict detectors (read the single release-verdict source) ──────
#
# The reducer is the single home for the no-diff outcome decision, so the
# detectors that classify it live here next to it. They read the recorded
# final-acceptance verdict only through ``release_verdict.is_rejected`` /
# ``is_approved`` (ADR 0115 slice 2/3a) — no open-coded ``== "APPROVED"``.


def _has_no_diff_final_acceptance_target(
    phases: Mapping[str, Any], final_acceptance: Mapping[str, Any],
) -> bool:
    review = phases.get("review_changes")
    if (
        isinstance(review, Mapping)
        and review.get("skipped") == "no uncommitted changes"
    ):
        return True

    return (
        final_acceptance.get("diff") == "none"
        and final_acceptance.get("review_target")
        in {"not_applicable", "uncommitted"}
    )


def _final_acceptance_rejected_without_diff(session: Mapping[str, Any]) -> bool:
    """Return True for the verify-only no-diff release-gate shape.

    A well-formed rejected final-acceptance verdict is normally non-terminal:
    delivery/correction handling can still let the operator decide what to do
    with a real diff. When review skipped because there was no uncommitted
    target and final acceptance also rejects due to that absence, there is no
    diff for delivery or repair to operate on. Treating that as green DONE is
    misleading, so finalization turns it into an explicit no-op halt.
    """
    phases = session.get("phases")
    if not isinstance(phases, Mapping):
        return False

    final_acceptance = phases.get("final_acceptance")
    if not isinstance(final_acceptance, Mapping):
        return False
    if not _has_no_diff_final_acceptance_target(phases, final_acceptance):
        return False

    verdict = final_acceptance.get("verdict")
    rejected = is_rejected(verdict)
    not_ship_ready = final_acceptance.get("ship_ready") is False
    not_approved = final_acceptance.get("approved") is False
    return rejected or not_ship_ready or not_approved


def _final_acceptance_approved_without_diff(session: Mapping[str, Any]) -> bool:
    phases = session.get("phases")
    if not isinstance(phases, Mapping):
        return False

    final_acceptance = phases.get("final_acceptance")
    if not isinstance(final_acceptance, Mapping):
        return False
    if not _has_no_diff_final_acceptance_target(phases, final_acceptance):
        return False

    verdict = final_acceptance.get("verdict")
    approved_verdict = is_approved(verdict)
    return approved_verdict or final_acceptance.get("ship_ready") is True


# ── rejected / override / approved-supersede terminal reducer ───────────────
#
# The post-delivery reconcile of a still-``done`` session against its
# authoritative final-acceptance verdict. The finalization seam
# (``_apply_rejected_release_terminal_outcome``) reads the facts (rejected,
# delivery status, verdict / blockers / summary) and delegates the decision
# here; this reducer owns the flip done↔halted and the display markers.

#: Delivery executor outcomes that mean the diff was actually shipped to the
#: project checkout (committed or applied uncommitted). A rejected release that
#: nonetheless reaches one of these is an operator override, not a clean pass.
#: This set is the reducer's own decision input, not a finalization fact.
_DELIVERY_APPLIED_STATUSES: frozenset[str] = frozenset(
    {"committed", "applied_uncommitted"},
)

#: The three structural fields a verification gap / engine receipt backstop gap
#: shares (mirrors ``correction_fixed_point._gap_key``). ``required_check`` is
#: the actionable receipt / command identity; ``risk`` is the prose fallback.
_GAP_FIELDS: tuple[str, ...] = ("risk", "missing_evidence", "required_check")


@dataclass(frozen=True)
class EngineBackstopReason:
    """The engine-authoritative rejection cause read off ``final_acceptance``.

    A REJECTED terminal forced by the engine receipt backstop (or carrying
    reviewer-flagged verification gaps) must surface that deterministic cause,
    not just the agent's positive ``short_summary`` + empty ``release_blockers``.
    The finalization seam reads two fields off the SAME persisted
    ``final_acceptance`` record the handler wrote — the deterministic
    ``engine_backstop`` (``{reason, gaps}``) and the reviewer ``verification_gaps``
    (``[{risk, missing_evidence, required_check}]``) — normalizes them here with
    no re-classification, and hands the result to the reducer.

    ``present`` is False for the pure agent-blocker REJECT shape (no backstop, no
    gaps); the reducer then writes byte-identical markers to the pre-engine form.
    """

    backstop_reason: str
    backstop_gaps: tuple[dict[str, Any], ...]
    verification_gaps: tuple[dict[str, Any], ...]

    @property
    def present(self) -> bool:
        """True when any engine-authoritative cause was recorded."""
        return bool(
            self.backstop_reason or self.backstop_gaps or self.verification_gaps
        )


def _normalize_gap(gap: Any) -> dict[str, Any] | None:
    """Project a gap mapping onto the stable ``{risk, …}`` fields it carries.

    Robust to missing / non-mapping inputs (returns ``None``) and never
    re-classifies: only the present :data:`_GAP_FIELDS` are copied through.
    """
    if not isinstance(gap, Mapping):
        return None
    norm = {field: gap[field] for field in _GAP_FIELDS if field in gap}
    return norm or None


def _normalize_gap_list(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    out: list[dict[str, Any]] = []
    for gap in value:
        norm = _normalize_gap(gap)
        if norm is not None:
            out.append(norm)
    return tuple(out)


def normalize_engine_reason(
    *,
    verification_gaps: Any = None,
    engine_backstop: Any = None,
) -> EngineBackstopReason:
    """Normalize the engine-backstop facts off a ``final_acceptance`` record.

    Pure and total: any missing / non-mapping / unexpected shape collapses to an
    empty field (never raised on), so the pure agent-blocker REJECT record yields
    a non-``present`` reason and the reducer stays byte-identical. The
    ``engine_backstop`` mapping contributes its ``reason`` string and normalized
    ``gaps``; ``verification_gaps`` contributes its own normalized gap list.
    """
    backstop_reason = ""
    backstop_gaps: tuple[dict[str, Any], ...] = ()
    if isinstance(engine_backstop, Mapping):
        reason = engine_backstop.get("reason")
        if isinstance(reason, str):
            backstop_reason = reason
        backstop_gaps = _normalize_gap_list(engine_backstop.get("gaps"))
    return EngineBackstopReason(
        backstop_reason=backstop_reason,
        backstop_gaps=backstop_gaps,
        verification_gaps=_normalize_gap_list(verification_gaps),
    )


def _apply_engine_reason_to_marker(
    marker: dict[str, Any],
    engine_reason: EngineBackstopReason | None,
    *,
    base_message: str,
) -> str:
    """Stamp the engine-cause fields onto a rejected/override marker.

    When ``engine_reason`` is absent or not :attr:`~EngineBackstopReason.present`
    this is a no-op that returns ``base_message`` unchanged and adds NO keys, so
    the pure agent-blocker REJECT and the operator-override-without-backstop
    markers stay byte-identical to the pre-engine form. When present it records an
    ``engine_backstop`` (``{reason, gaps}``) and/or ``verification_gaps`` field
    naming the cause, and returns a message led by the engine cause as headline.
    """
    if engine_reason is None or not engine_reason.present:
        return base_message

    headline = ""
    if engine_reason.backstop_reason or engine_reason.backstop_gaps:
        marker["engine_backstop"] = {
            "reason": engine_reason.backstop_reason,
            "gaps": [dict(gap) for gap in engine_reason.backstop_gaps],
        }
        if engine_reason.backstop_reason:
            headline = (
                "Engine backstop rejected the release: "
                f"{engine_reason.backstop_reason}."
            )
    if engine_reason.verification_gaps:
        marker["verification_gaps"] = [
            dict(gap) for gap in engine_reason.verification_gaps
        ]
        if not headline:
            headline = "Final acceptance flagged unresolved verification gaps."

    return f"{headline} {base_message}".strip() if headline else base_message


def _attach_short_summary(
    marker: dict[str, Any], short_summary: Any, *, superseded: bool,
) -> None:
    """Attach the agent ``short_summary`` to a marker, flagged if superseded.

    With no engine cause the summary is recorded verbatim (parity). When an
    engine cause is present the positive agent summary is no longer the headline,
    so it is explicitly tagged ``(superseded agent view)`` — the engine verdict
    already leads the ``message``.
    """
    if not (isinstance(short_summary, str) and short_summary):
        return
    if superseded:
        marker["short_summary"] = f"(superseded agent view) {short_summary}"
    else:
        marker["short_summary"] = short_summary


def resolve_rejected_release_terminal(
    session: dict[str, Any],
    *,
    rejected: bool,
    delivery_status: str,
    verdict: str,
    blockers: list[Any],
    short_summary: Any,
    engine_reason: EngineBackstopReason | None = None,
) -> None:
    """Reconcile a still-``done`` terminal to the authoritative release verdict.

    Three mutually-exclusive branches against the facts the finalization seam
    extracted from the persisted ``final_acceptance`` record:

    * NOT rejected (an approved authoritative verdict) →
      :func:`supersede_same_run_residue`: a successful repeat / resumed final
      acceptance evicts any terminal-rejection residue AND the phantom rejected
      ``commit_delivery`` gate a prior REJECTED attempt of the same run left
      behind (ADR 0109 bidirectional refinement).
    * rejected AND ``delivery_status`` in :data:`_DELIVERY_APPLIED_STATUSES`
      (operator override — ``committed`` / ``applied_uncommitted``) → keep
      ``done`` but record a durable ``delivery_override`` marker, so the outcome
      is observably distinct from a clean success.
    * rejected AND delivery NOT applied → flip the stale ``done`` to ``halted``
      via :func:`mark_run_halted` (``halt_reason='final_acceptance_rejected'``)
      and record a structured ``rejected_outcome`` marker carrying the visible
      verdict / blockers / short summary, so a rejected run never reads as a
      silent successful ``done``.

    When the finalization seam supplies an ``engine_reason`` that is
    :attr:`~EngineBackstopReason.present` (the engine receipt backstop forced the
    REJECT, or reviewer ``verification_gaps`` are unresolved), both rejected
    branches additionally stamp the authoritative engine cause onto their marker
    (an ``engine_backstop`` / ``verification_gaps`` field) and lead the marker
    ``message`` with that cause — so a positive agent ``short_summary`` over an
    empty ``release_blockers`` list can never read as the headline (it is tagged
    ``(superseded agent view)`` with the engine verdict above it). An absent /
    non-present ``engine_reason`` (the pure agent-blocker REJECT, or an
    operator-override with no backstop) leaves both markers byte-identical to the
    pre-engine form — no new keys.

    The flip done↔halted lives ONLY here: ``status`` is written exclusively
    through ``mark_run_halted``; the ``delivery_override`` / ``rejected_outcome``
    dicts are display markers (modeled on ``no_op_outcome``) whose own
    ``"status"`` field is descriptive and does not touch ``session['status']``.

    The rejected branch is ADR 0106; the approved-supersede branch is ADR 0109.
    """
    if not rejected:
        supersede_same_run_residue(session)
        return

    engine_present = engine_reason is not None and engine_reason.present

    if delivery_status in _DELIVERY_APPLIED_STATUSES:
        override: dict[str, Any] = {
            "phase": "final_acceptance",
            "reason": "final_acceptance_rejected_override",
            "status": "done",
            "release_verdict": verdict,
            "release_blockers": blockers,
            "delivery_status": delivery_status,
        }
        override["message"] = _apply_engine_reason_to_marker(
            override,
            engine_reason,
            base_message=(
                "Operator override: delivery was applied despite a rejected "
                "final acceptance. This run reached 'done' by override, not "
                "clean success."
            ),
        )
        _attach_short_summary(override, short_summary, superseded=engine_present)
        session["delivery_override"] = override
        return

    mark_run_halted(session, halt_reason="final_acceptance_rejected")
    rejected_marker: dict[str, Any] = {
        "phase": "final_acceptance",
        "reason": "final_acceptance_rejected",
        "status": "halted",
        "release_verdict": verdict,
        "release_blockers": blockers,
    }
    rejected_marker["message"] = _apply_engine_reason_to_marker(
        rejected_marker,
        engine_reason,
        base_message=(
            "Final acceptance rejected the release and delivery was not "
            "applied, so the run halted instead of finishing as done."
        ),
    )
    _attach_short_summary(rejected_marker, short_summary, superseded=engine_present)
    session["rejected_outcome"] = rejected_marker


def supersede_same_run_residue(session: dict[str, Any]) -> None:
    """Evict last-attempt rejection residue on a clean approved retry.

    Reached only from the approved branch of
    :func:`resolve_rejected_release_terminal` — a still-``done`` run whose
    persisted authoritative final acceptance is NOT rejected. An earlier REJECTED
    attempt of the same run could have left two kinds of stale residue:

    * top-level terminal-rejection markers written by a prior finalization —
      ``halt_reason`` / ``halted_at`` / ``rejected_outcome`` /
      ``delivery_override`` and the nested ``halt`` block;
    * a phantom rejected ``commit_delivery`` gate written by
      ``run.py::_run_commit_delivery`` — a ``not_applicable`` / refused record
      carrying a non-``APPROVED`` ``release_verdict``. On the approved retry the
      now-APPROVED delivery decision resolved to ``not_applicable`` / ``no_diff``
      and early-returned without overwriting ``commit_delivery``, so the stale
      rejected record survives and ``delivery_decision_state`` mis-reads it as a
      decidable correction gate.

    Both are evicted idempotently so meta.json, ``run.end``, SDK status, and the
    delivery-gate projection all reconcile to the authoritative APPROVED verdict.

    Pointwise and conservative on delivery: a ``commit_delivery`` whose
    ``release_verdict`` is ``'APPROVED'`` or empty/absent is the legitimate
    current applied/parked APPROVED gate (including one parked on
    verification/scope per ADR 0099/0100) and is left untouched. The
    accompanying ``multi_project_delivery`` block is dropped only when it mirrors
    the same superseded phantom (its ``primary_status`` equals the evicted
    record's ``status``).
    """
    evict_transient_settle_keys(session)

    # Conditional delivery eviction is *decision-logic* (WHEN to clear), not
    # residue: only a phantom rejected ``commit_delivery`` gate is dropped, and
    # an APPROVED / parked gate is left intact. The canonical set never touches
    # these keys; this guard stays on the reducer call-path (ADR 0115 slice 1).
    delivery = session.get("commit_delivery")
    if not isinstance(delivery, Mapping):
        return
    if not is_release_blocked(delivery.get("release_verdict"), empty_blocks=False):
        return
    stale_status = str(delivery.get("status") or "")
    session.pop("commit_delivery", None)
    companion = session.get("multi_project_delivery")
    if (
        isinstance(companion, Mapping)
        and str(companion.get("primary_status") or "") == stale_status
    ):
        session.pop("multi_project_delivery", None)


# ── SDK commit-delivery settle reducer ──────────────────────────────────────
#
# The terminal flip an applied SDK delivery decision settles to: a successful
# delivery (committed / applied / skipped) finishes ``done``, anything else
# halts. The SDK executor (``sdk/run_control/delivery.py::_finalize``) owns the
# delivery facts — it resolves the applied ``status``, the ``halt_reason`` (from
# ``COMMIT_DELIVERY_HALT_REASONS``), the ``halted_at`` timestamp, and the
# accepted/blocker shaping — and hands them here so the status flip and the
# canonical eviction live on the single reducer call-path, not open-coded in the
# orchestration body.

#: Applied SDK commit-delivery statuses that settle the run to ``done``. This is
#: the reducer's OWN decision input for the settle path and is deliberately
#: distinct from :data:`_DELIVERY_APPLIED_STATUSES`: a ``skipped`` delivery is a
#: clean done outcome for the SDK settle, but it did NOT actually ship the diff,
#: so it is not an applied-override status in the rejected-release reconcile.
_DELIVERY_DONE_STATUSES: frozenset[str] = frozenset(
    {"committed", "applied_uncommitted", "skipped"},
)


def settle_delivery_terminal(
    meta: dict[str, Any],
    *,
    applied_status: str,
    halt_reason: str,
    halted_at: str | None = None,
) -> str:
    """Settle an applied SDK commit-delivery decision to its terminal status.

    The single reducer home for the done↔halted flip an applied delivery
    settles to. The SDK executor resolves the delivery facts and passes them in:
    the applied ``applied_status``, the already-resolved ``halt_reason`` (from
    ``COMMIT_DELIVERY_HALT_REASONS``), and the ``halted_at`` ISO string the SDK
    stamped (the reducer never computes a timestamp). Two mutually-exclusive
    branches, byte-identical to the prior open-coded ``_finalize`` tail:

    * ``applied_status`` in :data:`_DELIVERY_DONE_STATUSES`
      (``committed`` / ``applied_uncommitted`` / ``skipped``) → settle ``done``
      via :func:`mark_run_done`, then the canonical
      :func:`evict_transient_settle_keys` so any halt residue or prior
      rejected-attempt marker is cleared. ``commit_delivery`` is intentionally
      left untouched — the caller just overwrote it with the applied decision,
      and the canonical eviction set never touches delivery keys.
    * otherwise → flip to ``halted`` via :func:`mark_run_halted` with the
      supplied ``halt_reason`` / ``halted_at`` and **no** eviction: a failed /
      operator-halt delivery keeps its halt residue.

    Returns the resulting terminal outcome (``'done'`` / ``'halted'``) so the
    caller can carry it onto the typed result without re-reading ``status``.

    Pure in-place mutation: like the rest of ``run_state`` this does no file IO,
    emits no events, touches no checkpoint, and computes no timestamp; it depends
    only on its sibling ``terminal`` writers (and the shared ``release_verdict``
    detectors imported by this module).
    """
    if applied_status in _DELIVERY_DONE_STATUSES:
        mark_run_done(meta)
        evict_transient_settle_keys(meta)
        return "done"

    mark_run_halted(meta, halt_reason=halt_reason, halted_at=halted_at)
    return "halted"


def supersede_parent_meta(
    parent_meta: MutableMapping[str, Any],
    *,
    child_run_id: str,
    child_status: str,
    delivery_status: str,
) -> None:
    """Reconcile a rejected-FA / correction *parent* meta to ``done`` in place.

    The pure-mutation core of the cross-run supersede (ADR 0115 slice 3b-1): the
    finalization seam (``_supersede_parent_correction_after_followup``) owns all
    the file IO and guards — it loads the parent ``meta.json`` off disk, confirms
    a delivered ``from_run_plan`` child and a genuine rejected-FA / fix terminal,
    then hands the loaded mapping here and persists the result. This function
    never does IO and imports no runtime / SDK / control module.

    Three in-place steps reconcile the parent so it stops reading as an active
    correction candidate across every surface:

    * canonical transient-residue eviction via
      :func:`evict_transient_settle_keys` (``rejected_outcome`` / ``halt_reason``
      / ``halted_at`` / ``halt`` / ``delivery_override`` …);
    * an UNCONDITIONAL drop of the parent's whole delivery record
      (``commit_delivery`` + ``multi_project_delivery``): a *delivered* follow-up
      shipped the diff, so the parent's gate is stale regardless of its verdict.
      That unconditional WHEN-to-clear is site-local decision-logic, deliberately
      kept OUT of the canonical ``TRANSIENT_SETTLE_KEYS`` set (which never touches
      delivery keys) — distinct from the *conditional* phantom-gate guard in
      :func:`supersede_same_run_residue`;
    * settle the parent to ``done`` through :func:`mark_run_done` and stamp the
      durable ``superseded_by_followup`` marker referencing this child, so the
      delivery gate, diagnose, and live status all read the parent as
      superseded/closed rather than active.
    """
    evict_transient_settle_keys(parent_meta)
    # Site-local decision: a *delivered* follow-up supersedes the parent's whole
    # delivery record unconditionally (the child shipped the diff), so the
    # parent's ``commit_delivery`` / ``multi_project_delivery`` are stale here
    # too. That unconditional WHEN-to-clear is this call-site's decision-logic,
    # deliberately kept out of the canonical residue set (ADR 0115 slice 1).
    parent_meta.pop("commit_delivery", None)
    parent_meta.pop("multi_project_delivery", None)
    mark_run_done(parent_meta)
    parent_meta["superseded_by_followup"] = {
        "child_run_id": child_run_id,
        "child_status": child_status,
        "delivery_status": delivery_status,
        "reason": "correction delivered via ordinary follow-up",
    }


__all__ = [
    "EngineBackstopReason",
    "apply_no_diff_terminal",
    "normalize_engine_reason",
    "resolve_rejected_release_terminal",
    "resolve_terminal_outcome",
    "settle_delivery_terminal",
    "supersede_parent_meta",
    "supersede_same_run_residue",
]
