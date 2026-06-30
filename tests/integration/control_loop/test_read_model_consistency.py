"""Core lifecycle ↔ SDK read-model consistency on real run artifacts.

Drives real mock ``run_pipeline`` runs (via ``_harness`` drivers) to six
named lifecycle states and proves the SDK read-model agrees with the durable
meta each run leaves on disk:

* ``run_diagnosis(...).condition`` equals the expected closed-set string
  (``sdk/run_control/diagnosis.py``);
* the attached ``run_diagnosis(...).recovery`` (RecoveryLineage) matches an
  independent ``recovery_lineage(...)`` call;
* ``delivery_decision_state(...).{decidable,kind}`` agrees with the condition.

Two classes of **drift invariant** are added on top of the equality checks.
They are written so that a classifier regression would FAIL here:

1. ACTIVE (the originating bug class): an independent set of
   ``pipeline.control.resume_context`` terminality predicates is evaluated on
   the captured running-meta. ALL must be False, and the SDK must then call
   the run ``active`` — never terminal / ``resume_inert_terminal`` /
   ``needs_decision``. This would fail if the classifier ever marked a live
   (``status='running'``) run as terminal or awaiting-decision.

2. TERMINAL FAMILY: an independent resume_context terminality predicate is
   computed on the same settled meta and cross-checked against the SDK
   condition family. This would fail if the SDK diverged from the settled meta
   (e.g. a ``final_acceptance_rejected`` halt no longer mapping into the
   correction/recover/inert family, or a live run leaking into it).

Finally, two resume/decide round-trips go through the real SDK command layer
(``decide_delivery`` and ``RunService.decide_handoff`` built via
``build_decision_command``) and re-diagnose to confirm a consistent condition
transition.

Determinism / serial-safety: mock provider, fixed run id, temp run dirs, and
the markers below. No production classification / settle logic is touched.
"""
from __future__ import annotations

import functools
import json
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from pipeline.control.resume_context import (
    detect_active_followup_child,
    is_terminal_commit_delivery_pending,
    is_terminal_final_acceptance_rejected,
    is_terminal_phase_handoff_halt,
    is_terminal_resume_parent,
    is_terminal_success,
)
from pipeline.project.finalization import (
    _supersede_parent_correction_after_followup,
    _supersede_stale_rejection_residue,
)
from pipeline.run_state.release_verdict import is_release_blocked
from pipeline.run_state.terminal import (
    TRANSIENT_SETTLE_KEYS,
    evict_transient_settle_keys,
)
from pipeline.run_state.terminal_outcome import settle_delivery_terminal
from sdk.phase_handoff import phase_handoff_decide
from sdk.run_control import (
    RunService,
    build_decision_command,
    decide_delivery,
    delivery_decision_state,
    load_run_snapshot,
    recovery_lineage,
    run_diagnosis,
)
from tests.integration.control_loop import _harness as H

pytestmark = [pytest.mark.project_run, pytest.mark.serial]


# ── Expected core→SDK matrix ─────────────────────────────────────────────────
# condition: the exact closed-set string the SDK must report.
# decidable / kind: the delivery_decision_state agreement for that condition.
EXPECTED: dict[str, dict[str, object]] = {
    "active": {
        "condition": "active", "decidable": False, "kind": "none",
    },
    "resume_inert_terminal": {
        "condition": "resume_inert_terminal", "decidable": False, "kind": "none",
    },
    "needs_decision": {
        "condition": "needs_decision", "decidable": False, "kind": "none",
    },
    "needs_delivery_decision": {
        "condition": "needs_delivery_decision", "decidable": True, "kind": "delivery",
    },
    "correction_followup_required": {
        "condition": "correction_followup_required",
        "decidable": True, "kind": "correction",
    },
    "failed": {
        "condition": "failed", "decidable": False, "kind": "none",
    },
}

# ``active`` is the only state whose on-disk meta is overwritten by
# finalization before we read it; its real artifact is the running-meta the
# driver captured mid-run, fed back through the legitimate ``meta=`` embedder
# seam. Every other state is read straight from disk (the real read path).
_USE_CAPTURED_META = frozenset({"active"})


# ── Shared real-run fixture (read-only consumers) ────────────────────────────

@pytest.fixture(scope="module")
def states(tmp_path_factory: pytest.TempPathFactory) -> dict[str, H.DriverResult]:
    """Run each driver exactly once into its own temp run dir.

    Read-only consumers share these results. The resume/decide round-trip
    tests do NOT use this fixture — they drive fresh runs because deciding
    mutates the run on disk.
    """
    out: dict[str, H.DriverResult] = {}
    for name, driver in H.ALL_DRIVERS.items():
        base = tmp_path_factory.mktemp(f"cl_{name}")
        out[name] = driver(base)
    return out


def _views(res: H.DriverResult, *, use_captured_meta: bool):
    """Compute the three SDK read-model views for one run.

    For ``active`` the disk meta is already finalized, so the captured
    running-meta is supplied via the ``meta=`` seam; otherwise everything is
    read from disk via ``runs_dir`` (the real read path).
    """
    runs_dir = res.run_dir.parent
    meta_kw = {"meta": res.meta} if use_captured_meta else {}
    diag = run_diagnosis(res.run_id, runs_dir=runs_dir, cwd=None, **meta_kw)
    rl = recovery_lineage(res.run_id, runs_dir=runs_dir, cwd=None, **meta_kw)
    dds = delivery_decision_state(res.run_id, runs_dir=runs_dir, cwd=None, **meta_kw)
    return diag, rl, dds


# ── (a)+(b) core→SDK consistency for every state ─────────────────────────────

@pytest.mark.parametrize("name", list(EXPECTED))
def test_condition_recovery_and_delivery_state_agree(
    states: dict[str, H.DriverResult], name: str,
) -> None:
    """Per state: exact ``condition`` + RecoveryLineage agreement (diagnosis's
    attached recovery == an independent ``recovery_lineage(...)`` call) +
    ``delivery_decision_state.{decidable,kind}`` consistent with the condition.
    """
    res = states[name]
    expect = EXPECTED[name]
    diag, rl, dds = _views(res, use_captured_meta=name in _USE_CAPTURED_META)

    # (a) exact closed-set condition string.
    assert diag.condition == expect["condition"], (
        f"{name}: SDK condition {diag.condition!r} != {expect['condition']!r}"
    )

    # (b1) the diagnosis-attached recovery agrees with an independent call.
    assert diag.recovery is not None, f"{name}: recovery lineage missing"
    assert diag.recovery.continuation_subject == rl.continuation_subject, (
        f"{name}: recovery continuation_subject drift "
        f"{diag.recovery.continuation_subject!r} != {rl.continuation_subject!r}"
    )
    assert diag.recovery.recommended_next_action == rl.recommended_next_action, (
        f"{name}: recovery recommended_next_action drift "
        f"{diag.recovery.recommended_next_action!r} != "
        f"{rl.recommended_next_action!r}"
    )

    # (b2) delivery_decision_state agrees with the condition.
    assert dds.decidable is expect["decidable"], (
        f"{name}: dds.decidable {dds.decidable!r} != {expect['decidable']!r}"
    )
    assert dds.kind == expect["kind"], (
        f"{name}: dds.kind {dds.kind!r} != {expect['kind']!r}"
    )
    # A decidable delivery/correction gate exists IFF the condition is one of
    # the two delivery-gated conditions — never for active/inert/needs_decision.
    decidable_conditions = {"needs_delivery_decision", "correction_followup_required"}
    assert (diag.condition in decidable_conditions) is bool(dds.decidable), (
        f"{name}: decidable={dds.decidable} inconsistent with condition "
        f"{diag.condition!r}"
    )


