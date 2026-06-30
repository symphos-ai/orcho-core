# SPDX-License-Identifier: Apache-2.0
"""Focused parity unit for the terminal-outcome reducer (ADR 0115 slice 3b-1).

The two halting reducer branches (``state.halt`` + no-diff *rejected*) are pinned
on real mock runs by the control-loop harness. The no-diff *approved* branch
keeps ``status='done'`` and only adds a display marker, so it is not worth a
harness driver — this direct reducer assertion closes that residual-risk gap:
``apply_no_diff_terminal`` on an approved-without-diff session must leave the
status ``done`` and record ``no_change_outcome`` with the exact pre-migration
dict shape.
"""
from __future__ import annotations

import pytest

from pipeline.run_state.terminal import TRANSIENT_SETTLE_KEYS
from pipeline.run_state.terminal_outcome import (
    apply_no_diff_terminal,
    normalize_engine_reason,
    resolve_rejected_release_terminal,
    settle_delivery_terminal,
)


def test_apply_no_diff_terminal_approved_keeps_done_with_no_change_outcome() -> None:
    """approved-without-diff → status stays ``done`` + ``no_change_outcome`` marker.

    The reducer must NOT flip status (an honest verification-only pass that
    produced no diff is still a success) and must write the ``no_change_outcome``
    marker byte-for-byte as the open-coded site did before the seam.
    """
    session = {
        "status": "done",
        "phases": {
            "review_changes": {
                "clean": True,
                "skipped": "no uncommitted changes",
            },
            "final_acceptance": {
                "approved": True,
                "verdict": "APPROVED",
                "ship_ready": True,
            },
        },
    }

    apply_no_diff_terminal(session, diff_path=None)

    assert session["status"] == "done"
    assert "halt_reason" not in session
    assert session["no_change_outcome"] == {
        "phase": "final_acceptance",
        "review_target": "uncommitted",
        "diff": "none",
        "reason": "verification_no_changes",
        "status": "done",
        "message": (
            "Final acceptance approved a verification-only run that "
            "produced no file changes to review or deliver."
        ),
    }


def _settled_meta_with_residue(status: str) -> dict[str, object]:
    """A pre-settle meta seeded with every transient residue key + delivery.

    The applied SDK decision has just overwritten ``commit_delivery``; every
    :data:`TRANSIENT_SETTLE_KEYS` entry carries stale halt/rejection residue from
    a prior attempt that the settle must reconcile.
    """
    meta: dict[str, object] = {
        "status": "halted",
        "commit_delivery": {"status": status, "release_verdict": "APPROVED"},
    }
    for key in TRANSIENT_SETTLE_KEYS:
        meta[key] = f"stale::{key}"
    return meta


@pytest.mark.parametrize(
    "status", ["committed", "applied_uncommitted", "skipped"],
)
def test_settle_delivery_terminal_done_evicts_residue_keeps_delivery(
    status: str,
) -> None:
    """Each delivery-done status → ``done`` + residue evicted, ``commit_delivery`` kept.

    The done branch settles via ``mark_run_done`` then the canonical eviction:
    every transient residue key is cleared, but the just-applied
    ``commit_delivery`` record is left intact (the canonical set never touches
    delivery keys). No ``halt_reason`` / ``halted_at`` is written.
    """
    meta = _settled_meta_with_residue(status)

    outcome = settle_delivery_terminal(
        meta,
        applied_status=status,
        halt_reason="commit_delivery_failed",
        halted_at="2026-06-28T00:00:00+00:00",
    )

    assert outcome == "done"
    assert meta["status"] == "done"
    for key in TRANSIENT_SETTLE_KEYS:
        assert key not in meta
    assert "halt_reason" not in meta
    assert "halted_at" not in meta
    assert meta["commit_delivery"] == {
        "status": status,
        "release_verdict": "APPROVED",
    }


# ── rejected-release terminal: engine-backstop authoritative cause ──────────
#
# A REJECTED terminal forced by the engine receipt backstop must carry the
# engine cause in the persisted marker, not just the agent's positive
# ``short_summary`` over an empty ``release_blockers`` list. These pin both the
# engine-present surfacing and the byte-identical parity for the pure
# agent-blocker REJECT / approved-supersede paths.

