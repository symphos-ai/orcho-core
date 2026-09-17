"""Conservative delivery applicability from the full recipe and completed facts.

This module owns classification, not Git reads or verification receipt policy.
An unknown recipe or incomplete run remains subject to the ordinary delivery gate.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pipeline.runtime import LoopStep, PhaseStep, Profile


def completed_plan_only(profile: object, session: Mapping[str, Any]) -> bool:
    """Recognize the proven plan/approval recipe, never a profile name."""
    if not isinstance(profile, Profile):
        return False
    phases = []
    for step in profile.steps:
        steps = step.steps if isinstance(step, LoopStep) else (step,)
        for inner in steps:
            if not isinstance(inner, PhaseStep):
                return False
            phases.append(inner.phase)
    if phases != ["plan", "validate_plan"]:
        return False
    if session.get("status") != "done" or session.get("phase_handoff"):
        return False
    facts = session.get("phases")
    if not isinstance(facts, Mapping):
        return False
    if any(value for key, value in facts.items() if key not in {"plan", "validate_plan"}):
        return False
    plans = facts.get("plan")
    reviews = facts.get("validate_plan")
    return bool(
        isinstance(plans, list) and plans
        and isinstance(plans[-1], Mapping)
        and isinstance(plans[-1].get("total_atomic_tasks"), int)
        and plans[-1]["total_atomic_tasks"] > 0
        and isinstance(reviews, list) and reviews
        and isinstance(reviews[-1], Mapping)
        and reviews[-1].get("approved") is True
        and reviews[-1].get("verdict") == "APPROVED"
    )


def absent_delivery_subject(patch: str, untracked: tuple[str, ...] | None) -> bool:
    """Unknown Git reads are not proof of absence."""
    return untracked == () and (not patch.strip() or patch == "(no diff)")