# ── ACTIVE: explicit assertion + drift invariant ─────────────────────────────

# The independent terminality predicates evaluated against the captured
# running-meta. They are imported straight from resume_context, NOT routed
# through the SDK path, so this is a genuine second opinion.
_ACTIVE_TERMINAL_PREDICATES = (
    is_terminal_success,
    is_terminal_final_acceptance_rejected,
    is_terminal_commit_delivery_pending,
    is_terminal_resume_parent,
    is_terminal_phase_handoff_halt,
)


def test_active_is_live_run_and_never_terminal_or_decision(
    states: dict[str, H.DriverResult],
) -> None:
    """ACTIVE (F1) — the live-run invariant that motivated this harness.

    A real running-meta (``status='running'``, captured mid-run) must:

    * classify as exactly ``active``;
    * carry a recovery lineage that reflects a LIVE run — a self-continuation
      with no terminal/rejected flag, not a source-recovery and not a closed
      dead-end;
    * expose NO decidable delivery/correction gate.

    DRIFT INVARIANT: every independent ``resume_context`` terminality predicate
    is False on this meta, and the SDK is then REQUIRED to say ``active`` and
    is forbidden from saying terminal / ``resume_inert_terminal`` /
    ``needs_decision`` / ``needs_delivery_decision`` /
    ``correction_followup_required``. This fails loudly if the classifier ever
    regresses to marking a live run as terminal or awaiting-decision — the
    exact drift this task exists to catch.
    """
    res = states["active"]
    assert res.meta.get("status") == "running", "active fixture is not a live run"

    # The captured running-meta must be a clean live-run artifact: none of the
    # priority fields that would steer the classifier away from `active` may be
    # present yet. If bootstrap ever started writing phase_handoff / halt_reason
    # / parent_run_id into the running-meta, the active invariant below could
    # pass for the wrong reason — pin the artifact shape explicitly here.
    assert not res.meta.get("phase_handoff"), "active running-meta must not carry phase_handoff"
    assert not res.meta.get("halt_reason"), "active running-meta must not carry halt_reason"
    assert not res.meta.get("parent_run_id"), "active running-meta must not carry parent_run_id"

    diag, rl, dds = _views(res, use_captured_meta=True)

    # Explicit active assertion.
    assert diag.condition == "active"

    # Recovery reflects a live run, not source-recovery / not a closed dead-end.
    assert diag.recovery is not None
    assert diag.recovery.is_terminal_or_rejected is False
    assert diag.recovery.continuation_subject == "none"
    assert diag.recovery.recommended_next_action is None
    assert diag.recovery.continuation_subject != "source_run_checkpoint"
    assert diag.recovery.active_child_run_id is None

    # No decidable delivery/correction gate on a live run.
    assert dds.decidable is False
    assert dds.kind == "none"

    # Independent terminality second opinion: ALL predicates are False.
    for predicate in _ACTIVE_TERMINAL_PREDICATES:
        assert predicate(res.meta) is False, (
            f"active running-meta unexpectedly matched {predicate.__name__}"
        )

    # Therefore the SDK MUST say active and NEVER a terminal/awaiting family.
    forbidden = {
        "resume_inert_terminal",
        "needs_decision",
        "needs_delivery_decision",
        "correction_followup_required",
        "recover_via_source_run",
        "closed_by_followup",
        "superseded_by_child",
        "failed",
    }
    assert diag.condition == "active"
    assert diag.condition not in forbidden


# ── TERMINAL FAMILY: drift invariant ─────────────────────────────────────────

def test_rejected_release_terminal_family_matches_predicate(
    states: dict[str, H.DriverResult],
) -> None:
    """TERMINAL-family drift catch for the rejected-release settle.

    Independently of the SDK path, ``is_terminal_final_acceptance_rejected`` is
    computed on the settled meta and must be True. The SDK condition is then
    constrained to the consistent terminal family
    (``correction_followup_required`` here, or the broader
    recover/inert family) and is FORBIDDEN from being ``active`` or a residual
    resumable status. This would fail if the SDK diverged from the settled
    meta — e.g. if a ``final_acceptance_rejected`` halt stopped mapping into
    the correction family, or a live/residual status leaked in.
    """
    res = states["correction_followup_required"]
    meta = res.meta
    assert meta.get("status") == "halted"
    assert meta.get("halt_reason") == "final_acceptance_rejected"

    # Independent predicate (not via the SDK).
    assert is_terminal_final_acceptance_rejected(meta) is True
    assert is_terminal_resume_parent(meta) is True

    diag, _rl, dds = _views(res, use_captured_meta=False)
    consistent_family = {
        "correction_followup_required",
        "recover_via_source_run",
        "resume_inert_terminal",
    }
    assert diag.condition in consistent_family, (
        f"rejected-release condition {diag.condition!r} left the terminal family"
    )
    assert diag.condition != "active"
    assert diag.condition != "failed"  # not a residual resumable status
    # A rejected release that is still decidable must be a correction gate.
    if dds.decidable:
        assert dds.kind == "correction"


def test_commit_delivery_pending_terminal_family_matches_predicate(
    states: dict[str, H.DriverResult],
) -> None:
    """TERMINAL-family drift catch for the parked-delivery settle.

    ``is_terminal_commit_delivery_pending`` is computed independently on the
    settled meta and must be True; the SDK condition must then stay in the
    delivery-gated / recover / inert family and never regress to ``active`` or
    a residual resumable status. Fails if the SDK stopped treating a parked
    ``commit_delivery_pending`` halt as a decidable terminal.
    """
    res = states["needs_delivery_decision"]
    meta = res.meta
    assert meta.get("status") == "halted"
    assert meta.get("halt_reason") == "commit_delivery_pending"

    assert is_terminal_commit_delivery_pending(meta) is True
    assert is_terminal_resume_parent(meta) is True

    diag, _rl, dds = _views(res, use_captured_meta=False)
    consistent_family = {
        "needs_delivery_decision",
        "correction_followup_required",
        "recover_via_source_run",
        "resume_inert_terminal",
    }
    assert diag.condition in consistent_family
    assert diag.condition != "active"
    assert diag.condition != "failed"
    assert dds.decidable is True
    assert dds.kind == "delivery"


# ── Reducer-seam parity: the two migrated terminal sites (ADR 0115 slice 3b-1) ─
# Real mock runs that exercise the two finalization sites now routed through
# ``pipeline.run_state.terminal_outcome``. Each asserts the DURABLE terminal form
# (status + halt_reason + the relevant markers) the reducer left on disk is
# byte-identical to the pre-migration open-coded behavior — proving the seam
# changed no terminal outcome.

# The exact ``no_op_outcome`` marker the no-diff REJECTED settle records. Mirrors
# the dict the open-coded site wrote pre-migration (and the unit pin in
# ``tests/unit/.../test_finalize_done_order.py``); reproduced here so a drift in
# the reducer's own marker shape fails on a real run, not only in a unit stub.
_NO_OP_OUTCOME_EXPECT = {
    "phase": "final_acceptance",
    "review_target": "uncommitted",
    "diff": "none",
    "reason": "final_acceptance_no_diff",
    "status": "halted",
    "message": (
        "Final acceptance rejected the run, but review_changes skipped "
        "because there was no uncommitted diff to review or deliver."
    ),
}