#: The dogfood engine_backstop shape: a forced REQUIRED-RECEIPT backstop with a
#: ``{risk, missing_evidence, required_check}`` gap (NOT scope-expansion, so the
#: repro survives ADR 0112-D promotion).
_BACKSTOP_GAP = {
    "risk": "required receipts unproven",
    "missing_evidence": "no passing pytest receipt for the touched slice",
    "required_check": "python -m pytest -q tests/unit/pipeline",
}
_ENGINE_BACKSTOP = {
    "reason": "required_receipts_unproven",
    "gaps": [_BACKSTOP_GAP],
}
_POSITIVE_SUMMARY = "All acceptance criteria met; ship it."


def test_rejected_terminal_surfaces_engine_backstop_reason() -> None:
    """Dogfood: forced engine backstop REJECT with empty blockers + green summary.

    The agent reported ship-ready with no ``release_blockers`` and a positive
    ``short_summary``, but the engine receipt backstop forced REJECTED. The
    rejected marker must name the engine cause (``engine_backstop`` field), lead
    the ``message`` with that cause, and demote the positive agent summary so it
    is not read as the headline.
    """
    session: dict[str, object] = {"status": "done"}
    engine_reason = normalize_engine_reason(engine_backstop=_ENGINE_BACKSTOP)

    resolve_rejected_release_terminal(
        session,
        rejected=True,
        delivery_status="not_applicable",
        verdict="REJECTED",
        blockers=[],
        short_summary=_POSITIVE_SUMMARY,
        engine_reason=engine_reason,
    )

    assert session["status"] == "halted"
    marker = session["rejected_outcome"]
    # Engine cause is named on the marker, read off the same backstop facts.
    assert marker["engine_backstop"] == {
        "reason": "required_receipts_unproven",
        "gaps": [_BACKSTOP_GAP],
    }
    # The message leads with the engine cause as headline.
    assert marker["message"].startswith(
        "Engine backstop rejected the release: required_receipts_unproven."
    )
    # The positive agent summary is demoted, never the headline.
    assert marker["short_summary"] == f"(superseded agent view) {_POSITIVE_SUMMARY}"
    # Agent contract untouched: empty release_blockers stay empty blockers.
    assert marker["release_blockers"] == []


def test_rejected_terminal_surfaces_verification_gaps_without_backstop() -> None:
    """Reviewer ``verification_gaps`` alone (no engine_backstop) still surface."""
    session: dict[str, object] = {"status": "done"}
    engine_reason = normalize_engine_reason(verification_gaps=[_BACKSTOP_GAP])

    resolve_rejected_release_terminal(
        session,
        rejected=True,
        delivery_status="not_applicable",
        verdict="REJECTED",
        blockers=[],
        short_summary=_POSITIVE_SUMMARY,
        engine_reason=engine_reason,
    )

    marker = session["rejected_outcome"]
    assert marker["verification_gaps"] == [_BACKSTOP_GAP]
    assert "engine_backstop" not in marker
    assert marker["message"].startswith(
        "Final acceptance flagged unresolved verification gaps."
    )
    assert marker["short_summary"] == f"(superseded agent view) {_POSITIVE_SUMMARY}"


@pytest.mark.parametrize("status", ["committed", "applied_uncommitted"])
def test_delivery_override_carries_engine_backstop_reason(status: str) -> None:
    """Operator-override REJECT with delivery applied also carries the engine cause.

    The same engine backstop on the override branch: ``status`` stays ``done``
    but the ``delivery_override`` marker names the engine cause, leads the message
    with it, and demotes the positive agent summary.
    """
    session: dict[str, object] = {"status": "done"}
    engine_reason = normalize_engine_reason(engine_backstop=_ENGINE_BACKSTOP)

    resolve_rejected_release_terminal(
        session,
        rejected=True,
        delivery_status=status,
        verdict="REJECTED",
        blockers=[],
        short_summary=_POSITIVE_SUMMARY,
        engine_reason=engine_reason,
    )

    assert session["status"] == "done"  # override keeps done
    override = session["delivery_override"]
    assert override["engine_backstop"] == {
        "reason": "required_receipts_unproven",
        "gaps": [_BACKSTOP_GAP],
    }
    assert override["message"].startswith(
        "Engine backstop rejected the release: required_receipts_unproven."
    )
    assert override["short_summary"] == f"(superseded agent view) {_POSITIVE_SUMMARY}"


