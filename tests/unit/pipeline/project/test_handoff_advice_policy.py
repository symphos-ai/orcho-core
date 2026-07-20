"""CI handoff-advice policy gates (T1).

Covers the pure policy surface in
``pipeline.project.handoff_advice_policy``: mode resolution + budget, the
write-scope builder from ``parsed_plan``, the auditable destructive classifier,
the per-gate ``proceed | stop(reason, state)`` decision, the findings
fingerprint, and the extended ``ci_agent`` provenance note. No provider is
invoked and no decision artifact is written — the module is a pure policy.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from agents.entities import SubTask
from pipeline.plan_parser import ParsedPlan
from pipeline.project.handoff_advice import HandoffAdvice
from pipeline.project.handoff_advice_artifact import build_provenance_note
from pipeline.project.handoff_advice_assessment import assess_advice
from pipeline.project.handoff_advice_contract import AdviceContractSnapshot
from pipeline.project.handoff_advice_intent import AdviceIntent, ProposedOperation
from pipeline.project.handoff_advice_policy import (
    HandoffAdvicePolicy,
    build_scope,
    findings_fingerprint,
    is_destructive_recommendation,
    resolve_handoff_advice_policy,
)

# ── helpers ────────────────────────────────────────────────────────────────


def _advice(
    *,
    action: str = "retry_feedback",
    confidence: str = "high",
    retry_feedback: str = "Add a regression test for edge case A and re-run pytest.",
    risks: tuple[str, ...] = (),
    expected_files: tuple[str, ...] = (),
    operator_note: str = "",
) -> HandoffAdvice:
    return HandoffAdvice(
        recommended_action=action,  # type: ignore[arg-type]
        confidence=confidence,  # type: ignore[arg-type]
        rationale="because",
        retry_feedback=retry_feedback,
        risks=risks,
        expected_files=expected_files,
        operator_note=operator_note,
        intent=AdviceIntent(proposed_operations=(ProposedOperation("repair", "finding", {}),)),
    )


def _finding(fid: str, severity: str, title: str = "t") -> dict[str, str]:
    return {"id": fid, "severity": severity, "title": title}


def _gate(advice: HandoffAdvice, *, findings=(), scope=frozenset(),
          budget_remaining=1, repeated=False):
    return assess_advice(
        advice, AdviceContractSnapshot("", "", True), findings=findings,
        scope=scope, budget_remaining=budget_remaining, repeated=repeated,
    )


# ── resolve_handoff_advice_policy ───────────────────────────────────────────


def test_resolve_interactive_no_auto_action():
    policy = resolve_handoff_advice_policy(SimpleNamespace(no_interactive=False))
    assert policy.auto_retry_with_agent is False


def test_resolve_non_interactive_auto_retry_default_budget():
    policy = resolve_handoff_advice_policy(SimpleNamespace(no_interactive=True))
    assert policy.auto_retry_with_agent is True
    assert policy.max_agent_retries == 1


def test_resolve_accepts_raw_bool():
    assert resolve_handoff_advice_policy(True).auto_retry_with_agent is True
    assert resolve_handoff_advice_policy(False).auto_retry_with_agent is False


def test_max_agent_retries_is_overridable_and_widens_budget_gate():
    # Default budget=1 → a second retry (remaining 0) stops.
    policy = HandoffAdvicePolicy(auto_retry_with_agent=True)
    advice = _advice()
    remaining_after_one = policy.max_agent_retries - 1
    assert _gate(advice, budget_remaining=remaining_after_one).proceed is False
    # Overridden budget=2 → the second retry (remaining 1) proceeds.
    widened = replace(policy, max_agent_retries=2)
    remaining_after_one = widened.max_agent_retries - 1
    assert _gate(advice, budget_remaining=remaining_after_one).proceed is True


# ── build_scope ─────────────────────────────────────────────────────────────


def test_build_scope_unions_plan_and_subtask_globs():
    plan = ParsedPlan(
        subtasks=(SubTask(id="t1", goal="g", allowed_modifications=("b/*",)),),
        source="json",
        owned_files=("a.py",),
    )
    scope = build_scope(SimpleNamespace(parsed_plan=plan))
    assert scope == frozenset({"a.py", "b/*"})


def test_build_scope_none_plan_is_unlimited_marker():
    assert build_scope(SimpleNamespace(parsed_plan=None)) == frozenset()


def test_build_scope_empty_plan_is_unlimited_marker():
    plan = ParsedPlan(subtasks=(), source="json")
    assert build_scope(SimpleNamespace(parsed_plan=plan)) == frozenset()


# ── is_destructive_recommendation ───────────────────────────────────────────


def test_destructive_marker_in_retry_feedback():
    advice = _advice(retry_feedback="Run git reset --hard to drop the changes.")
    assert is_destructive_recommendation(advice) is True


def test_destructive_marker_in_risks():
    advice = _advice(risks=("This could rm -rf the working tree.",))
    assert is_destructive_recommendation(advice) is True


def test_destructive_marker_in_operator_note():
    advice = _advice(operator_note="A force push may be needed afterwards.")
    assert is_destructive_recommendation(advice) is True


def test_destructive_case_insensitive():
    advice = _advice(retry_feedback="GIT PUSH --FORCE to origin.")
    assert is_destructive_recommendation(advice) is True


def test_non_destructive_corrective_feedback():
    advice = _advice(retry_feedback="Add a missing null check and a unit test.")
    assert is_destructive_recommendation(advice) is False


def test_empty_text_is_not_destructive():
    advice = _advice(retry_feedback="", risks=(), operator_note="")
    assert is_destructive_recommendation(advice) is False


# ── evaluate_ci_gates: proceed + destructive ────────────────────────────────


def test_gate_clean_in_scope_non_destructive_proceeds():
    advice = _advice(expected_files=("a.py",))
    decision = _gate(advice, scope=frozenset({"a.py"}))
    assert decision.proceed is True
    assert decision.scope_unchecked is False


def test_gate_destructive_stops_needs_operator():
    # retry_feedback / high, but a destructive marker hides in risks.
    advice = _advice(risks=("we may need git reset --hard",), expected_files=("a.py",))
    decision = _gate(advice, scope=frozenset({"a.py"}))
    assert decision.proceed is False
    assert decision.reason == "destructive_action"
    assert decision.stop_state == "needs_operator"


# ── evaluate_ci_gates: per-gate stops ───────────────────────────────────────


def test_gate_waiver_stops():
    decision = _gate(_advice(action="continue_with_waiver"))
    assert decision.proceed is False
    assert decision.reason == "waiver"
    assert decision.stop_state == "needs_operator"


def test_gate_halt_stops_with_halt_state():
    decision = _gate(_advice(action="halt"))
    assert decision.proceed is False
    assert decision.reason == "halt"
    assert decision.stop_state == "halt"


def test_gate_continue_stops_needs_operator():
    decision = _gate(_advice(action="continue"))
    assert decision.proceed is False
    assert decision.reason == "continue"
    assert decision.stop_state == "needs_operator"


def test_gate_low_confidence_stops():
    decision = _gate(_advice(confidence="low"))
    assert decision.proceed is False
    assert decision.reason == "low_confidence_ambiguity"
    assert decision.stop_state == "needs_operator"


def test_gate_budget_exhausted_stops():
    decision = _gate(_advice(), budget_remaining=0)
    assert decision.proceed is False
    assert decision.reason == "budget_exhausted"
    assert decision.stop_state == "budget_exhausted"


def test_gate_repeated_blocking_finding_stops():
    findings = (_finding("F1", "P1"),)
    decision = _gate(_advice(), findings=findings, repeated=True)
    assert decision.proceed is False
    assert decision.reason == "repeated_finding"
    assert decision.stop_state == "repeated_finding"


def test_gate_repeated_low_severity_does_not_block():
    # A repeated but non-blocking (P3) finding is not a P1/P2 loop.
    findings = (_finding("F1", "P3"),)
    decision = _gate(
        _advice(expected_files=("a.py",)),
        findings=findings, scope=frozenset({"a.py"}), repeated=True,
    )
    assert decision.proceed is True


# ── evaluate_ci_gates: scope gate ───────────────────────────────────────────


def test_gate_in_scope_proceeds():
    advice = _advice(expected_files=("pkg/mod.py",))
    decision = _gate(advice, scope=frozenset({"pkg/*"}))
    assert decision.proceed is True
    assert decision.scope_unchecked is False


def test_gate_out_of_scope_stops():
    advice = _advice(expected_files=("other/secret.py",))
    decision = _gate(advice, scope=frozenset({"pkg/*"}))
    assert decision.proceed is False
    assert decision.reason == "out_of_scope"
    assert decision.stop_state == "needs_operator"


def test_gate_missing_scope_proceeds_unchecked():
    advice = _advice(expected_files=("anywhere/file.py",))
    decision = _gate(advice, scope=frozenset())
    assert decision.proceed is True
    assert decision.scope_unchecked is True


# ── findings_fingerprint ────────────────────────────────────────────────────


def test_fingerprint_identical_findings_are_repeated():
    a = findings_fingerprint((_finding("F1", "P1"),))
    b = findings_fingerprint((_finding("F1", "P1"),))
    assert a == b


def test_fingerprint_changed_findings_differ():
    a = findings_fingerprint((_finding("F1", "P1"),))
    b = findings_fingerprint((_finding("F1", "P2"),))
    assert a != b


# ── build_provenance_note source ────────────────────────────────────────────


def test_provenance_note_ci_agent_source():
    note = build_provenance_note("phase_handoff_advice/h1.json", source="ci_agent")
    assert note.startswith("feedback_source=ci_agent;")


def test_provenance_note_default_agent_advice_source():
    note = build_provenance_note("phase_handoff_advice/h1.json")
    assert note.startswith("feedback_source=agent_advice;")