def test_state_halt_settles_halted_with_nested_halt_block(fresh_base) -> None:
    """MIGRATED SITE 1 (``_resolve_terminal_status``): a ``state.halt`` run.

    A malformed closing-release payload halts the run via ``state.stop``. The
    pre-delivery reducer must settle the durable meta to ``status='halted'`` with
    a top-level ``halt_reason`` AND the nested ``halt`` compat block carrying the
    same reason plus the halting phase — the byte-for-byte shape the open-coded
    body produced before the seam.
    """
    res = H.drive_state_halt_terminal(fresh_base)
    meta = res.meta

    assert meta.get("status") == "halted"
    halt_reason = meta.get("halt_reason")
    # The state.halt reason is the final_acceptance hard-contract rejection.
    assert isinstance(halt_reason, str)
    assert halt_reason.startswith("final_acceptance contract rejected:"), halt_reason

    # Nested ``halt`` compat block preserved: same reason + the halting phase
    # (downstream consumers read ``halt.phase``).
    assert meta.get("halt") == {"reason": halt_reason, "phase": "final_acceptance"}


def test_no_diff_rejected_settles_halted_no_op_outcome(fresh_base) -> None:
    """MIGRATED SITE 2 (``_apply_no_diff_final_acceptance_outcome``): no-diff reject.

    A verify-only run with no diff target + incomplete implement evidence settles
    ``done`` pre-delivery, then the post-delivery reducer flips it to
    ``status='halted'`` / ``halt_reason='final_acceptance_no_diff'`` and records
    the ``no_op_outcome`` marker — verbatim the pre-migration shape. Because the
    pre-delivery status was ``done`` (no ``state.halt``), there is NO nested
    ``halt`` block, distinguishing this reducer branch from site 1.
    """
    res = H.drive_no_diff_rejected_terminal(fresh_base)
    meta = res.meta

    assert meta.get("status") == "halted"
    assert meta.get("halt_reason") == "final_acceptance_no_diff"
    assert meta.get("no_op_outcome") == _NO_OP_OUTCOME_EXPECT
    # This branch is the post-delivery no-diff reducer, not the state.halt path:
    # no nested halt compat block is written.
    assert meta.get("halt") is None


# The fixed scaffold of the ``rejected_outcome`` marker the reducer's
# rejected-not-delivered branch (``resolve_rejected_release_terminal``) records.
# Byte-identical to the dict the open-coded finalization site wrote before the
# ADR 0115 slice 3b-1 migration; reproduced here so a drift in the reducer's own
# marker shape fails on a real run, not only in a unit stub. The variable
# ``release_blockers`` / ``short_summary`` are asserted structurally below.
_REJECTED_OUTCOME_SCAFFOLD = {
    "phase": "final_acceptance",
    "reason": "final_acceptance_rejected",
    "status": "halted",
    "release_verdict": "REJECTED",
    "message": (
        "Final acceptance rejected the release and delivery was not "
        "applied, so the run halted instead of finishing as done."
    ),
}


def test_rejected_not_delivered_settles_halted_rejected_outcome(fresh_base) -> None:
    """MIGRATED SITE 3 (``_apply_rejected_release_terminal_outcome``): rejected halt.

    APPROVED review/plan loop + a REJECTED closing release gate with delivery NOT
    applied. The post-delivery reducer flips the still-``done`` session to
    ``status='halted'`` / ``halt_reason='final_acceptance_rejected'`` and records
    the ``rejected_outcome`` marker — the byte-for-byte durable form the
    open-coded body produced before the seam delegated the decision to the
    reducer. No nested ``halt`` block (the pre-delivery status was ``done``, not a
    ``state.halt``), distinguishing this from the state.halt site.
    """
    res = H.drive_correction_followup_required(fresh_base)
    meta = res.meta

    assert meta.get("status") == "halted"
    assert meta.get("halt_reason") == "final_acceptance_rejected"
    assert meta.get("halt") is None

    rejected = meta.get("rejected_outcome")
    assert isinstance(rejected, dict)
    # Fixed scaffold is byte-identical to the pre-migration marker.
    for key, value in _REJECTED_OUTCOME_SCAFFOLD.items():
        assert rejected.get(key) == value, f"rejected_outcome.{key} drifted"
    # Variable fields are carried through intact: the REJECTED gate shipped a
    # real blocker and a short summary, both surfaced on the durable marker.
    blockers = rejected.get("release_blockers")
    assert isinstance(blockers, list) and len(blockers) >= 1
    assert isinstance(rejected.get("short_summary"), str) and rejected["short_summary"]


def test_approved_retry_supersede_settles_clean_done(fresh_base) -> None:
    """MIGRATED SITE 3, not-rejected branch (``supersede_same_run_residue``).

    A clean APPROVED review + release run reaches the reducer's not-rejected
    branch, which runs the same-run supersede: canonical transient-residue
    eviction with no rejection markers written. The durable form is a clean
    ``status='done'`` carrying NONE of the rejection-terminal markers — identical
    to the pre-migration open-coded supersede path. The conditional phantom-gate
    ``commit_delivery`` drop inside that supersede is exercised directly against
    a seeded residue mapping in
    ``test_eviction_one_set_canon_and_finalization_site_a``.
    """
    res = H.drive_resume_inert_terminal(fresh_base)
    meta = res.meta

    assert meta.get("status") == "done"
    assert meta.get("halt_reason") is None
    # The supersede branch never stamps a rejection terminal on an approved run.
    for residue in ("rejected_outcome", "delivery_override", "halt", "halted_at"):
        assert residue not in meta, f"approved supersede left {residue!r} residue"


# ── Resume/decide round-trips through the real SDK command layer ──────────────

@pytest.fixture
def fresh_base(tmp_path) -> Iterator:
    """A fresh temp base for a single mutating round-trip run."""
    yield tmp_path


def test_delivery_skip_roundtrip_transitions_consistently(fresh_base) -> None:
    """needs_delivery_decision → ``decide_delivery(skip)`` → re-diagnosis.

    The real delivery command layer resolves the parked gate, ships nothing,
    and finalizes the run; the SDK condition must transition consistently from
    ``needs_delivery_decision`` to the resolved terminal
    ``resume_inert_terminal``.
    """
    res = H.drive_needs_delivery_decision(fresh_base)
    runs_dir = res.run_dir.parent

    before = run_diagnosis(res.run_id, runs_dir=runs_dir, cwd=None)
    assert before.condition == "needs_delivery_decision"
    before_dds = delivery_decision_state(res.run_id, runs_dir=runs_dir, cwd=None)
    assert before_dds.decidable is True

    result = decide_delivery(res.run_id, "skip", runs_dir=runs_dir, cwd=None)
    assert result.accepted is True
    assert result.terminal_outcome == "done"

    after = run_diagnosis(res.run_id, runs_dir=runs_dir, cwd=None)
    assert after.condition == "resume_inert_terminal", (
        f"post-skip condition {after.condition!r} not the resolved terminal"
    )
    # The gate is no longer decidable once resolved.
    after_dds = delivery_decision_state(res.run_id, runs_dir=runs_dir, cwd=None)
    assert after_dds.decidable is False


