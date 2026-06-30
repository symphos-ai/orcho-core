"""ADR 0115 slice 4 — cross settle/handoff residue + verdict parity (T3).

Harness choice (explicit, per the slice-4 escape hatch). The slice-4 spec
PREFERS extending ``tests/integration/control_loop/`` with a full multi-alias
mock cross run driven to a settled terminal. A real end-to-end cross run only
reaches a settled terminal by dispatching one child ``run_project_pipeline``
per alias through real git-worktree isolation (the heaviest test class —
``git_worktree`` + ``slow_process`` + ``filesystem_heavy``) and then
deterministically steering CFA approval and cross delivery. That is
disproportionate for pinning the NARROW residue/verdict invariants ADR 0115
slice 4 (T1 cross-eviction split + T2 cross-verdict single source) actually
changed. The slice-4 done-criteria explicitly permit asserting the same
invariants through the existing cross slice with a REAL settle (not a
hand-authored terminal meta). This module does exactly that: it drives the
production settle writers — ``finalize_cross_run`` / ``finalize_cross_terminal``
/ ``evict_cross_handoff_markers`` — which persist ``meta.json`` +
``cross_checkpoint.json`` to a real run dir, then reloads BOTH artifacts from
disk. Nothing here writes a settled meta by hand; every asserted terminal is
the byte the production settle left on disk.

The per-child cross-delivery verdict routing through the single
``release_verdict`` source (T2) is already pinned behaviourally by
``test_cross_delivery.py`` (``test_override_delivers_despite_rejected_child_*``
/ ``test_rejected_child_without_override_*``); this module pins the
complementary cross-TERMINAL verdict agreement.

Invariants pinned on real persisted disk state:

1. RESIDUE — a settled cross terminal carries no ``pending_gate`` residue in
   EITHER the durable meta or the cross checkpoint, including the gate-resume
   path where ``pending_gate`` was restored into the session/checkpoint from a
   prior pause (``run_setup`` + ``contract_check`` write it to both); and no
   stale ``phase_handoff`` / ``phase_handoff_kind`` (+ siblings) survive. The
   two eviction sets are DISJOINT: handoff consumption clears the
   kind-discriminated markers but never ``pending_gate`` (settle-only); settle
   clears ``pending_gate`` but is not a handoff site.
2. VERDICT — the settled cross verdict agrees with the single
   ``release_verdict`` source: ``is_approved`` ⇒ ``done`` with no delivery
   halt_reason; otherwise a cross halt-leaf
   (``cross_final_acceptance_failed`` / ``cross_delivery_failed`` /
   ``phase_handoff_halt``).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pipeline.cross_project.checkpoint import (
    read_cross_checkpoint,
    write_cross_checkpoint,
)
from pipeline.cross_project.finalization import (
    CrossFinalizationContext,
    finalize_cross_run,
)
from pipeline.cross_project.terminal import finalize_cross_terminal
from pipeline.run_state.release_verdict import is_approved, is_release_blocked
from pipeline.run_state.terminal import (
    CROSS_HANDOFF_MARKER_KEYS,
    CROSS_SETTLE_RESIDUE_KEYS,
    evict_cross_handoff_markers,
)

pytestmark = pytest.mark.cross_project

_PENDING_GATE = {
    "name": "contract_check",
    "run_policy": "always",
    "choices": ["run", "skip"],
    "on_skip": "block",
}


def _read_meta(run_dir: Path) -> dict:
    return json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))


def _make_cfa(*, approved: bool, source: str = "agent") -> SimpleNamespace:
    """A real ``CrossFinalAcceptanceResult``-shaped stand-in: the finalizer
    reads only ``parsed.approved`` + ``source``."""
    return SimpleNamespace(
        parsed=SimpleNamespace(approved=approved), source=source,
    )


def _session_with_residue() -> dict:
    """A cross session as it looks right before a settle on the gate-resume
    path: a stale active ``phase_handoff`` and the ``pending_gate`` that
    ``run_setup`` restored from the prior pause's meta."""
    return {
        "run_id": "TEST_CROSS_RUN",
        "status": "awaiting_gate_decision",
        "phase_handoff": {
            "id": "cfa:1",
            "phase": "cross_final_acceptance",
            "available_actions": ["continue", "halt"],
        },
        "pending_gate": dict(_PENDING_GATE),
        "phases": {"projects": {}},
    }


def _ctx(
    run_dir: Path,
    session: dict,
    *,
    cfa_result,
    cross_ckpt: dict,
    delivery_result=None,
) -> CrossFinalizationContext:
    return CrossFinalizationContext(
        run_dir=run_dir,
        output_dir=True,
        session=session,
        projects={"api": run_dir / "api"},
        max_rounds=2,
        cfa_result=cfa_result,
        contract_results={},
        contract_check_failed=False,
        contract_check_failure_reason=None,
        cross_phase_usage={},
        delivery_result=delivery_result,
        cross_ckpt=cross_ckpt,
    )


