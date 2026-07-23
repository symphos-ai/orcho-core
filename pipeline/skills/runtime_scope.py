"""Project Orcho skill trust onto provider-native runtime discovery."""

from __future__ import annotations

from typing import Any

from pipeline.skills.types import SkillTrustPolicy

_PHASE_AGENT_ATTRS = (
    "plan_agent",
    "validate_plan_agent",
    "implement_agent",
    "review_changes_agent",
    "repair_changes_agent",
    "repair_escalation_agent",
    "final_acceptance_agent",
)


def configure_agent_skill_scope(
    agent: Any,
    policy: SkillTrustPolicy,
) -> Any:
    """Apply effective source scope when a runtime exposes the capability."""
    configure = getattr(agent, "configure_skill_scope", None)
    if callable(configure):
        configure(include_user_skills=policy.trust_user)
    return agent


def configure_phase_agent_skill_scope(
    phase_config: Any,
    policy: SkillTrustPolicy,
) -> None:
    """Configure every distinct concrete agent in a phase config."""
    seen: set[int] = set()
    for attr in _PHASE_AGENT_ATTRS:
        agent = getattr(phase_config, attr, None)
        if agent is None or id(agent) in seen:
            continue
        seen.add(id(agent))
        configure_agent_skill_scope(agent, policy)