def test_handoff_halt_roundtrip_transitions_consistently(fresh_base) -> None:
    """needs_decision → ``build_decision_command`` + ``RunService.decide_handoff``
    (``halt``) → re-diagnosis.

    The pending phase-handoff action is read from the real run snapshot, a
    validated command is built, and the real ``phase_handoff_decide`` executor
    runs it (run-discovery context injected into the service's decide callable,
    the same context the CLI/MCP would resolve). The SDK condition must
    transition consistently from ``needs_decision`` to the resolved terminal
    ``resume_inert_terminal`` (halted / ``phase_handoff_halt``).
    """
    res = H.drive_needs_decision(fresh_base)
    runs_dir = res.run_dir.parent

    before = run_diagnosis(res.run_id, runs_dir=runs_dir, cwd=None)
    assert before.condition == "needs_decision"

    snapshot = load_run_snapshot(res.run_id, runs_dir=runs_dir, cwd=None)
    pending = snapshot.pending_action
    assert pending is not None and pending.kind == "phase_handoff"
    assert "halt" in pending.available_actions

    command = build_decision_command(pending, "halt")
    service = RunService(
        decide=functools.partial(phase_handoff_decide, runs_dir=runs_dir, cwd=None),
    )
    service.decide_handoff(command)

    # The decision persisted a terminal phase-handoff halt.
    meta_after = json.loads((res.run_dir / "meta.json").read_text())
    assert meta_after.get("status") == "halted"
    assert meta_after.get("halt_reason") == "phase_handoff_halt"

    after = run_diagnosis(res.run_id, runs_dir=runs_dir, cwd=None)
    assert after.condition == "resume_inert_terminal", (
        f"post-halt condition {after.condition!r} not the resolved terminal"
    )


# ── Cross-surface parity: SDK settle ↔ finalization terminal (ADR 0115 3b-3) ──
# Both the core finalization tail and the SDK ``decide_delivery`` settle now
# derive their done↔halted terminal from the SINGLE reducer in
# ``pipeline.run_state.terminal_outcome`` (``settle_delivery_terminal``). The
# three tests below pin that one source across the two surfaces on real mock
# runs:
#
#   * APPROVED → both surfaces settle ``done``. The durable terminal CORE
#     finalization leaves on disk (a clean approved run) and the durable terminal
#     the SDK settle leaves (an approved parked gate resolved via
#     ``decide_delivery``) are compared as INDEPENDENT real runs — not a re-call
#     of the reducer — so a real divergence between the surfaces would split them.
#   * REJECTED → the SDK refuses to ship and leaves core finalization's halted
#     terminal untouched. This is the REFUSAL read-shape invariant (the shipping
#     guard refuses BEFORE ``_finalize`` runs, so the done-settle is structurally
#     unreachable for a rejected verdict); it is labelled as such, NOT as a run
#     of the SDK settle path.
#   * SDK settle HALTED branch → a real ``decide_delivery(halt)`` that DOES reach
#     ``_finalize`` settles ``halted`` and is cross-checked against an independent
#     reducer recomputation, proving the settle/terminal invariant on the
#     non-done branch the two refusal cases never execute.


def _reducer_terminal_for(applied_status: str) -> tuple[str, str]:
    """Second-opinion: settle a throwaway meta through the SAME reducer.

    Returns ``(terminal_outcome, status)`` the single
    ``settle_delivery_terminal`` reducer produces for ``applied_status`` — the
    exact function both production surfaces route through. Computed on a fresh
    mapping (no IO), so it is an independent recomputation, not a read of the run
    the SDK just settled.
    """
    probe: dict[str, object] = {"status": "halted"}
    outcome = settle_delivery_terminal(
        probe, applied_status=applied_status, halt_reason="commit_delivery_failed",
    )
    return outcome, str(probe["status"])


def test_sdk_settle_matches_finalization_terminal_for_approved(fresh_base) -> None:
    """APPROVED → CORE finalization and the SDK settle leave the IDENTICAL
    ``done`` durable terminal on two independent real mock runs.

    Rather than re-calling the reducer on the SDK result (which would only prove
    the reducer agrees with itself), this compares the durable terminal of two
    INDEPENDENT real runs that reach a terminal through the two surfaces this
    slice unifies:

    * CORE FINALIZATION — a clean APPROVED review+release run
      (``drive_resume_inert_terminal``) whose finalization tail settled the
      durable meta to ``done`` with no delivery decision involved.
    * SDK SETTLE — an APPROVED parked-delivery gate
      (``drive_needs_delivery_decision``) resolved via the real
      ``decide_delivery(skip)`` command, whose ``_finalize`` routes the
      done↔halted flip through ``settle_delivery_terminal``.

    DRIFT INVARIANT: both durable terminals must be the IDENTICAL approved-done
    shape — ``status='done'`` + ``is_terminal_success`` True + NOT a rejected-FA
    terminal — and the SDK result reports ``terminal_outcome='done'``. Because
    both surfaces now route the flip through the one reducer, a regression that
    split them (e.g. the SDK stopped treating a done settle as ``done``) would
    break the equality. Non-vacuous: the reducer yields ``halted`` for a non-done
    applied status (asserted below), so a done↔halted flip is actually caught.
    """
    # CORE FINALIZATION surface: a clean approved run settles ``done`` on disk,
    # with no delivery decision in the loop — the finalization-only terminal.
    core_res = H.drive_resume_inert_terminal(fresh_base / "core")
    core_meta = core_res.meta
    assert core_meta.get("status") == "done"
    assert is_terminal_success(core_meta) is True
    assert is_terminal_final_acceptance_rejected(core_meta) is False

    # SDK SETTLE surface: an APPROVED parked gate settles ``done`` via the real
    # ``decide_delivery`` command layer, routing ``_finalize`` through the reducer.
    sdk_res = H.drive_needs_delivery_decision(fresh_base / "sdk")
    runs_dir = sdk_res.run_dir.parent
    meta_path = sdk_res.run_dir / "meta.json"
    parked = json.loads(meta_path.read_text(encoding="utf-8"))
    assert parked.get("status") == "halted"
    assert parked.get("halt_reason") == "commit_delivery_pending"
    assert (
        is_release_blocked(
            parked.get("commit_delivery", {}).get("release_verdict"),
            empty_blocks=False,
        )
        is False
    )

    result = decide_delivery(sdk_res.run_id, "skip", runs_dir=runs_dir, cwd=None)
    assert result.accepted is True
    assert result.terminal_outcome == "done"
    sdk_meta = json.loads(meta_path.read_text(encoding="utf-8"))

    # DRIFT: the two INDEPENDENT surfaces left the IDENTICAL approved-done durable
    # terminal — same status, same terminality predicates.
    assert sdk_meta.get("status") == core_meta.get("status") == "done", (
        "SDK settle terminal diverged from core finalization for an APPROVED "
        f"verdict: SDK status={sdk_meta.get('status')!r} vs "
        f"core status={core_meta.get('status')!r}"
    )
    assert is_terminal_success(sdk_meta) is True
    assert is_terminal_final_acceptance_rejected(sdk_meta) is False
    assert result.terminal_outcome == sdk_meta.get("status") == "done"
    # Non-vacuous: the same reducer yields 'halted' for a non-done status, so a
    # done↔halted flip would actually be caught by the equality above.
    assert _reducer_terminal_for("commit_failed") == ("halted", "halted")


