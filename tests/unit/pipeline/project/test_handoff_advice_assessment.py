from pipeline.project.handoff_advice import HandoffAdvice
from pipeline.project.handoff_advice_assessment import assess_advice
from pipeline.project.handoff_advice_contract import AdviceContractSnapshot, ContractInvariant
from pipeline.project.handoff_advice_intent import AdviceIntent, ContractEffect, ProposedOperation


def _snapshot(plan=True):
    return AdviceContractSnapshot("task", "digest", plan, acceptance_criteria=(ContractInvariant("acceptance:1", "done"),))


def _advice(*, effects=None, confidence="high", action="retry_feedback"):
    effects = effects if effects is not None else (ContractEffect("acceptance:1", "advance", {}),)
    return HandoffAdvice(recommended_action=action, confidence=confidence, rationale="x", retry_feedback="fix", intent=AdviceIntent((ProposedOperation("repair", "x", {}),), effects))


def test_dispositions_and_precedence() -> None:
    assert assess_advice(_advice(), _snapshot(), scope=frozenset()).disposition == "safe"
    assert assess_advice(_advice(effects=(ContractEffect("acceptance:1", "violate", {}),)), _snapshot()).disposition == "contract_conflict"
    boundary = HandoffAdvice(recommended_action="retry_feedback", confidence="high", rationale="x", retry_feedback="fix", intent=AdviceIntent((ProposedOperation("remove", "correction_context", {}),), _advice().intent.contract_effects))
    assert assess_advice(boundary, AdviceContractSnapshot("task", "digest", True, acceptance_criteria=(ContractInvariant("acceptance:1", "done"),), correction_context="must preserve")).blocked_reason == "correction_boundary_conflict"
    assert assess_advice(_advice(effects=()), _snapshot()).disposition == "operator_review_required"
    assert assess_advice(_advice(), _snapshot(False)).blocked_reason == "parsed_plan_unavailable"
    assert assess_advice(_advice(confidence="low"), _snapshot()).disposition == "operator_review_required"
    assert assess_advice(_advice(action="continue_with_waiver"), _snapshot()).disposition == "policy_blocked"


def test_policy_gates_follow_intent_precedence() -> None:
    assert assess_advice(_advice(), _snapshot(), budget_remaining=0).blocked_reason == "budget_exhausted"
    assert assess_advice(_advice(), _snapshot(), findings=({"severity": "P1"},), repeated=True).blocked_reason == "repeated_finding"
    assert assess_advice(_advice(), _snapshot(), scope=frozenset({"a.py"})).disposition == "safe"
    destructive = HandoffAdvice(recommended_action="retry_feedback", confidence="high", rationale="x", retry_feedback="git reset --hard", intent=_advice().intent)
    assert assess_advice(destructive, _snapshot()).blocked_reason == "destructive_action"
