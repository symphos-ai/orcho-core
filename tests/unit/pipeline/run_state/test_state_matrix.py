"""Table-driven run-state *state* matrix (Stage 0 brain).

Classifies every known run-state shape through the production source of truth
without providers, subprocess, git, or sleep:

- projection / reachability via :func:`pipeline.run_state.projector.project_run_dir`
  and :attr:`RunStateSnapshot.terminal` / ``status``;
- inconsistency classification via
  :func:`pipeline.run_state.consistency.validate_run_state` — this test asserts
  the *codes*, *severities*, and ``ok`` flag that ``validate_run_state``
  returns; it never re-derives them.

Coverage (per the T0 in-repo contract — ``pipeline/run_state/`` modules):

- all ten :class:`RunStatus` values, both as reducer-reached projections
  (where an event reaches them) and as ``meta.status`` flowing through
  ``validate_run_state``;
- active handoff present / absent;
- decision artifact present / absent;
- ``halt_reason`` present / absent (consistency never reads it — proven
  classification-neutral);
- valid terminal snapshots (``done`` / ``halted`` / ``failed``);
- torn / ragged snapshots: ``halt_decision_without_halted_meta``,
  ``terminal_with_stale_handoff``, ``meta_handoff_without_event``,
  ``interrupted_with_active_handoff`` (+ the ``active_handoff_without_decision``
  info that co-fires).

All artifacts are tiny JSON trees built under the per-test ``tmp_path`` via the
``tests.helpers.run_state_dirs`` builders, so the suite is deterministic and
xdist-safe.

Fast local gates (run all from the repo root)
----------------------------------------------

- **Reducer / run-state inner loop** — the fastest signal while editing
  ``pipeline/run_state/`` (reducer, snapshot, consistency, terminal, handoff,
  cross): ``pytest -q -m "run_state or state_transition"`` (the whole run-state
  slice plus these matrices, well under a second). ``-m run_state`` alone is the
  recommended gate for a run-state change.
- **Lifecycle safety gate** — when touching handoff / finalization / resume
  *wiring* (not just classification), also run the integration guards:
  ``pytest -q tests/unit/pipeline/project -k "finalization or handoff or resume"``,
  ``pytest -q tests/unit/pipeline/cross_project -k "finalization or terminal or handoff or cfa or checkpoint"``,
  and ``pytest -q tests/sdk/test_phase_handoff.py``. These exercise the real
  pipeline / SDK paths that produce the durable shapes the matrices classify.
- **Broad pre-readiness gate** — before calling a change ready, or when editing
  shared contracts / schemas / CLI / orchestration:
  ``pytest -q -m "not e2e and not packaging"``.
- **Still needs the expensive integration tests** — the zero-stdout
  presentation boundary (``project/test_silent_boundary.py``), cross-orchestrator
  dispatch + checkpoint persistence, the public handoff-decision SDK surface, and
  git-worktree / delivery isolation. These guard behaviour the pure run-state
  matrices intentionally do not (real IO, dispatch, persistence), so they stay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pipeline.run_state import (
    RunStatus,
    project_run_dir,
    validate_run_state,
)
from tests.helpers.run_state_dirs import (
    handoff_event,
    run_event,
    write_decision,
    write_events,
    write_meta,
)

pytestmark = [pytest.mark.run_state, pytest.mark.filesystem_light]


# ── severity contract (mirrors pipeline/run_state/consistency.py) ──────────
#
# Single source of truth for the *expected* severity of each diagnosed code,
# transcribed from the T0 contract. The test asserts validate_run_state's
# returned severities equal these; it does not re-derive classification logic.
_SEVERITY = {
    "interrupted_with_active_handoff": "warning",
    "terminal_with_stale_handoff": "warning",
    "active_handoff_without_decision": "info",
    "halt_decision_without_halted_meta": "error",
    "meta_handoff_without_event": "error",
}


def _start(seq: int = 1) -> dict:
    return run_event("run.start", seq=seq, run_kind="single_project", task="t")


# ── A. reducer reachability: events drive the projected status/terminal ────


@pytest.mark.parametrize(
    ("events", "expected_status", "expected_terminal"),
    [
        pytest.param([], RunStatus.UNKNOWN, False, id="unknown_seed"),
        pytest.param([_start()], RunStatus.RUNNING, False, id="running"),
        pytest.param(
            [_start(), handoff_event("h1", "validate_plan", 2)],
            RunStatus.AWAITING_PHASE_HANDOFF,
            False,
            id="awaiting_phase_handoff",
        ),
        pytest.param(
            [_start(), run_event("run.end", seq=2, status="done")],
            RunStatus.DONE,
            True,
            id="done",
        ),
        pytest.param(
            [_start(), run_event("run.end", seq=2, status="failed")],
            RunStatus.FAILED,
            True,
            id="failed",
        ),
        pytest.param(
            [_start(), run_event("run.end", seq=2, status="halted")],
            RunStatus.HALTED,
            True,
            id="halted_via_run_end",
        ),
        pytest.param(
            [_start(), run_event("run.halted", seq=2)],
            RunStatus.HALTED,
            True,
            id="halted_via_event",
        ),
        pytest.param(
            [_start(), run_event("run.interrupted", seq=2)],
            RunStatus.INTERRUPTED,
            False,
            id="interrupted",
        ),
        pytest.param(
            [
                _start(),
                handoff_event("h1", "validate_plan", 2),
                run_event(
                    "phase_handoff.decided", seq=3, action="halt", handoff_id="h1"
                ),
            ],
            RunStatus.HALTED,
            True,
            id="halted_via_decided",
        ),
    ],
)
def test_projection_reaches_status(
    tmp_path: Path,
    events: list[dict],
    expected_status: RunStatus,
    expected_terminal: bool,
) -> None:
    """The reducer projects the expected status/terminal flag from the stream.

    ``AWAITING_GATE_DECISION`` / ``AWAITING_HUMAN_REVIEW`` / ``CANCELLED`` are
    intentionally absent here: no current event reaches them (they appear only
    as durable ``meta.status`` — see :func:`test_meta_status_clean_is_ok`).
    """
    write_events(tmp_path, events)
    snap = project_run_dir(tmp_path)
    assert snap.status is expected_status
    assert snap.terminal is expected_terminal


# ── B. every RunStatus value flows cleanly through validate_run_state ──────


@pytest.mark.parametrize("status", list(RunStatus), ids=lambda s: s.value)
def test_meta_status_clean_is_ok(tmp_path: Path, status: RunStatus) -> None:
    """A clean run (no active handoff, no decisions) is ok for every status.

    Covers all ten ``RunStatus`` values as durable ``meta.status`` — including
    the three the reducer never reaches (``awaiting_gate_decision``,
    ``awaiting_human_review``, ``cancelled``) and the ``unknown`` seed. With no
    active handoff and no halt decision, none of the five codes can fire.
    """
    write_events(tmp_path, [_start()])
    write_meta(tmp_path, {"status": status.value})

    report = validate_run_state(tmp_path)

    assert report.issues == ()
    assert report.ok
    assert report.meta_status == status.value


# ── C. invariant matrix: handoff/decision/halt_reason × torn shapes ────────


@dataclass(frozen=True, slots=True)
class Case:
    """One row of the state matrix.

    ``expected`` maps each code ``validate_run_state`` should emit to its
    expected severity. ``ok`` is derived (false iff any expected severity is
    ``error``), so a row cannot silently disagree with the contract.
    """

    id: str
    events: list[dict]
    meta: dict
    decisions: list[tuple[str, dict]] = field(default_factory=list)
    expected: dict[str, str] = field(default_factory=dict)


_H1 = "h1"
_HANDOFF_EVENTS = [_start(), handoff_event(_H1, "validate_plan", 2)]
_CONTINUE_DECISION = ("001", {"action": "continue", "handoff_id": _H1})
_HALT_DECISION = (
    "001",
    {"action": "halt", "handoff_id": _H1, "decided_at": "2026-06-08T00:00:00Z"},
)

_CASES = [
    Case(
        id="running_active_no_decision",
        events=_HANDOFF_EVENTS,
        meta={"status": "running", "phase_handoff": {"id": _H1}},
        expected={"active_handoff_without_decision": "info"},
    ),
    Case(
        id="running_active_with_decision",
        events=_HANDOFF_EVENTS,
        meta={"status": "running", "phase_handoff": {"id": _H1}},
        decisions=[_CONTINUE_DECISION],
        expected={},
    ),
    Case(
        id="halted_stale_handoff_with_halt_decision",
        events=_HANDOFF_EVENTS,
        meta={
            "status": "halted",
            "halt_reason": "phase_handoff_halt",
            "phase_handoff": {"id": _H1},
        },
        decisions=[_HALT_DECISION],
        expected={"terminal_with_stale_handoff": "warning"},
    ),
    Case(
        id="done_stale_handoff_with_decision",
        events=_HANDOFF_EVENTS,
        meta={"status": "done", "phase_handoff": {"id": _H1}},
        decisions=[_CONTINUE_DECISION],
        expected={"terminal_with_stale_handoff": "warning"},
    ),
    Case(
        id="done_stale_handoff_no_decision",
        events=_HANDOFF_EVENTS,
        meta={"status": "done", "phase_handoff": {"id": _H1}},
        expected={
            "terminal_with_stale_handoff": "warning",
            "active_handoff_without_decision": "info",
        },
    ),
    Case(
        id="torn_halt_decision_without_halted_meta",
        events=_HANDOFF_EVENTS,
        meta={"status": "running"},
        decisions=[_HALT_DECISION],
        expected={"halt_decision_without_halted_meta": "error"},
    ),
    Case(
        id="interrupted_active_with_decision",
        events=_HANDOFF_EVENTS,
        meta={
            "status": "interrupted",
            "halt_reason": "interrupted",
            "phase_handoff": {"id": _H1},
        },
        decisions=[_CONTINUE_DECISION],
        expected={"interrupted_with_active_handoff": "warning"},
    ),
    Case(
        id="interrupted_active_no_decision",
        events=_HANDOFF_EVENTS,
        meta={"status": "interrupted", "phase_handoff": {"id": _H1}},
        expected={
            "interrupted_with_active_handoff": "warning",
            "active_handoff_without_decision": "info",
        },
    ),
    Case(
        id="meta_handoff_without_event",
        events=[_start()],
        meta={"status": "running", "phase_handoff": {"id": "ghost"}},
        expected={
            "meta_handoff_without_event": "error",
            "active_handoff_without_decision": "info",
        },
    ),
    Case(
        id="valid_done_no_handoff",
        events=[
            _start(),
            run_event("phase.end", seq=2, phase="PLAN", title="PLAN", outcome="ok"),
            run_event("run.end", seq=3, status="done"),
        ],
        meta={"status": "done"},
        expected={},
    ),
    Case(
        id="valid_halted_no_handoff",
        events=[_start(), run_event("run.end", seq=2, status="halted")],
        meta={
            "status": "halted",
            "halt_reason": "phase_handoff_halt",
            "halted_at": "2026-06-08T00:00:00Z",
        },
        expected={},
    ),
    Case(
        id="valid_failed_no_handoff",
        events=[_start(), run_event("run.end", seq=2, status="failed")],
        meta={"status": "failed", "halt_reason": "boom"},
        expected={},
    ),
]


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c.id)
def test_state_matrix_classification(tmp_path: Path, case: Case) -> None:
    """Each shape classifies to exactly the expected codes/severities/ok flag."""
    write_events(tmp_path, case.events)
    write_meta(tmp_path, case.meta)
    for name, decision in case.decisions:
        write_decision(tmp_path, name, decision)

    report = validate_run_state(tmp_path)

    actual = {issue.code: issue.severity for issue in report.issues}
    assert actual == case.expected
    # Every emitted severity must match the consistency contract.
    for code, severity in actual.items():
        assert severity == _SEVERITY[code]
    expected_ok = not any(sev == "error" for sev in case.expected.values())
    assert report.ok is expected_ok
    assert report.meta_status == case.meta["status"]


# ── halt_reason is classification-neutral (present vs absent) ──────────────


@pytest.mark.parametrize("with_halt_reason", [True, False], ids=["with", "without"])
def test_halt_reason_presence_is_classification_neutral(
    tmp_path: Path, with_halt_reason: bool
) -> None:
    """``halt_reason`` is never read by consistency — it cannot change codes.

    A halted run carrying a stale handoff classifies to exactly
    ``terminal_with_stale_handoff`` whether or not ``halt_reason`` is present.
    """
    meta: dict = {"status": "halted", "phase_handoff": {"id": _H1}}
    if with_halt_reason:
        meta["halt_reason"] = "phase_handoff_halt"
    write_events(tmp_path, _HANDOFF_EVENTS)
    write_meta(tmp_path, meta)
    write_decision(tmp_path, *_HALT_DECISION)

    report = validate_run_state(tmp_path)

    assert {i.code for i in report.issues} == {"terminal_with_stale_handoff"}
    assert report.ok