def test_sdk_refuses_rejected_release_leaving_finalization_terminal(
    fresh_base,
) -> None:
    """REJECTED → the SDK refuses to ship and leaves core finalization's halted
    terminal untouched (refusal READ-SHAPE invariant, NOT the done-settle path).

    A rejected final-acceptance verdict can never reach the SDK done-settle: the
    shipping guard refuses ``approve`` / ``apply`` BEFORE ``_finalize`` runs, so
    ``settle_delivery_terminal`` is structurally unreachable for a rejected
    verdict. This test therefore covers the REFUSAL read-shape explicitly — it
    does NOT exercise the SDK settle path — proving the complementary single-
    source guarantee in the opposite direction from the APPROVED case: neither
    core finalization nor the SDK ever turns a REJECTED verdict into a ``done``.

    * CORE FINALIZATION — ``drive_correction_followup_required`` settled the run
      to ``halted`` / ``final_acceptance_rejected`` (independently confirmed by
      ``is_terminal_final_acceptance_rejected``).
    * SDK READ — ``decide_delivery(approve)`` refuses with
      ``blocker='release_blocked'`` / ``terminal_outcome='halted'`` and leaves
      the durable meta byte-stable at its halted terminal — never ``done``.
    """
    res = H.drive_correction_followup_required(fresh_base)
    runs_dir = res.run_dir.parent
    meta_path = res.run_dir / "meta.json"

    # Finalization surface: a REJECTED release halted the run.
    before = json.loads(meta_path.read_text(encoding="utf-8"))
    assert before.get("status") == "halted"
    assert before.get("halt_reason") == "final_acceptance_rejected"
    assert is_terminal_final_acceptance_rejected(before) is True

    # SDK read surface: the shipping action on the rejected release is refused
    # (BEFORE ``_finalize``) and never flips the run to done.
    result = decide_delivery(res.run_id, "approve", runs_dir=runs_dir, cwd=None)
    assert result.accepted is False
    assert result.blocker == "release_blocked"
    assert result.terminal_outcome == "halted"

    # The run is left at its finalization terminal — byte-stable, never done.
    after = json.loads(meta_path.read_text(encoding="utf-8"))
    assert after.get("status") == "halted"
    assert after.get("halt_reason") == "final_acceptance_rejected"
    assert is_terminal_final_acceptance_rejected(after) is True

    # Both surfaces agree the verdict is halted, never done.
    assert result.terminal_outcome == after.get("status") == "halted"


def test_sdk_settle_halts_via_operator_halt_matching_reducer(fresh_base) -> None:
    """SDK settle HALTED branch → ``decide_delivery(halt)`` reaches ``_finalize``
    and settles ``halted``, agreeing with an independent reducer recomputation.

    The two refusal cases above never run ``_finalize`` (the guard refuses
    earlier), so they cannot prove the SDK settle's HALTED branch agrees with the
    reducer. This drives a real APPROVED parked gate through
    ``decide_delivery(halt)`` — an operator halt that DOES reach ``_finalize`` —
    so ``settle_delivery_terminal`` actually flips the run to ``halted`` on disk.

    DRIFT INVARIANT: the durable terminal the SDK settle left and an independent
    ``settle_delivery_terminal`` recomputation for the applied (``halted``) status
    are the IDENTICAL ``halted`` pair. A regression that settled a halted delivery
    to ``done`` would split the two and fail. Non-vacuous: the reducer yields
    ``done`` for a done status (asserted below).
    """
    res = H.drive_needs_delivery_decision(fresh_base)
    runs_dir = res.run_dir.parent
    meta_path = res.run_dir / "meta.json"

    result = decide_delivery(res.run_id, "halt", runs_dir=runs_dir, cwd=None)
    assert result.terminal_outcome == "halted"
    settled = json.loads(meta_path.read_text(encoding="utf-8"))
    assert settled.get("status") == "halted"
    assert is_terminal_success(settled) is False

    # DRIFT: independent reducer recomputation agrees on the SAME halted pair the
    # real SDK settle just produced through ``_finalize``.
    reducer_outcome, reducer_status = _reducer_terminal_for(result.status)
    assert (reducer_outcome, reducer_status) == (result.terminal_outcome, "halted"), (
        "SDK delivery settle diverged from the reducer on the HALTED branch: "
        f"SDK={result.terminal_outcome!r}/{settled.get('status')!r} "
        f"vs reducer={reducer_outcome!r}/{reducer_status!r}"
    )
    # Non-vacuous: the same reducer yields 'done' for a done status.
    assert _reducer_terminal_for("skipped") == ("done", "done")


# ── Lineage states (T3): real parent+child runs ──────────────────────────────
# These two conditions need two sibling runs in one runs_dir, so they use the
# T3 lineage drivers (parent + follow-up / from-run-plan child) rather than the
# single-run state matrix above. Both were verified reachable by real mock runs
# with NO production changes; the captured running parent-meta fed via the
# ``source_meta`` seam is a real artifact, not a hand-authored dict.


def test_superseded_by_child_consistency_and_drift(fresh_base) -> None:
    """``superseded_by_child`` — a parent with a live follow-up child.

    Consistency: the parent diagnoses as ``superseded_by_child``; its attached
    recovery (and an independent ``recovery_lineage`` call) point at the active
    child (``continuation_subject='active_child_run'``,
    ``recommended_next_action='resume_active_child'``); no decidable delivery
    gate exists.

    DRIFT: an independent ``detect_active_followup_child`` (the resume_context
    primitive, NOT via the SDK) is computed on the real on-disk metas and must
    find the same child id. This fails if the SDK ever stopped deferring a
    superseded parent to its live child while the child still runs.
    """
    res = H.drive_superseded_by_child(fresh_base)

    diag = run_diagnosis(res.parent_run_id, runs_dir=res.runs_dir, cwd=None)
    rl = recovery_lineage(res.parent_run_id, runs_dir=res.runs_dir, cwd=None)
    dds = delivery_decision_state(res.parent_run_id, runs_dir=res.runs_dir, cwd=None)

    assert diag.condition == "superseded_by_child"
    assert diag.recommended_run_id == res.child_run_id

    # Recovery agreement between the two SDK calls.
    assert diag.recovery is not None
    assert diag.recovery.continuation_subject == rl.continuation_subject == "active_child_run"
    assert (
        diag.recovery.recommended_next_action
        == rl.recommended_next_action
        == "resume_active_child"
    )
    assert diag.recovery.active_child_run_id == res.child_run_id

    # No delivery/correction gate on a superseded parent.
    assert dds.decidable is False
    assert dds.kind == "none"

    # DRIFT: independent resume_context detection finds the same live child.
    child = detect_active_followup_child(
        parent_run_id=res.parent_run_id, runs_dir=res.runs_dir,
    )
    assert child is not None, "independent detection found no active child"
    assert child.child_run_id == res.child_run_id
    # The child is genuinely live (non-terminal) on disk — the precondition the
    # SDK superseded branch depends on.
    assert is_terminal_resume_parent(res.child_meta) is False