def _finalize(ctx: CrossFinalizationContext):
    """Run the silent finalizer with the artifact mirror stubbed (no project
    dirs to mirror into) — every other write is the real production path."""
    with patch(
        "pipeline.engine.artifact_mirror.mirror_to_projects", return_value=[],
    ):
        return finalize_cross_run(ctx)


# ── (1) RESIDUE — pending_gate cleared from meta AND checkpoint on settle ──


def test_done_settle_clears_pending_gate_from_meta_and_checkpoint(
    tmp_path: Path,
) -> None:
    """Gate-resume → settle: ``pending_gate`` (restored into both the session
    and the persisted checkpoint by the pause/resume seam) is evicted from the
    durable meta AND the on-disk ``cross_checkpoint.json`` at the single
    settle-only clearing point — read back from disk, not asserted in memory.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # The pause wrote pending_gate onto the checkpoint; persist it so the
    # read-back below is a genuine disk round-trip, not an in-memory dict.
    ckpt = {"phase0_done": True, "sub_status": {}, "pending_gate": dict(_PENDING_GATE)}
    write_cross_checkpoint(run_dir, ckpt)
    ckpt = read_cross_checkpoint(run_dir)
    assert ckpt.get("pending_gate") == _PENDING_GATE  # precondition: residue present

    session = _session_with_residue()
    ctx = _ctx(run_dir, session, cfa_result=_make_cfa(approved=True), cross_ckpt=ckpt)
    result = _finalize(ctx)

    assert result.status == "done"
    meta = _read_meta(run_dir)
    assert meta["status"] == "done"
    # Settle residue gone from the durable meta…
    assert "pending_gate" not in meta
    assert "phase_handoff" not in meta
    # …and from the persisted checkpoint (the single settle-only clearing
    # point evicts both the session mirror and the checkpoint copy).
    disk_ckpt = read_cross_checkpoint(run_dir)
    assert "pending_gate" not in disk_ckpt
    # Eviction-only: unrelated checkpoint progress is untouched.
    assert disk_ckpt.get("phase0_done") is True


def test_early_terminal_clears_pending_gate_from_meta_and_checkpoint(
    tmp_path: Path,
) -> None:
    """The OTHER settle entry — the early-return terminal
    ``finalize_cross_terminal`` (contract_check ABORT) — clears the same
    ``pending_gate`` residue from meta + checkpoint on real disk, confirming
    every cross terminal funnels through the one settle-only clearing point.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ckpt = {"phase0_done": False, "sub_status": {}, "pending_gate": dict(_PENDING_GATE)}
    write_cross_checkpoint(run_dir, ckpt)
    ckpt = read_cross_checkpoint(run_dir)

    session = _session_with_residue()
    finalize_cross_terminal(
        run_dir=run_dir,
        session=session,
        status="cancelled",
        halt_reason="cross_gate_aborted:contract_check",
        cross_ckpt=ckpt,
    )

    meta = _read_meta(run_dir)
    assert meta["status"] == "cancelled"
    assert meta["halt_reason"] == "cross_gate_aborted:contract_check"
    assert "pending_gate" not in meta
    assert "phase_handoff" not in meta
    disk_ckpt = read_cross_checkpoint(run_dir)
    assert "pending_gate" not in disk_ckpt


# ── (1) RESIDUE — handoff consumption clears kind markers, NOT pending_gate ─


def test_handoff_consumption_disjoint_from_pending_gate() -> None:
    """Disjointness invariant (the load-bearing T1 split): consuming a cross
    handoff clears the kind-discriminated markers (and flips
    ``phase_handoff_pending`` to ``False``) but must NEVER touch
    ``pending_gate`` — that key is settle-only, cleared at a different site.
    """
    ckpt = {
        "phase0_done": True,
        "phase_handoff_pending": True,
        "phase_handoff_id": "project:1",
        "phase_handoff_kind": "project",
        "phase_handoff_project_alias": "web",
        "phase_handoff_child_id": "child_1",
        "pending_gate": dict(_PENDING_GATE),
    }

    evict_cross_handoff_markers(ckpt)

    # Every handoff marker gone; the active-pause boolean flipped off.
    for key in CROSS_HANDOFF_MARKER_KEYS:
        assert key not in ckpt, f"handoff marker {key!r} survived consumption"
    assert ckpt["phase_handoff_pending"] is False
    # The settle-only key is deliberately untouched by handoff consumption.
    for key in CROSS_SETTLE_RESIDUE_KEYS:
        assert key in ckpt, f"handoff consumption wrongly evicted settle-only {key!r}"
    assert ckpt["pending_gate"] == _PENDING_GATE
    # Eviction-only: unrelated progress preserved.
    assert ckpt["phase0_done"] is True


