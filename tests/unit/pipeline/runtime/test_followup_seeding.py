"""Follow-up session seed wiring tests."""

from __future__ import annotations

from agents.registry import PhaseAgentConfig
from pipeline.project.profile_dispatch import (
    apply_followup_session_seeds as _apply_followup_session_seeds,
)


class FakeAgent:
    def __init__(self) -> None:
        self.model = "fake"
        self.session_id: str | None = None
        self._followup_resume_pending = False
        self._last_resumed_session_id: str | None = None
        self._last_followup_parent_session_id: str | None = None

    def invoke(self, prompt: str, cwd: str, **kwargs) -> str:  # noqa: ARG002
        return "ok"

    def reset_session(self) -> None:
        self.session_id = None


def _phase_config() -> PhaseAgentConfig:
    return PhaseAgentConfig(
        plan_agent=FakeAgent(),
        validate_plan_agent=FakeAgent(),
        implement_agent=FakeAgent(),
        review_changes_agent=FakeAgent(),
        repair_changes_agent=FakeAgent(),
        repair_escalation_agent=FakeAgent(),
        final_acceptance_agent=FakeAgent(),
    )


def test_apply_followup_session_seeds_sets_each_present_slot() -> None:
    cfg = _phase_config()
    count = _apply_followup_session_seeds(
        cfg,
        {
            "plan": "plan-sid",
            "validate_plan": "validate-sid",
            "implement": "implement-sid",
            "review_changes": "review-sid",
            "repair_changes": "repair-sid",
            "final_acceptance": "final-sid",
        },
    )

    assert count == 6
    assert cfg.plan_agent.session_id == "plan-sid"
    assert cfg.validate_plan_agent.session_id == "validate-sid"
    assert cfg.implement_agent.session_id == "implement-sid"
    assert cfg.review_changes_agent.session_id == "review-sid"
    assert cfg.repair_changes_agent.session_id == "repair-sid"
    assert cfg.final_acceptance_agent.session_id == "final-sid"
    assert cfg.plan_agent._followup_resume_pending is True
    assert cfg.final_acceptance_agent._followup_resume_pending is True


def test_missing_seed_leaves_agent_untouched() -> None:
    cfg = _phase_config()
    count = _apply_followup_session_seeds(cfg, {"implement": "implement-sid"})

    assert count == 1
    assert cfg.implement_agent.session_id == "implement-sid"
    assert cfg.plan_agent.session_id is None
    assert cfg.plan_agent._followup_resume_pending is False


def test_missing_agent_slot_is_ignored() -> None:
    cfg = _phase_config()
    cfg.repair_changes_agent = None  # type: ignore[assignment]

    count = _apply_followup_session_seeds(cfg, {"repair_changes": "repair-sid"})

    assert count == 0