def test_recover_via_source_run_consistency_and_drift(fresh_base) -> None:
    """``recover_via_source_run`` — a terminal child whose source is resumable.

    Consistency: the terminal ``from_run_plan`` child diagnoses as
    ``recover_via_source_run`` when its source (parent) running-meta is supplied
    via the ``source_meta`` supervisor seam; the attached recovery (and an
    independent ``recovery_lineage`` call) name the source as the resume target
    (``continuation_subject='source_run_checkpoint'``,
    ``recommended_next_action='resume_source_run'``, ``source_run_id`` = parent).

    DRIFT: independently of the SDK, the child meta IS a terminal-resume-parent
    while the source running-meta is NOT — so the only forward motion is to
    resume the live source, not the dead child. This fails if the SDK ever
    pointed recovery at the terminal child itself, or treated the live source as
    terminal.
    """
    res = H.drive_recover_via_source_run(fresh_base)
    source_meta = {res.source_run_id: res.source_running_meta}

    diag = run_diagnosis(
        res.child_run_id, runs_dir=res.runs_dir, cwd=None, source_meta=source_meta,
    )
    rl = recovery_lineage(
        res.child_run_id, runs_dir=res.runs_dir, cwd=None, source_meta=source_meta,
    )
    dds = delivery_decision_state(res.child_run_id, runs_dir=res.runs_dir, cwd=None)

    assert diag.condition == "recover_via_source_run"

    # Recovery agreement: both SDK calls route to the source checkpoint.
    assert diag.recovery is not None
    assert (
        diag.recovery.continuation_subject
        == rl.continuation_subject
        == "source_run_checkpoint"
    )
    assert (
        diag.recovery.recommended_next_action
        == rl.recommended_next_action
        == "resume_source_run"
    )
    assert diag.recovery.source_run_id == rl.source_run_id == res.source_run_id
    assert diag.recommended_run_id == res.source_run_id
    # A terminal dead-end child carries no live delivery gate of its own.
    assert dds.decidable is False

    # DRIFT: independent terminality — dead child, live source.
    assert is_terminal_resume_parent(res.child_meta) is True
    assert is_terminal_success(res.child_meta) is True
    assert is_terminal_resume_parent(res.source_running_meta) is False
    assert res.source_running_meta.get("status") == "running"


# ── Cross-run parent supersede: the migrated site-B reducer (ADR 0115 slice 3b-1)
# Real rejected-FA parent + a ``from_run_plan`` child, reproducing the
# stale-correction bug on a live core→SDK lineage (not seeded meta). The parent's
# durable terminal is rewritten only as a consequence of the child's REAL
# delivery; a child that does not deliver leaves the parent halted. This proves
# the no-return guarantee both ways through the migrated
# ``terminal_outcome.supersede_parent_meta`` reducer behind the finalization seam.


def test_delivered_followup_supersedes_parent_to_done(fresh_base) -> None:
    """POSITIVE: a delivered ``from_run_plan`` child reconciles its rejected-FA
    parent to ``done`` + ``superseded_by_followup``, evicting the stale residue.

    The parent dead-ended at ``halted`` / ``final_acceptance_rejected`` with a
    phantom ``commit_delivery`` gate and a ``rejected_outcome`` marker. Its
    follow-up child hydrates the parent's plan, APPROVES, and actually commits a
    run-owned diff. The child's finalize then runs the migrated reducer
    (``supersede_parent_meta``) behind the seam: it settles the parent to ``done``,
    stamps ``superseded_by_followup`` referencing this child, and evicts the whole
    stale correction residue so the parent stops reading as an active correction
    everywhere.
    """
    res = H.drive_parent_superseded_by_followup_delivery(
        fresh_base, child_delivers=True,
    )

    # The child genuinely delivered and is bound to the parent.
    assert res.child_meta.get("status") == "done"
    child_delivery = res.child_meta.get("commit_delivery")
    assert isinstance(child_delivery, dict)
    assert child_delivery.get("status") == "committed"
    assert res.child_meta.get("plan_source_run_id") == res.parent_run_id

    parent = res.parent_meta
    # Parent reconciled to a clean done with the durable supersede marker.
    assert parent.get("status") == "done"
    assert parent.get("halt_reason") is None
    marker = parent.get("superseded_by_followup")
    assert isinstance(marker, dict)
    assert marker.get("child_run_id") == res.child_run_id
    assert marker.get("child_status") == "done"
    assert marker.get("delivery_status") == "committed"
    assert marker.get("reason") == "correction delivered via from_run_plan follow-up"

    # The stale-correction residue is gone: the canonical transient markers AND
    # the site-local delivery record were evicted, so no surface still reads the
    # parent as a decidable correction.
    for residue in (
        "rejected_outcome",
        "halt_reason",
        "halted_at",
        "halt",
        "delivery_override",
        "commit_delivery",
        "multi_project_delivery",
    ):
        assert residue not in parent, (
            f"superseded parent still carries stale {residue!r}"
        )

    # Read-model agreement: the reconciled parent is no longer a rejected-FA
    # terminal; the SDK reads it as closed by its follow-up (the durable
    # ``superseded_by_followup`` marker drives ``closed_by_followup``), and it
    # carries no decidable delivery gate.
    assert is_terminal_final_acceptance_rejected(parent) is False
    diag = run_diagnosis(res.parent_run_id, runs_dir=res.runs_dir, cwd=None)
    assert diag.condition == "closed_by_followup"
    dds = delivery_decision_state(res.parent_run_id, runs_dir=res.runs_dir, cwd=None)
    assert dds.decidable is False


def test_non_delivering_followup_leaves_parent_halted(fresh_base) -> None:
    """NEGATIVE CONTROL: a follow-up that does NOT deliver must leave the parent
    halted/rejected — superseded only as a consequence of real child delivery.

    Same lineage, but the child itself rejects, so its ``commit_delivery`` resolves
    to ``not_applicable`` (∉ DELIVERED_STATUSES). The seam's unchanged
    'child delivered' precondition then short-circuits BEFORE any parent rewrite,
    so the parent keeps its full rejected-FA terminal — the precise stale-correction
    state that the positive case clears. This is the no-return proof: nothing but a
    real delivery may close the parent.
    """
    res = H.drive_parent_superseded_by_followup_delivery(
        fresh_base, child_delivers=False,
    )

    # The child is bound to the parent but did NOT deliver.
    assert res.child_meta.get("plan_source_run_id") == res.parent_run_id
    child_delivery = res.child_meta.get("commit_delivery")
    child_status = (
        child_delivery.get("status") if isinstance(child_delivery, dict) else None
    )
    assert child_status not in ("committed", "applied_uncommitted", "skipped")

    parent = res.parent_meta
    # Parent UNCHANGED: still the rejected-FA terminal, never superseded.
    assert parent.get("status") == "halted"
    assert parent.get("halt_reason") == "final_acceptance_rejected"
    assert "superseded_by_followup" not in parent
    assert is_terminal_final_acceptance_rejected(parent) is True
    # The rejected residue is intact (the bug state the positive case reconciles).
    assert "rejected_outcome" in parent
    assert "commit_delivery" in parent

    # Read-model agreement: the parent is still a live correction follow-up gate.
    diag = run_diagnosis(res.parent_run_id, runs_dir=res.runs_dir, cwd=None)
    assert diag.condition == "correction_followup_required"