def test_rejected_terminal_agent_blocker_marker_byte_identical_parity() -> None:
    """Parity: a real agent-blocker REJECT (no engine cause) is byte-identical.

    With an empty/non-present engine reason the rejected marker must carry NO new
    keys and the exact pre-engine message + verbatim short_summary, so the
    persisted shape of the agent-blocker path never drifts.
    """
    blockers = [{"id": "RB1", "severity": "high", "detail": "data loss"}]
    summary = "Two blockers remain; not ship-ready."

    with_arg: dict[str, object] = {"status": "done"}
    resolve_rejected_release_terminal(
        with_arg,
        rejected=True,
        delivery_status="not_applicable",
        verdict="REJECTED",
        blockers=blockers,
        short_summary=summary,
        engine_reason=normalize_engine_reason(),  # empty → not present
    )

    assert with_arg["rejected_outcome"] == {
        "phase": "final_acceptance",
        "reason": "final_acceptance_rejected",
        "status": "halted",
        "release_verdict": "REJECTED",
        "release_blockers": blockers,
        "message": (
            "Final acceptance rejected the release and delivery was not "
            "applied, so the run halted instead of finishing as done."
        ),
        "short_summary": summary,
    }
    assert "engine_backstop" not in with_arg["rejected_outcome"]
    assert "verification_gaps" not in with_arg["rejected_outcome"]


def test_resolve_rejected_release_terminal_not_rejected_supersede_untouched() -> None:
    """Parity: the not-rejected (approved-supersede) branch is unaffected.

    An engine reason on an APPROVED authoritative verdict must not introduce any
    marker: the supersede branch only evicts stale rejection residue, leaving no
    ``rejected_outcome`` / ``delivery_override``.
    """
    session: dict[str, object] = {
        "status": "done",
        "halt_reason": "final_acceptance_rejected",
        "rejected_outcome": {"stale": True},
    }

    resolve_rejected_release_terminal(
        session,
        rejected=False,
        delivery_status="not_applicable",
        verdict="APPROVED",
        blockers=[],
        short_summary=_POSITIVE_SUMMARY,
        engine_reason=normalize_engine_reason(engine_backstop=_ENGINE_BACKSTOP),
    )

    # Supersede evicted the stale residue; no new rejection marker was written.
    assert session["status"] == "done"
    assert "rejected_outcome" not in session
    assert "delivery_override" not in session
    assert "halt_reason" not in session


@pytest.mark.parametrize(
    ("status", "halt_reason"),
    [
        ("target_dirty", "target_dirty"),
        ("commit_failed", "commit_delivery_failed"),
    ],
)
def test_settle_delivery_terminal_halted_records_reason_no_eviction(
    status: str, halt_reason: str,
) -> None:
    """A non-done delivery → ``halted`` with reason/timestamp and NO eviction.

    The halted branch settles via ``mark_run_halted`` with the SDK-supplied
    ``halt_reason`` / ``halted_at`` and performs no eviction: stale residue keys
    and the delivery record survive the flip.
    """
    meta = _settled_meta_with_residue(status)
    halted_at = "2026-06-28T12:34:56+00:00"

    outcome = settle_delivery_terminal(
        meta,
        applied_status=status,
        halt_reason=halt_reason,
        halted_at=halted_at,
    )

    assert outcome == "halted"
    assert meta["status"] == "halted"
    assert meta["halt_reason"] == halt_reason
    assert meta["halted_at"] == halted_at
    # No eviction on the halt branch: stale rejection residue survives. The
    # writer itself owns ``halt_reason`` / ``halted_at`` (it just wrote them) and
    # ``phase_handoff`` (``mark_run_halted`` clears a stale active handoff); the
    # remaining residue keys are untouched because the halt branch never evicts.
    writer_owned = {"halt_reason", "halted_at", "phase_handoff"}
    for key in TRANSIENT_SETTLE_KEYS:
        if key in writer_owned:
            continue
        assert meta[key] == f"stale::{key}"
    assert meta["commit_delivery"] == {
        "status": status,
        "release_verdict": "APPROVED",
    }
