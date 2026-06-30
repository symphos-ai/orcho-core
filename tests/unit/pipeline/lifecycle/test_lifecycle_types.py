"""lifecycle FSM type stubs.

The FSM logic itself arrives in; this only pins the
typed StepOutcome contract so downstream phases ( HumanReview,
 quality gates) build on stable types.
"""
from types import SimpleNamespace

import pytest

from pipeline.lifecycle import StepOutcome, StepStatus


class TestStepStatus:
    def test_complete_set(self) -> None:
        assert {s.value for s in StepStatus} == {
            "completed", "skipped", "retry_requested", "halted", "failed",
        }


class TestStepOutcome:
    def test_completed_minimal(self) -> None:
        out = StepOutcome(status=StepStatus.COMPLETED, state=SimpleNamespace())
        assert out.reason is None
        assert out.retry_payload is None

    def test_halted_requires_reason(self) -> None:
        with pytest.raises(ValueError, match="requires reason"):
            StepOutcome(status=StepStatus.HALTED, state=SimpleNamespace())

    def test_skipped_requires_reason(self) -> None:
        with pytest.raises(ValueError, match="requires reason"):
            StepOutcome(status=StepStatus.SKIPPED, state=SimpleNamespace())

    def test_failed_requires_reason(self) -> None:
        with pytest.raises(ValueError, match="requires reason"):
            StepOutcome(status=StepStatus.FAILED, state=SimpleNamespace())

    def test_retry_requested_requires_payload(self) -> None:
        with pytest.raises(ValueError, match="requires retry_payload"):
            StepOutcome(status=StepStatus.RETRY_REQUESTED, state=SimpleNamespace())

    def test_retry_requested_with_payload(self) -> None:
        out = StepOutcome(
            status=StepStatus.RETRY_REQUESTED,
            state=SimpleNamespace(),
            retry_payload={"loop_round_delta": 1, "trigger": "human"},
        )
        assert out.retry_payload["trigger"] == "human"