# ── Eviction consistency (ADR 0115 slice 1) ──────────────────────────────────
# Two complementary guards that the three formerly-divergent stale-key eviction
# lists now collapse onto the single canonical set
# (``pipeline.run_state.terminal.TRANSIENT_SETTLE_KEYS``):
#
#   1. INTEGRATION — real settled meta from every terminal driver carries no
#      transient marker that should have been cleared, and each driver's
#      status / halt_reason are unchanged (eviction-only).
#   2. CONTRAST — seed a mapping with every transient key and run each of the
#      three eviction call-sites; the cleared canonical set is identical for
#      all three and equal to TRANSIENT_SETTLE_KEYS. The expectation is bound to
#      the imported constant, so re-introducing a divergent hand-rolled list
#      (clearing a smaller or larger set) fails the test.

# Per-terminal-driver expectation. ``retained`` is the subset of the canonical
# set that is the *genuine terminal record* for that driver (not residue) and so
# is legitimately allowed to remain. ``must_clear`` (= constant − retained) is
# the residue that MUST be gone after settle. ``done`` retains nothing → its
# whole canonical set must be cleared (the headline "no transient marker
# survives a terminal settle").
_TERMINAL_EVICTION_EXPECT: dict[str, dict[str, object]] = {
    "resume_inert_terminal": {
        "status": "done",
        "halt_reason": None,
        "retained": frozenset(),
    },
    "needs_delivery_decision": {
        "status": "halted",
        "halt_reason": "commit_delivery_pending",
        # genuine parked-halt record
        "retained": frozenset({"halt", "halt_reason", "halted_at"}),
    },
    "correction_followup_required": {
        "status": "halted",
        "halt_reason": "final_acceptance_rejected",
        # genuine rejected-terminal record: the halt + its rejected_outcome marker
        "retained": frozenset({"halt", "halt_reason", "halted_at", "rejected_outcome"}),
    },
    "failed": {
        "status": "failed",
        "halt_reason": "phase_failure:RuntimeError",
        # ``failed`` preserves halt_reason AND any active phase_handoff
        "retained": frozenset({"halt_reason", "phase_handoff"}),
    },
}


@pytest.mark.parametrize("name", list(_TERMINAL_EVICTION_EXPECT))
def test_terminal_settle_evicts_transient_markers_eviction_only(
    states: dict[str, H.DriverResult], name: str,
) -> None:
    """INTEGRATION: a real settled meta carries no should-be-cleared transient
    marker, and the driver's status / halt_reason are unchanged (eviction-only).

    For each terminal driver, the residue that must be gone is the canonical
    constant minus the keys that are the genuine terminal record for that driver
    (``retained``). The ``done`` driver retains nothing, so its entire canonical
    set must be absent — the headline invariant. Simultaneously the exact
    (status, halt_reason) pair is asserted to prove no settle DECISION changed.
    """
    res = states[name]
    meta = res.meta
    expect = _TERMINAL_EVICTION_EXPECT[name]

    # Eviction-only: the terminal DECISION (status / halt_reason) is unchanged.
    assert meta.get("status") == expect["status"], (
        f"{name}: status {meta.get('status')!r} != {expect['status']!r}"
    )
    assert meta.get("halt_reason") == expect["halt_reason"], (
        f"{name}: halt_reason {meta.get('halt_reason')!r} != {expect['halt_reason']!r}"
    )

    # No should-be-cleared transient marker survived. Bound to the imported
    # constant: a new canonical key automatically becomes a checked key here.
    # Absence (``not in``) is the precise invariant — a key left present with a
    # falsy value is still a survivor a ``meta.get`` truthiness check would miss.
    must_clear = set(TRANSIENT_SETTLE_KEYS) - set(expect["retained"])  # type: ignore[arg-type]
    for key in must_clear:
        assert key not in meta, (
            f"{name}: transient marker {key!r} survived terminal settle"
        )


def test_resume_inert_terminal_clears_entire_canonical_set(
    states: dict[str, H.DriverResult],
) -> None:
    """The strongest form of the headline invariant: a clean settle-to-``done``
    leaves NONE of the canonical transient keys on the durable meta."""
    meta = states["resume_inert_terminal"].meta
    assert meta.get("status") == "done"
    survivors = [k for k in TRANSIENT_SETTLE_KEYS if k in meta]
    assert not survivors, f"done settle left transient markers: {survivors}"


# Documented-retained NON-canonical durable / sibling markers that NO eviction
# path may drop (terminal.py "Deliberately NOT in the set"). Seeding them lets
# the contrast tests assert an EXACT removed-set and so catch *over-eviction* —
# a re-introduced hand-rolled ``pop('phase_handoff_waiver')`` (or any other
# documented-retained key) at a call-site, the dangerous residue-bug direction
# terminal.py flags as audit-state loss. None of these are in
# ``TRANSIENT_SETTLE_KEYS``, so an honest canonical eviction never removes them.
_RETAINED_NON_CANONICAL: tuple[str, ...] = (
    "phase_handoff_waiver",    # durable ADR 0073 operator waiver (evidence reads post-settle)
    "phase_handoff_override",  # state.extras sibling, not a key of this mapping
    "human_feedback",          # state.extras sibling, not residue
    "last_critique",           # reviewed-loop sibling, not residue
)


def _seed_all_transient(extra: dict | None = None) -> dict:
    """A mapping carrying every canonical transient key (truthy unique values),
    every documented-retained NON-canonical durable / sibling marker, and an
    unrelated sentinel — all of which must survive any canonical eviction.

    The retained markers are what makes the contrast tests catch over-eviction:
    they let each test assert the EXACT removed-set, so popping a non-canonical
    durable key fails ``removed == canonical`` instead of slipping through a
    ``canonical - after`` computation that only ever inspects canonical keys.
    """
    seeded = {key: {"_seeded": key} for key in TRANSIENT_SETTLE_KEYS}
    for key in _RETAINED_NON_CANONICAL:
        seeded[key] = {"_seeded": key}
    seeded["keep_me_sentinel"] = "preserve"
    if extra:
        seeded.update(extra)
    return seeded


