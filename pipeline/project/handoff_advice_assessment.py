# SPDX-License-Identifier: Apache-2.0
"""Single authoritative disposition for handoff-advice automation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pipeline.project.handoff_advice_intent import EFFECT_KINDS
from pipeline.project.handoff_advice_policy import policy_block_reason

if TYPE_CHECKING:
    from pipeline.project.handoff_advice import HandoffAdvice
    from pipeline.project.handoff_advice_contract import AdviceContractSnapshot

Disposition = Literal["safe", "contract_conflict", "operator_review_required", "policy_blocked"]


@dataclass(frozen=True, slots=True)
class AdviceAssessment:
    disposition: Disposition
    blocked_reason: str = ""
    conflict_details: tuple[str, ...] = ()
    scope_unchecked: bool = False

    @property
    def auto_apply_ok(self) -> bool:
        return self.disposition == "safe"

    @property
    def proceed(self) -> bool:
        return self.auto_apply_ok

    @property
    def reason(self) -> str:
        return self.blocked_reason

    @property
    def stop_state(self) -> str:
        return {"halt": "halt", "budget_exhausted": "budget_exhausted", "repeated_finding": "repeated_finding"}.get(self.blocked_reason, "needs_operator")


def assess_advice(
    advice: HandoffAdvice,
    snapshot: AdviceContractSnapshot | None,
    *,
    findings: object = (),
    scope: frozenset[str] = frozenset(),
    budget_remaining: int = 1,
    repeated: bool = False,
) -> AdviceAssessment:
    """Classify advice once, in conflict → ambiguity → policy → safe order."""
    if snapshot is None or not snapshot.parsed_plan_available:
        return AdviceAssessment("operator_review_required", "parsed_plan_unavailable")
    intent = advice.intent
    expected = tuple(item.id for item in snapshot.acceptance_criteria) + tuple(
        item.id for task in snapshot.subtasks for item in task.done_criteria
    )
    effects = intent.contract_effects
    violations = tuple(effect.invariant_id for effect in effects if effect.effect == "violate")
    if violations:
        return AdviceAssessment("contract_conflict", "explicit_violate", violations)
    boundary_conflicts = tuple(
        operation.target
        for operation in intent.proposed_operations
        if snapshot.correction_context
        and operation.target == "correction_context"
        and operation.kind in {"revert", "remove"}
    )
    if boundary_conflicts:
        return AdviceAssessment("contract_conflict", "correction_boundary_conflict", boundary_conflicts)
    ids = tuple(effect.invariant_id for effect in effects)
    duplicates = tuple(sorted({item for item in ids if item and ids.count(item) > 1}))
    unknown = tuple(item for item in ids if item not in expected)
    missing = tuple(item for item in expected if item not in ids)
    malformed = tuple(intent.diagnostics) + tuple(
        f"unknown_effect:{effect.effect}" for effect in effects if effect.effect not in EFFECT_KINDS
    )
    if malformed or duplicates or unknown or missing or advice.confidence == "low":
        details = malformed + duplicates + unknown + missing
        reason = "low_confidence_ambiguity" if advice.confidence == "low" and not details else "intent_coverage_ambiguous"
        return AdviceAssessment("operator_review_required", reason, details)
    reason, scope_unchecked = policy_block_reason(
        advice, findings=findings, scope=scope, budget_remaining=budget_remaining, repeated=repeated,
    )
    if reason:
        return AdviceAssessment("policy_blocked", reason, scope_unchecked=scope_unchecked)
    return AdviceAssessment("safe", scope_unchecked=scope_unchecked)


__all__ = ["AdviceAssessment", "Disposition", "assess_advice"]
