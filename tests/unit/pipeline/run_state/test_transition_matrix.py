"""Table-driven run-state *transition* matrix (Stage 0/3b/3c brain).

Exercises the allowed/refused lifecycle edges through the production source of
truth, with no providers / subprocess / git / sleep:

- reducer folds (:func:`apply_run_event`) for the snapshot edges
  active→awaiting_handoff, awaiting_handoff→halted, awaiting_handoff→continue
  (a reducer status no-op — resume is owned by the explicit handoff writer),
  active→done/failed/interrupted/halted;
- flat-state writers (:mod:`pipeline.run_state.terminal` /
  :mod:`pipeline.run_state.handoff`) and their load-bearing ``phase_handoff``
  cleanup policy (``done``/``halted``/``clear`` clear it; ``failed``/
  ``interrupted`` preserve it);
- the F2 invariant: ``seen_handoff_ids`` survives a halt that clears the active
  pointer;
- terminal→active is *refused* unless an explicit repair/follow-up owns it —
  a terminal run with a stale handoff is **repairable** (repair clears the
  stale payload, status stays terminal) but never **resumable**, and an
  interrupted run with an undecided handoff is refused
  (``needs_operator_decision``);
- the cross analogue (Stage 3c): ``cross_terminal_with_stale_handoff`` is
  repaired by ``repair_cross_run_state`` only when ``phase_handoff_pending`` is
  NOT set, plus the cross checkpoint pending/action invariants.

All artifacts are tiny JSON trees under the per-test ``tmp_path`` via the T1
helper, so the suite is deterministic and xdist-safe.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pipeline.control.resume_context import is_terminal_resume_parent
from pipeline.run_state import (
    RunStatus,
    apply_run_event,
    classify_cross_run_state,
    clear_active_handoff,
    continue_handoff,
    mark_run_done,
    mark_run_failed,
    mark_run_halted,
    mark_run_interrupted,
    project_events,
    repair_cross_run_state,
    repair_run_state,
    request_active_handoff,
    validate_cross_run_state,
    validate_run_state,
)
from tests.helpers.run_state_dirs import (
    handoff_event,
    run_event,
    write_checkpoint,
    write_cross_meta,
    write_events,
    write_meta,
)

pytestmark = [pytest.mark.run_state, pytest.mark.state_transition]

_H1 = "h1"


def _start(seq: int = 1) -> dict:
    return run_event("run.start", seq=seq, run_kind="single_project", task="t")


def _read_meta(run_dir: Path) -> dict:
    return json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))


# ── A. reducer snapshot transitions ────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReducerCase:
    id: str
    events: list[dict]
    status: RunStatus
    terminal: bool
    active_handoff_id: str | None
    seen_handoff_ids: tuple[str, ...]


_REDUCER_CASES = [
    ReducerCase(
        id="active_to_awaiting_handoff",
        events=[_start(), handoff_event(_H1, "validate_plan", 2)],
        status=RunStatus.AWAITING_PHASE_HANDOFF,
        terminal=False,
        active_handoff_id=_H1,
        seen_handoff_ids=(_H1,),
    ),
    ReducerCase(
        id="awaiting_to_halted_clears_pointer_keeps_history_F2",
        events=[
            _start(),
            handoff_event(_H1, "validate_plan", 2),
            run_event("phase_handoff.decided", seq=3, action="halt", handoff_id=_H1),
        ],
        status=RunStatus.HALTED,
        terminal=True,
        active_handoff_id=None,
        seen_handoff_ids=(_H1,),
    ),
    ReducerCase(
        id="awaiting_continue_is_reducer_status_noop",
        events=[
            _start(),
            handoff_event(_H1, "validate_plan", 2),
            run_event(
                "phase_handoff.decided", seq=3, action="continue", handoff_id=_H1
            ),
        ],
        status=RunStatus.AWAITING_PHASE_HANDOFF,
        terminal=False,
        active_handoff_id=_H1,
        seen_handoff_ids=(_H1,),
    ),
    ReducerCase(
        id="active_to_done",
        events=[_start(), run_event("run.end", seq=2, status="done")],
        status=RunStatus.DONE,
        terminal=True,
        active_handoff_id=None,
        seen_handoff_ids=(),
    ),
    ReducerCase(
        id="active_to_failed",
        events=[_start(), run_event("run.end", seq=2, status="failed")],
        status=RunStatus.FAILED,
        terminal=True,
        active_handoff_id=None,
        seen_handoff_ids=(),
    ),
    ReducerCase(
        id="active_to_interrupted",
        events=[_start(), run_event("run.interrupted", seq=2)],
        status=RunStatus.INTERRUPTED,
        terminal=False,
        active_handoff_id=None,
        seen_handoff_ids=(),
    ),
    ReducerCase(
        id="active_to_halted_via_event",
        events=[_start(), run_event("run.halted", seq=2)],
        status=RunStatus.HALTED,
        terminal=True,
        active_handoff_id=None,
        seen_handoff_ids=(),
    ),
]


@pytest.mark.parametrize("case", _REDUCER_CASES, ids=lambda c: c.id)
def test_reducer_transition(case: ReducerCase) -> None:
    """Folding the stream yields the expected status/terminal/handoff fields."""
    snap = project_events(case.events)
    assert snap.status is case.status
    assert snap.terminal is case.terminal
    assert snap.active_handoff_id == case.active_handoff_id
    assert snap.seen_handoff_ids == case.seen_handoff_ids


def test_f2_seen_handoff_ids_survive_halt_with_multiple_ids() -> None:
    """F2: every requested handoff id is retained in history after a halt."""
    snap = project_events([
        _start(),
        handoff_event(_H1, "validate_plan", 2),
        handoff_event("h2", "review_changes", 3),
        run_event("phase_handoff.decided", seq=4, action="halt", handoff_id="h2"),
    ])
    assert snap.status is RunStatus.HALTED
    assert snap.terminal is True
    assert snap.active_handoff_id is None
    assert snap.seen_handoff_ids == (_H1, "h2")


# ── B. flat-state writer phase_handoff cleanup policy ──────────────────────


@dataclass(frozen=True, slots=True)
class WriterCase:
    id: str
    initial: dict
    apply: Callable[[dict], object]
    status: str
    handoff_present: bool
    extra_fields: dict = field(default_factory=dict)


def _awaiting_state() -> dict:
    return {"status": "awaiting_phase_handoff", "phase_handoff": {"id": _H1}}


def _running_state() -> dict:
    return {"status": "running"}


_WRITER_CASES = [
    WriterCase(
        id="request_active_handoff_running_to_awaiting",
        initial=_running_state(),
        apply=lambda s: request_active_handoff(s, payload={"id": _H1}),
        status="awaiting_phase_handoff",
        handoff_present=True,
    ),
    WriterCase(
        id="clear_active_handoff_awaiting_to_running",
        initial=_awaiting_state(),
        apply=clear_active_handoff,
        status="running",
        handoff_present=False,
    ),
    WriterCase(
        id="continue_handoff_awaiting_to_running",
        initial=_awaiting_state(),
        apply=lambda s: continue_handoff(
            s, handoff_id=_H1, note=None, decided_at=None
        ),
        status="running",
        handoff_present=False,
    ),
    WriterCase(
        id="mark_run_done_clears_handoff",
        initial=_awaiting_state(),
        apply=mark_run_done,
        status="done",
        handoff_present=False,
    ),
    WriterCase(
        id="mark_run_halted_clears_handoff",
        initial=_awaiting_state(),
        apply=lambda s: mark_run_halted(s, halt_reason="phase_handoff_halt"),
        status="halted",
        handoff_present=False,
        extra_fields={"halt_reason": "phase_handoff_halt"},
    ),
    WriterCase(
        id="mark_run_failed_preserves_handoff",
        initial=_awaiting_state(),
        apply=lambda s: mark_run_failed(s, halt_reason="boom"),
        status="failed",
        handoff_present=True,
        extra_fields={"halt_reason": "boom"},
    ),
    WriterCase(
        # interrupted -> failed: the failed writer preserves an active handoff
        # regardless of the prior status (a failure does not resolve an
        # outstanding operator handoff). Guards the interrupted source branch
        # the run.end reducer path does not exercise.
        id="mark_run_failed_from_interrupted_preserves_handoff",
        initial={
            "status": "interrupted",
            "interrupted_at": "2026-06-08T00:00:00Z",
            "phase_handoff": {"id": _H1},
        },
        apply=lambda s: mark_run_failed(s, halt_reason="boom"),
        status="failed",
        handoff_present=True,
        extra_fields={"halt_reason": "boom"},
    ),
    WriterCase(
        id="mark_run_interrupted_preserves_handoff",
        initial=_awaiting_state(),
        apply=lambda s: mark_run_interrupted(s, interrupted_at="2026-06-08T00:00:00Z"),
        status="interrupted",
        handoff_present=True,
        extra_fields={
            "interrupted_at": "2026-06-08T00:00:00Z",
            "halt_reason": "interrupted",
        },
    ),
]


@pytest.mark.parametrize("case", _WRITER_CASES, ids=lambda c: c.id)
def test_writer_transition_and_handoff_policy(case: WriterCase) -> None:
    """Each writer sets the expected status and clears/preserves phase_handoff."""
    state = dict(case.initial)
    case.apply(state)
    assert state["status"] == case.status
    assert ("phase_handoff" in state) is case.handoff_present
    for key, value in case.extra_fields.items():
        assert state[key] == value


# ── C. terminal→active is refused unless owned by repair / follow-up ───────


def test_terminal_stale_handoff_is_repairable_not_resumable(tmp_path: Path) -> None:
    """A halted run with a stale handoff is healed in place — never resumed.

    ``repair_run_state`` clears the stale ``phase_handoff`` but leaves
    ``meta.status`` terminal: repair fixes the torn shape, it does not transition
    the run back to ``running``.
    """
    write_events(tmp_path, [_start(), handoff_event(_H1, "validate_plan", 2)])
    write_meta(
        tmp_path,
        {"status": "halted", "halt_reason": "phase_handoff_halt",
         "phase_handoff": {"id": _H1}},
    )
    # Pre-state: validate flags the stale handoff (a repairable warning).
    assert "terminal_with_stale_handoff" in {
        i.code for i in validate_run_state(tmp_path).issues
    }

    report = repair_run_state(tmp_path, apply=True)

    assert report.applied is True
    meta = _read_meta(tmp_path)
    assert meta["status"] == "halted"  # still terminal — NOT resumed
    assert "phase_handoff" not in meta  # stale payload cleared
    assert {c.field for c in report.changes} == {"phase_handoff"}


def test_interrupted_active_no_decision_refuses_auto_transition(
    tmp_path: Path,
) -> None:
    """An interrupted run with an undecided handoff is not flipped automatically.

    The only path back to active is an explicit operator decision (follow-up),
    so the safe repair refuses: ``needs_operator_decision`` with no write.
    """
    write_events(tmp_path, [_start(), handoff_event(_H1, "validate_plan", 2)])
    write_meta(tmp_path, {"status": "interrupted", "phase_handoff": {"id": _H1}})

    report = repair_run_state(tmp_path, apply=True)

    assert report.applied is False
    assert report.needs_operator_decision is True
    assert report.changes == ()
    meta = _read_meta(tmp_path)
    assert meta["status"] == "interrupted"  # unchanged
    assert meta["phase_handoff"] == {"id": _H1}  # preserved for the operator


def test_terminal_snapshot_not_resurrected_by_continue_decision() -> None:
    """The reducer's resume-decision fold never moves a terminal run to active.

    Once a run is terminal (here ``halted`` via a halt decision), a later
    ``phase_handoff.decided`` ``continue`` event is a reducer no-op — it cannot
    flip the snapshot back to ``running``. Resume of a *terminal* run is owned
    by the explicit resume guard (below), not by passively folding events.
    """
    snap = project_events([
        _start(),
        handoff_event(_H1, "validate_plan", 2),
        run_event("phase_handoff.decided", seq=3, action="halt", handoff_id=_H1),
    ])
    assert snap.status is RunStatus.HALTED
    assert snap.terminal is True

    resumed = apply_run_event(
        snap,
        run_event("phase_handoff.decided", seq=4, action="continue", handoff_id=_H1),
    )
    assert resumed.status is RunStatus.HALTED  # NOT resurrected to running
    assert resumed.terminal is True


@dataclass(frozen=True, slots=True)
class ResumeGuardCase:
    id: str
    meta: dict
    refused: bool


# The run-state handoff writers (clear_active_handoff / continue_handoff) set
# status='running' UNCONDITIONALLY — they are the awaiting→running follow-up
# tail, not a terminal guard. The contract that a terminal run is not silently
# re-activated by a default checkpoint resume is owned by the canonical guard
# pipeline.control.resume_context.is_terminal_resume_parent: a terminal-success
# or halt-reason terminal parent is refused for default checkpoint-resume, and
# only an explicit follow-up task re-activates it.
_RESUME_GUARD_CASES = [
    ResumeGuardCase("terminal_success_done", {"status": "done"}, True),
    ResumeGuardCase(
        "terminal_phase_handoff_halt",
        {"status": "halted", "halt_reason": "phase_handoff_halt"},
        True,
    ),
    ResumeGuardCase(
        "failed_is_resumable", {"status": "failed", "halt_reason": "boom"}, False
    ),
    ResumeGuardCase("interrupted_is_resumable", {"status": "interrupted"}, False),
    ResumeGuardCase(
        "awaiting_handoff_is_resumable",
        {"status": "awaiting_phase_handoff", "phase_handoff": {"id": _H1}},
        False,
    ),
]


@pytest.mark.parametrize("case", _RESUME_GUARD_CASES, ids=lambda c: c.id)
def test_terminal_to_active_refused_by_resume_guard(case: ResumeGuardCase) -> None:
    """terminal→active is refused by the resume guard, not the run-state writers.

    ``is_terminal_resume_parent`` is the canonical owner: it refuses a default
    checkpoint-resume of a terminal-success or halt-reason terminal parent (so
    the run is not auto-flipped to active), while ``failed`` / ``interrupted`` /
    awaiting runs stay resumable. This pins that terminal→active is allowed only
    via the explicit repair/follow-up path, never the bare transition writers.
    """
    assert is_terminal_resume_parent(case.meta) is case.refused


# ── D. cross classification: terminal stale + checkpoint pending/action ────


@dataclass(frozen=True, slots=True)
class CrossCase:
    id: str
    meta: dict
    checkpoint: dict | None
    codes: set[str]


_CROSS_CASES = [
    CrossCase(
        id="cross_terminal_with_stale_handoff",
        meta={"status": "done", "phase_handoff": {"id": "cfa:1"}},
        checkpoint=None,
        codes={
            "cross_terminal_with_stale_handoff",
            "active_handoff_without_checkpoint_pending",
        },
    ),
    CrossCase(
        id="checkpoint_pending_without_active_handoff",
        meta={"status": "running"},
        checkpoint={"phase_handoff_pending": True, "phase_handoff_kind": "plan"},
        codes={"checkpoint_pending_without_active_handoff"},
    ),
    CrossCase(
        id="active_handoff_without_checkpoint_pending",
        meta={"status": "awaiting_phase_handoff",
              "phase_handoff": {"id": "cross_plan:1"}},
        checkpoint={"phase_handoff_pending": False},
        codes={"active_handoff_without_checkpoint_pending"},
    ),
    CrossCase(
        id="cfa_pending_without_paused_state",
        meta={"status": "awaiting_phase_handoff", "phase_handoff": {"id": "cfa:1"}},
        checkpoint={
            "phase_handoff_pending": True,
            "phase_handoff_kind": "cfa",
            "phase_handoff_id": "cfa:1",
        },
        codes={"cfa_pending_without_paused_state"},
    ),
    CrossCase(
        id="pending_gate_and_handoff_active",
        meta={"status": "awaiting_phase_handoff",
              "phase_handoff": {"id": "cross_plan:1"}},
        checkpoint={
            "phase_handoff_pending": True,
            "phase_handoff_kind": "plan",
            "phase_handoff_id": "cross_plan:1",
            "pending_gate": {"gate": "manual_confirm"},
        },
        codes={"pending_gate_and_handoff_active"},
    ),
]


@pytest.mark.parametrize("case", _CROSS_CASES, ids=lambda c: c.id)
def test_cross_classification(tmp_path: Path, case: CrossCase) -> None:
    """Cross checkpoint pending/action combos classify to the expected codes.

    No child run is started — only the durable ``meta.json`` /
    ``cross_checkpoint.json`` artifacts are read.
    """
    write_cross_meta(tmp_path, case.meta)
    if case.checkpoint is not None:
        write_checkpoint(tmp_path, case.checkpoint)
    actual = {i.code for i in validate_cross_run_state(tmp_path)}
    assert actual == case.codes


# ── E. cross terminal stale handoff: Stage 3c repair policy ────────────────


def test_cross_terminal_stale_repairable_when_not_pending(tmp_path: Path) -> None:
    """Stage 3c: clear the stale terminal handoff when checkpoint is NOT pending."""
    write_cross_meta(
        tmp_path, {"status": "halted", "phase_handoff": {"id": "cfa:1"}}
    )
    # No checkpoint file → phase_handoff_pending is False.
    assert classify_cross_run_state(tmp_path).checkpoint_pending is False

    report = repair_cross_run_state(tmp_path, apply=True)

    assert report.applied is True
    meta = _read_meta(tmp_path)
    assert meta["status"] == "halted"  # still terminal — NOT resumed
    assert "phase_handoff" not in meta


def test_cross_terminal_stale_diagnostic_when_pending(tmp_path: Path) -> None:
    """Stage 3c: refuse the clear when ``phase_handoff_pending`` is still set.

    Clearing ``meta.phase_handoff`` alone would downgrade a repairable warning
    into the ``checkpoint_pending_without_active_handoff`` error, so the safe
    cross repair stays diagnostic with ``needs_operator_decision``.
    """
    write_cross_meta(
        tmp_path, {"status": "halted", "phase_handoff": {"id": "cfa:1"}}
    )
    write_checkpoint(
        tmp_path,
        {"phase_handoff_pending": True, "phase_handoff_kind": "cfa",
         "phase_handoff_id": "cfa:1"},
    )

    report = repair_cross_run_state(tmp_path, apply=True)

    assert report.applied is False
    assert report.needs_operator_decision is True
    assert report.changes == ()
    meta = _read_meta(tmp_path)
    assert meta["status"] == "halted"
    assert meta["phase_handoff"] == {"id": "cfa:1"}  # preserved
