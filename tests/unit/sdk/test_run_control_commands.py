"""Unit tests for :mod:`sdk.run_control.commands`.

Pins that ``build_decision_command``:

* builds a valid command from a phase-handoff ``PendingOperatorAction``;
* rejects actions outside ``available_actions``;
* rejects empty feedback for feedback-required actions (reusing the
  ``sdk.phase_handoff`` rule, not a local copy);
* rejects gate pauses with a clear out-of-scope error;
* never executes the decision (no ``phase_handoff_decide`` call, no disk
  write);
* produces ``to_decide_kwargs`` that bind cleanly to the
  ``phase_handoff_decide`` signature.

Pure: synthetic ``PendingOperatorAction`` values, no filesystem.
"""
from __future__ import annotations

import inspect

import pytest

import sdk.phase_handoff as ph
from sdk.run_control.commands import build_decision_command
from sdk.run_control.types import PendingOperatorAction, PhaseHandoffDecisionCommand

# ── helpers ──────────────────────────────────────────────────────────────────

_ALL_ACTIONS = ("continue", "retry_feedback", "halt", "continue_with_waiver")


def _handoff_pending(
    *,
    available: tuple[str, ...] = _ALL_ACTIONS,
    handoff_id: str | None = "implement:r1",
) -> PendingOperatorAction:
    return PendingOperatorAction(
        run_id="run-1",
        kind="phase_handoff",
        handoff_id=handoff_id,
        phase="implement",
        available_actions=available,
    )


def _gate_pending() -> PendingOperatorAction:
    return PendingOperatorAction(
        run_id="run-1",
        kind="gate",
        raw={"name": "review_gate", "choices": ["run", "skip"]},
    )


# ── happy path ───────────────────────────────────────────────────────────────


class TestBuildValid:
    def test_builds_command_for_continue(self) -> None:
        cmd = build_decision_command(_handoff_pending(), "continue")
        assert isinstance(cmd, PhaseHandoffDecisionCommand)
        assert cmd.run_id == "run-1"
        assert cmd.handoff_id == "implement:r1"
        assert cmd.action == "continue"
        assert cmd.feedback is None
        assert cmd.note is None

    def test_builds_command_with_feedback_and_note(self) -> None:
        cmd = build_decision_command(
            _handoff_pending(), "retry_feedback", feedback="try again", note="n",
        )
        assert cmd.action == "retry_feedback"
        assert cmd.feedback == "try again"
        assert cmd.note == "n"

    def test_to_decide_kwargs_binds_to_decide_signature(self) -> None:
        cmd = build_decision_command(
            _handoff_pending(), "continue_with_waiver", feedback="waived",
        )
        kwargs = cmd.to_decide_kwargs()
        # The kwargs must bind cleanly to the real executor signature.
        sig = inspect.signature(ph.phase_handoff_decide)
        bound = sig.bind(**kwargs)  # raises TypeError if incompatible
        assert bound.arguments["run_id"] == "run-1"
        assert bound.arguments["action"] == "continue_with_waiver"


# ── validation ───────────────────────────────────────────────────────────────


class TestValidation:
    def test_action_outside_available_actions_rejected(self) -> None:
        pending = _handoff_pending(available=("continue", "halt"))
        with pytest.raises(ValueError, match="available_actions"):
            build_decision_command(pending, "retry_feedback")

    def test_retry_feedback_requires_feedback(self) -> None:
        with pytest.raises(ValueError):
            build_decision_command(_handoff_pending(), "retry_feedback")
        with pytest.raises(ValueError):
            build_decision_command(_handoff_pending(), "retry_feedback", feedback="   ")

    def test_continue_with_waiver_requires_feedback(self) -> None:
        with pytest.raises(ValueError):
            build_decision_command(_handoff_pending(), "continue_with_waiver")

    def test_continue_and_halt_do_not_require_feedback(self) -> None:
        # Behavioural parity with sdk.phase_handoff's feedback-required set:
        # only retry_feedback / continue_with_waiver need feedback.
        assert build_decision_command(_handoff_pending(), "continue").feedback is None
        assert build_decision_command(_handoff_pending(), "halt").feedback is None

    def test_missing_handoff_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="handoff_id"):
            build_decision_command(_handoff_pending(handoff_id=None), "continue")


# ── gate boundary ────────────────────────────────────────────────────────────


class TestGateBoundary:
    def test_gate_pending_rejected_with_clear_error(self) -> None:
        with pytest.raises(ValueError, match="gate"):
            build_decision_command(_gate_pending(), "continue")


# ── no execution / no disk write ─────────────────────────────────────────────


class TestNoExecution:
    def test_build_never_calls_phase_handoff_decide(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = {"hit": False}

        def _boom(*args, **kwargs):  # pragma: no cover - must never run
            called["hit"] = True
            raise AssertionError("phase_handoff_decide must not be called")

        monkeypatch.setattr(ph, "phase_handoff_decide", _boom)
        # Building several commands (including feedback-required) must not
        # execute the decision nor touch the executor.
        build_decision_command(_handoff_pending(), "continue")
        build_decision_command(_handoff_pending(), "retry_feedback", feedback="f")
        assert called["hit"] is False

    def test_command_module_neither_imports_nor_calls_decide(self) -> None:
        import sdk.run_control.commands as commands_mod

        # Not bound in the module namespace (not imported).
        assert not hasattr(commands_mod, "phase_handoff_decide")
        # No call site in the source (docstrings may name it to document
        # the boundary, but there must be no invocation).
        source = inspect.getsource(commands_mod)
        assert "phase_handoff_decide(" not in source
