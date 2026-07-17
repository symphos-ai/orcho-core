"""Provider-native runtime scope projection tests."""

from __future__ import annotations

from types import SimpleNamespace

from agents.registry import PhaseAgentConfig
from pipeline.project.runtime_setup import setup_runtime
from pipeline.skills import (
    SkillTrustPolicy,
    configure_agent_skill_scope,
    configure_phase_agent_skill_scope,
)


class _ConfigurableAgent:
    runtime = "codex"

    def __init__(self, model: str = "gpt-test") -> None:
        self.model = model
        self.user_scope: list[bool] = []

    def configure_skill_scope(self, *, include_user_skills: bool) -> None:
        self.user_scope.append(include_user_skills)


def test_configure_agent_uses_effective_user_trust() -> None:
    agent = _ConfigurableAgent()

    returned = configure_agent_skill_scope(
        agent,
        SkillTrustPolicy(trust_user=True),
    )

    assert returned is agent
    assert agent.user_scope == [True]


def test_phase_scope_configures_shared_agent_once() -> None:
    shared = _ConfigurableAgent()
    other = _ConfigurableAgent()
    phase_config = SimpleNamespace(
        plan_agent=shared,
        validate_plan_agent=shared,
        implement_agent=other,
        review_changes_agent=shared,
        repair_changes_agent=shared,
        repair_escalation_agent=shared,
        final_acceptance_agent=shared,
    )

    configure_phase_agent_skill_scope(phase_config, SkillTrustPolicy())

    assert shared.user_scope == [False]
    assert other.user_scope == [False]


def test_runtime_without_scope_capability_is_unchanged() -> None:
    agent = object()

    assert configure_agent_skill_scope(agent, SkillTrustPolicy()) is agent


def test_project_runtime_threads_scope_to_phase_and_subtask_agents() -> None:
    phase_agent = _ConfigurableAgent()
    phase_config = PhaseAgentConfig(
        plan_agent=phase_agent,
        validate_plan_agent=phase_agent,
        implement_agent=phase_agent,
        review_changes_agent=phase_agent,
        repair_changes_agent=phase_agent,
        repair_escalation_agent=phase_agent,
        final_acceptance_agent=phase_agent,
    )

    class _Provider:
        def resolve(self, runtime, model, *, effort=None):
            return _ConfigurableAgent(model)

    setup = setup_runtime(
        phase_config=phase_config,
        provider=_Provider(),
        model="gpt-test",
        skill_trust=SkillTrustPolicy(trust_user=True),
    )

    assert phase_agent.user_scope == [True]
    subtask_agent = setup.agent_registry.resolve("gpt-subtask", "codex")
    assert subtask_agent.user_scope == [True]