def test_eviction_one_set_canon_and_finalization_site_a() -> None:
    """CONTRAST: the canonical helper and ``_supersede_stale_rejection_residue``
    clear the IDENTICAL set, equal to ``TRANSIENT_SETTLE_KEYS``.

    Both run on identically seeded mappings carrying — beyond the canonical keys
    — every documented-retained durable / sibling marker and an unrelated
    sentinel. The *exact* removed-set (seed − survivors) is computed and asserted
    equal to the imported constant. Re-introducing a divergent hand-rolled list
    makes ``removed != canonical`` and fails: under-eviction drops a canonical
    key from ``removed``; **over-eviction** of a documented-retained durable key
    (e.g. ``pop('phase_handoff_waiver')``) enlarges ``removed`` past the
    constant. The site-A guard is also exercised: an APPROVED ``commit_delivery``
    (and its companion mirror) are left untouched — proving those delivery keys
    are conditional call-site decision-logic, never part of the unconditional
    canonical set.
    """
    canonical = set(TRANSIENT_SETTLE_KEYS)

    # (1) the canonical helper itself. Exact removed-set, not a canonical-only
    # diff: a stray pop of a retained durable marker would show up here.
    m_canon = _seed_all_transient()
    before_canon = set(m_canon)
    evict_transient_settle_keys(m_canon)
    removed_canon = before_canon - set(m_canon)
    assert m_canon.get("keep_me_sentinel") == "preserve"
    for key in _RETAINED_NON_CANONICAL:
        assert key in m_canon, f"canonical helper over-evicted retained {key!r}"

    # (2) finalization site A, with an APPROVED delivery the guard must keep.
    approved_delivery = {"release_verdict": "APPROVED", "status": "committed"}
    companion = {"primary_status": "committed"}
    m_site_a = _seed_all_transient(
        extra={
            "commit_delivery": dict(approved_delivery),
            "multi_project_delivery": dict(companion),
        },
    )
    before_site_a = set(m_site_a)
    _supersede_stale_rejection_residue(m_site_a)
    removed_site_a = before_site_a - set(m_site_a)
    assert m_site_a.get("keep_me_sentinel") == "preserve"
    # Guard preserved: APPROVED delivery + companion mirror untouched (not
    # cleared as residue) — conditional decision-logic, not the canonical set.
    assert m_site_a.get("commit_delivery") == approved_delivery
    assert m_site_a.get("multi_project_delivery") == companion
    for key in _RETAINED_NON_CANONICAL:
        assert key in m_site_a, f"site A over-evicted retained {key!r}"

    # The one-set invariant, bound to the constant: removed is EXACTLY canonical
    # for both paths — no fewer (under-eviction) and no more (over-eviction).
    assert removed_canon == canonical
    assert removed_site_a == canonical
    assert removed_canon == removed_site_a == canonical


def test_eviction_finalization_site_b_parent_supersede(tmp_path) -> None:
    """CONTRAST: ``_supersede_parent_correction_after_followup`` evicts the same
    canonical set from the parent meta (its site-local unconditional delivery
    clear is exercised separately).

    A minimal stale parent meta (seeded with every transient key + the
    documented-retained durable markers + a sentinel + a delivery record) is
    written to a real run dir; a fake delivered-follow-up child drives the
    supersede. The reloaded parent's EXACT removed-set is asserted to be the
    canonical constant PLUS exactly the two site-local delivery keys — no more:
    every documented-retained durable marker and the sentinel survive
    (over-eviction guard), the parent settles ``done`` (eviction-only output),
    and the site-local ``commit_delivery`` / ``multi_project_delivery`` clear is
    confirmed as call-site decision-logic distinct from the canonical set.
    """
    runs_dir = tmp_path / "runs"
    parent_dir = runs_dir / "parent_run"
    parent_dir.mkdir(parents=True)
    child_dir = runs_dir / "child_run"
    child_dir.mkdir()

    parent_meta = _seed_all_transient(
        extra={
            "status": "halted",
            # makes is_terminal_final_acceptance_rejected(parent_meta) True
            "halt_reason": "final_acceptance_rejected",
            "commit_delivery": {"status": "committed"},
            "multi_project_delivery": {"primary_status": "committed"},
        },
    )
    (parent_dir / "meta.json").write_text(json.dumps(parent_meta), encoding="utf-8")
    before_parent = set(parent_meta)

    # Minimal fake run: a delivered (``committed``) follow-up child pointing at
    # the parent via plan_source_run_id — the only inputs the helper reads.
    run = SimpleNamespace(
        output_dir=child_dir,
        session={"commit_delivery": {"status": "committed"}, "status": "done"},
        session_ts="child_run",
        state=SimpleNamespace(extras={"plan_source_run_id": "parent_run"}),
    )
    _supersede_parent_correction_after_followup(run)

    settled = json.loads((parent_dir / "meta.json").read_text(encoding="utf-8"))
    # Every canonical transient key evicted (bound to the constant).
    survivors = [k for k in TRANSIENT_SETTLE_KEYS if k in settled]
    assert not survivors, f"site B left transient markers: {survivors}"
    # EXACT removed-set: canonical PLUS exactly the two site-local delivery keys
    # — no more. An over-eviction of a documented-retained durable marker would
    # enlarge ``removed`` past this expected set and fail. (``status`` survives
    # as ``done`` and ``superseded_by_followup`` is an addition, so neither is in
    # ``removed``.)
    removed = before_parent - set(settled)
    assert removed == set(TRANSIENT_SETTLE_KEYS) | {
        "commit_delivery",
        "multi_project_delivery",
    }, f"site B removed an unexpected set: {removed}"
    # Site-local decision (not part of the canonical set): the parent's whole
    # delivery record is also dropped because the child shipped the diff.
    assert "commit_delivery" not in settled
    assert "multi_project_delivery" not in settled
    # Over-eviction guard: every documented-retained durable marker survives.
    for key in _RETAINED_NON_CANONICAL:
        assert key in settled, f"site B over-evicted retained {key!r}"
    # Eviction-only output + sentinel survives.
    assert settled.get("status") == "done"
    assert settled.get("keep_me_sentinel") == "preserve"
    assert settled.get("superseded_by_followup", {}).get("child_run_id") == "child_run"


def test_eviction_sdk_delivery_settle_path(fresh_base) -> None:
    """CONTRAST: the SDK delivery settle done-branch evicts the same canonical
    set on a real ``decide_delivery(skip)`` round-trip.

    A real parked-delivery run's meta is seeded with extra stale residue markers,
    the documented-retained durable markers, and a sentinel, then resolved via
    the real SDK command layer. The settled ``done`` meta has every canonical
    transient key gone (bound to the constant) while the unrelated sentinel AND
    every documented-retained durable marker survive (over-eviction guard: a
    re-introduced hand-rolled ``pop`` of e.g. ``phase_handoff_waiver`` would drop
    one of these and fail).
    """
    res = H.drive_needs_delivery_decision(fresh_base)
    runs_dir = res.run_dir.parent
    meta_path = res.run_dir / "meta.json"

    parked = json.loads(meta_path.read_text(encoding="utf-8"))
    assert parked.get("status") == "halted"
    assert parked.get("halt_reason") == "commit_delivery_pending"
    # Seed stale residue the prior-attempt finalization could have left behind,
    # plus the documented-retained durable markers the settle must NOT drop.
    parked.update(
        {
            "rejected_outcome": {"_seeded": "rejected_outcome"},
            "no_op_outcome": {"_seeded": "no_op_outcome"},
            "correction_fixed_point": {"_seeded": "correction_fixed_point"},
            "delivery_override": {"_seeded": "delivery_override"},
            "halt": {"reason": "stale", "phase": "x"},
            "keep_me_sentinel": "preserve",
            **{key: {"_seeded": key} for key in _RETAINED_NON_CANONICAL},
        },
    )
    meta_path.write_text(json.dumps(parked), encoding="utf-8")

    result = decide_delivery(res.run_id, "skip", runs_dir=runs_dir, cwd=None)
    assert result.accepted is True
    assert result.terminal_outcome == "done"

    settled = json.loads(meta_path.read_text(encoding="utf-8"))
    assert settled.get("status") == "done"
    survivors = [k for k in TRANSIENT_SETTLE_KEYS if k in settled]
    assert not survivors, f"SDK delivery settle left transient markers: {survivors}"
    # Over-eviction guard: the unrelated sentinel and every documented-retained
    # durable marker are preserved.
    assert settled.get("keep_me_sentinel") == "preserve"
    for key in _RETAINED_NON_CANONICAL:
        assert key in settled, f"SDK delivery settle over-evicted retained {key!r}"