def test_fully_settled_terminal_carries_no_cross_residue(tmp_path: Path) -> None:
    """End-to-end disk state of a cross run that paused for a handoff AND a
    gate, then settled: the handoff markers were consumed mid-run and
    ``pending_gate`` is cleared by the settle, so the durable meta +
    checkpoint carry NEITHER residue class — proven by reloading both files.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # A checkpoint mid-run carrying both residue classes.
    ckpt = {
        "phase0_done": True,
        "sub_status": {"api": "done"},
        "phase_handoff_pending": True,
        "phase_handoff_id": "cfa:1",
        "phase_handoff_kind": "cfa",
        "phase_handoff_project_alias": "web",
        "phase_handoff_child_id": "child_1",
        "pending_gate": dict(_PENDING_GATE),
    }
    write_cross_checkpoint(run_dir, ckpt)

    # Step 1 — the operator handoff decision is consumed during the run.
    ckpt = read_cross_checkpoint(run_dir)
    evict_cross_handoff_markers(ckpt)
    write_cross_checkpoint(run_dir, ckpt)

    # Step 2 — the run settles to its terminal through the production finalizer.
    ckpt = read_cross_checkpoint(run_dir)
    session = _session_with_residue()
    ctx = _ctx(run_dir, session, cfa_result=_make_cfa(approved=True), cross_ckpt=ckpt)
    assert _finalize(ctx).status == "done"

    meta = _read_meta(run_dir)
    disk_ckpt = read_cross_checkpoint(run_dir)
    assert meta["status"] == "done"
    # No residue of EITHER class in the durable meta…
    assert "pending_gate" not in meta
    assert "phase_handoff" not in meta
    assert "phase_handoff_kind" not in meta
    # …nor in the persisted checkpoint: settle cleared pending_gate, handoff
    # consumption cleared the kind markers.
    assert "pending_gate" not in disk_ckpt
    for key in CROSS_HANDOFF_MARKER_KEYS:
        assert key not in disk_ckpt, f"stale {key!r} survived a settled terminal"
    assert disk_ckpt.get("phase_handoff_pending") is False


# ── (2) VERDICT — settled cross terminal agrees with release_verdict ───────


@pytest.mark.parametrize("verdict", ["APPROVED", "REJECTED"])
def test_cross_terminal_verdict_agrees_with_single_source(
    tmp_path: Path, verdict: str,
) -> None:
    """The settled cross terminal is driven by the SAME ``release_verdict``
    source as mono: an ``is_approved`` verdict settles ``done`` with no
    delivery halt_reason; a blocked verdict settles a cross halt-leaf. The
    finalizer's ``parsed.approved`` is derived from ``is_approved`` here so the
    test asserts agreement with the single source, not a parallel mapping.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    approved = is_approved(verdict)
    session = _session_with_residue()
    ctx = _ctx(
        run_dir,
        session,
        cfa_result=_make_cfa(approved=approved),
        cross_ckpt={"phase0_done": True, "sub_status": {}},
    )
    result = _finalize(ctx)

    meta = _read_meta(run_dir)
    # done IFF the single source approves; the two never disagree.
    assert (meta["status"] == "done") is approved
    assert (meta["status"] == "done") is (not is_release_blocked(verdict, empty_blocks=False))
    if approved:
        assert result.status == "done"
        assert meta.get("halt_reason") is None
    else:
        assert result.status == "failed"
        # A blocked cross verdict lands on the CFA halt-leaf, never a clean done.
        assert meta["halt_reason"] == "cross_final_acceptance_failed"


@pytest.mark.parametrize(
    ("overall", "exp_status", "exp_halt_reason"),
    [
        ("ok", "done", None),
        ("disabled", "done", None),
        ("halted", "halted", "phase_handoff_halt"),
        ("failed", "failed", "cross_delivery_failed"),
        ("partial", "failed", "cross_delivery_partial"),
    ],
)
def test_approved_delivery_overall_maps_to_terminal(
    tmp_path: Path, overall: str, exp_status: str, exp_halt_reason: str | None,
) -> None:
    """With an APPROVED cross verdict the delivery aggregate decides the
    terminal: a clean/disabled delivery stays ``done`` with NO halt_reason;
    every non-clean delivery outcome maps to a cross halt-leaf — the
    "approved ⇒ done without delivery halt_reason; otherwise halt-leaf" rule.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session = _session_with_residue()
    ctx = _ctx(
        run_dir,
        session,
        cfa_result=_make_cfa(approved=True),
        cross_ckpt={"phase0_done": True, "sub_status": {}},
        delivery_result=SimpleNamespace(overall=overall),
    )
    result = _finalize(ctx)

    meta = _read_meta(run_dir)
    assert result.status == exp_status
    assert meta["status"] == exp_status
    assert meta.get("halt_reason") == exp_halt_reason
    if exp_status == "done":
        assert meta.get("halt_reason") is None
