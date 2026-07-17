"""Reconcile a pending implement handoff with post-phase gate repair."""

from __future__ import annotations

from types import SimpleNamespace

from pipeline.project import gate_repair
from pipeline.project.pending_handoff_reconciliation import (
    reconcile_pending_handoff_after_gates,
)
from pipeline.runtime.handoff import PhaseHandoffRequested
from pipeline.runtime.roles import PhaseHandoffType


def _signal(handoff_id: str = "implement:implement_handoff:1") -> PhaseHandoffRequested:
    return PhaseHandoffRequested(
        handoff_id=handoff_id,
        phase="implement",
        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
        trigger="incomplete",
        verdict="INCOMPLETE",
        approved=False,
        round_extras_key="implement_handoff",
        round=1,
        loop_max_rounds=1,
        available_actions=("retry_feedback", "continue_with_waiver", "halt"),
        artifacts={
            "incomplete_subtasks": ["T2-route"],
            "attestation_incomplete": {"T2-route": "criterion 2 unverified"},
        },
    )


def test_fail_then_pass_annotates_separate_pending_handoff() -> None:
    original = _signal()
    state = SimpleNamespace(phase_handoff_request=original)
    result = reconcile_pending_handoff_after_gates(
        state,
        previous_signal=original,
        gate_events=[
            {"command": "cs", "decision": "executed_fail"},
            {"command": "cs", "decision": "executed_pass"},
            {"command": "rector", "decision": "executed_pass"},
        ],
    )

    assert result is not None
    assert result.repaired_commands == ("cs",)
    assert state.phase_handoff_request is result.signal
    assert result.signal.artifacts["post_phase_gate_repair"] == {
        "status": "passed",
        "commands": ["cs"],
        "handoff_cause": "separate_remaining_blocker",
    }


def test_new_gate_handoff_supersedes_old_incomplete_handoff() -> None:
    original = _signal()
    replacement = _signal("gate:cs:1")
    state = SimpleNamespace(phase_handoff_request=replacement)
    result = reconcile_pending_handoff_after_gates(
        state,
        previous_signal=original,
        gate_events=[
            {"command": "cs", "decision": "executed_fail"},
        ],
    )

    assert result is None
    assert state.phase_handoff_request is replacement


def test_pass_without_repair_does_not_add_causal_noise() -> None:
    original = _signal()
    state = SimpleNamespace(phase_handoff_request=original)
    result = reconcile_pending_handoff_after_gates(
        state,
        previous_signal=original,
        gate_events=[
            {"command": "cs", "decision": "executed_pass"},
        ],
    )

    assert result is None
    assert state.phase_handoff_request is original


def test_post_phase_hook_reconciles_ledger_delta_after_gate_repair(
    monkeypatch,
) -> None:
    original = _signal()
    state = SimpleNamespace(
        extras={"verification_contract": object()},
        phase_handoff_request=original,
    )
    run = SimpleNamespace(
        state=state,
        _gate_profile=object(),
        _gate_ctx=object(),
        _in_gate_hook=False,
    )
    events: list[SimpleNamespace] = []

    def fake_gate_hook(*_args, **_kwargs) -> gate_repair.GateRepairOutcome:
        events.extend([
            SimpleNamespace(command="cs", kind="execution", outcome="fail"),
            SimpleNamespace(command="cs", kind="execution", outcome="pass"),
        ])
        return gate_repair.GateRepairOutcome(active=True, passed=True, rounds=1)

    monkeypatch.setattr(gate_repair, "run_gate_hook", fake_gate_hook)
    monkeypatch.setattr(
        "pipeline.project.verification_ledger_runtime.live_delta",
        lambda _run: tuple(events),
    )

    gate_repair.evaluate_post_phase_gates(run, "implement")

    assert state.phase_handoff_request is not original
    assert state.phase_handoff_request.artifacts["post_phase_gate_repair"] == {
        "status": "passed",
        "commands": ["cs"],
        "handoff_cause": "separate_remaining_blocker",
    }
    assert run._in_gate_hook is False
