"""Non-interactive (CI) advisory sub-flow (T2).

Covers ``pipeline.project.handoff_advice_ci.handle_ci_advice``: the prompt-free,
policy-driven path that turns an eligible paused rejected/incomplete handoff into
either a ``ci_agent`` ``retry_feedback`` ``HandoffDecisionInput`` or a typed stop.

The advisor is monkeypatched on the ``handoff_advice`` module object — no real
provider is ever invoked — and a call counter proves the pre-advisor stops never
reach it. The only durable write is the advice artifact (a real temp run dir).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.entities import SubTask
from pipeline.plan_parser import ParsedPlan
from pipeline.project import handoff_advice as _adv, handoff_advice_ci as ci
from pipeline.project.handoff_advice import AdvisorResult, HandoffAdvice
from pipeline.project.handoff_advice_policy import HandoffAdvicePolicy

# ── fixtures / helpers ──────────────────────────────────────────────────────


def _advice(
    *,
    action: str = "retry_feedback",
    confidence: str = "high",
    retry_feedback: str = "Add the missing null check and a regression test.",
    risks: tuple[str, ...] = (),
    expected_files: tuple[str, ...] = ("a.py",),
    operator_note: str = "",
    parse_warnings: tuple[str, ...] = (),
) -> HandoffAdvice:
    return HandoffAdvice(
        recommended_action=action,  # type: ignore[arg-type]
        confidence=confidence,  # type: ignore[arg-type]
        rationale="because",
        retry_feedback=retry_feedback,
        risks=risks,
        expected_files=expected_files,
        operator_note=operator_note,
        parse_warnings=parse_warnings,
    )


def _plan(owned: tuple[str, ...] = ("a.py",)) -> ParsedPlan:
    return ParsedPlan(
        subtasks=(SubTask(id="t1", goal="g"),),
        source="json",
        owned_files=owned,
    )


def _run(tmp_path, parsed_plan: ParsedPlan | None) -> SimpleNamespace:
    state = SimpleNamespace(
        parsed_plan=parsed_plan,
        phase_config=None,
        task="do a thing",
        extras={},
    )
    return SimpleNamespace(
        state=state,
        output_dir=tmp_path,
        git_cwd=str(tmp_path),
        session_ts="20260613_1",
    )


def _signal(
    *,
    findings: tuple[dict, ...] = (),
    available: tuple[str, ...] = ("retry_feedback",),
    trigger: str = "rejected",
) -> SimpleNamespace:
    return SimpleNamespace(
        handoff_id="h1",
        phase="implement",
        trigger=trigger,
        verdict="REJECTED",
        approved=False,
        available_actions=available,
        artifacts={"findings": list(findings)},
        last_output="reviewer rejected the change",
        round=1,
        loop_max_rounds=1,
    )


def _finding(fid: str, severity: str, title: str = "t") -> dict:
    return {"id": fid, "severity": severity, "title": title}


class _Counter:
    def __init__(self) -> None:
        self.n = 0


@pytest.fixture
def patch_advisor(monkeypatch):
    """Monkeypatch ``invoke_advisor`` to return a fixed advice; returns counter."""

    counter = _Counter()

    def _install(advice: HandoffAdvice, *, raise_exc: bool = False):
        def fake_invoke(run, ctx, **kwargs):
            counter.n += 1
            if raise_exc:
                raise RuntimeError("provider blew up")
            return AdvisorResult(advice=advice, raw="{}", usage={})

        monkeypatch.setattr(_adv, "invoke_advisor", fake_invoke)
        return counter

    return _install


_AUTO = HandoffAdvicePolicy(auto_retry_with_agent=True)


# ── proceed path ────────────────────────────────────────────────────────────


def test_eligible_retry_feedback_produces_ci_agent_decision(tmp_path, patch_advisor):
    patch_advisor(_advice(expected_files=("a.py",)))
    out = ci.handle_ci_advice(
        _run(tmp_path, _plan(owned=("a.py",))),
        _signal(),
        _AUTO,
        budget_remaining=1,
    )
    assert out.outcome == "retry"
    assert out.decision_input is not None
    assert out.decision_input.action == "retry_feedback"
    assert out.decision_input.feedback == "Add the missing null check and a regression test."
    assert "feedback_source=ci_agent" in out.decision_input.note
    assert "phase_handoff_advice/" in out.decision_input.note
    assert out.last_recommendation == "retry_feedback"
    assert out.last_confidence == "high"


# ── gate-driven stops (advisor invoked) ─────────────────────────────────────


def test_low_confidence_stops(tmp_path, patch_advisor):
    patch_advisor(_advice(confidence="low"))
    out = ci.handle_ci_advice(
        _run(tmp_path, _plan()),
        _signal(),
        _AUTO,
        budget_remaining=1,
    )
    assert out.outcome == "stop"
    assert out.reason == "advice_confidence_low"


def test_halt_stops(tmp_path, patch_advisor):
    patch_advisor(_advice(action="halt"))
    out = ci.handle_ci_advice(
        _run(tmp_path, _plan()),
        _signal(),
        _AUTO,
        budget_remaining=1,
    )
    assert out.outcome == "stop"
    assert out.state == "halt"


def test_continue_stops(tmp_path, patch_advisor):
    patch_advisor(_advice(action="continue"))
    out = ci.handle_ci_advice(
        _run(tmp_path, _plan()),
        _signal(),
        _AUTO,
        budget_remaining=1,
    )
    assert out.outcome == "stop"
    assert out.reason == "continue"


def test_waiver_stops_needs_operator(tmp_path, patch_advisor):
    patch_advisor(_advice(action="continue_with_waiver"))
    out = ci.handle_ci_advice(
        _run(tmp_path, _plan()),
        _signal(),
        _AUTO,
        budget_remaining=1,
    )
    assert out.outcome == "stop"
    assert out.reason == "waiver"
    assert out.state == "needs_operator"


def test_hygiene_gate_stops_for_operator_waiver_without_advisor(tmp_path, patch_advisor):
    counter = patch_advisor(_advice())
    out = ci.handle_ci_advice(
        _run(tmp_path, _plan()),
        _signal(
            trigger="verification_gate_failed",
            available=("continue_with_waiver", "halt"),
            findings=({"failure_kind": "env_failure", "severity": "P3"},),
        ),
        _AUTO,
        budget_remaining=1,
    )
    assert out.outcome == "stop"
    assert out.state == "needs_operator"
    assert out.reason == "waiver"
    assert counter.n == 0


def test_out_of_scope_stops(tmp_path, patch_advisor):
    patch_advisor(_advice(expected_files=("other/secret.py",)))
    out = ci.handle_ci_advice(
        _run(tmp_path, _plan(owned=("a.py",))),
        _signal(),
        _AUTO,
        budget_remaining=1,
    )
    assert out.outcome == "stop"
    assert out.reason == "out_of_scope"


def test_destructive_marker_stops(tmp_path, patch_advisor):
    patch_advisor(_advice(risks=("we may need git reset --hard",)))
    out = ci.handle_ci_advice(
        _run(tmp_path, _plan(owned=("a.py",))),
        _signal(),
        _AUTO,
        budget_remaining=1,
    )
    assert out.outcome == "stop"
    assert out.reason == "destructive_action"
    assert out.state == "needs_operator"


def test_repeated_blocking_finding_stops(tmp_path, patch_advisor):
    findings = (_finding("F1", "P1"),)
    patch_advisor(_advice())
    from pipeline.project.handoff_advice_policy import findings_fingerprint

    prev = findings_fingerprint(findings)
    out = ci.handle_ci_advice(
        _run(tmp_path, _plan()),
        _signal(findings=findings),
        _AUTO,
        budget_remaining=1,
        prev_findings_fingerprint=prev,
    )
    assert out.outcome == "stop"
    assert out.state == "repeated_finding"


# ── advisor failure modes ───────────────────────────────────────────────────


def test_advisor_exception_stops_needs_operator(tmp_path, patch_advisor):
    counter = patch_advisor(_advice(), raise_exc=True)
    out = ci.handle_ci_advice(
        _run(tmp_path, _plan()),
        _signal(),
        _AUTO,
        budget_remaining=1,
    )
    assert out.outcome == "stop"
    assert out.state == "needs_operator"
    assert out.reason == "advisor_error"
    assert counter.n == 1


def test_unparseable_advice_stops_needs_operator(tmp_path, patch_advisor):
    patch_advisor(_advice(parse_warnings=("advice_unparseable",)))
    out = ci.handle_ci_advice(
        _run(tmp_path, _plan()),
        _signal(),
        _AUTO,
        budget_remaining=1,
    )
    assert out.outcome == "stop"
    assert out.state == "needs_operator"
    assert out.reason == "advice_unparseable"


# ── pre-advisor stops never invoke the advisor ──────────────────────────────


def test_policy_not_auto_stops_without_invoking(tmp_path, patch_advisor):
    counter = patch_advisor(_advice())
    out = ci.handle_ci_advice(
        _run(tmp_path, _plan()),
        _signal(),
        HandoffAdvicePolicy(auto_retry_with_agent=False),
        budget_remaining=1,
    )
    assert out.outcome == "stop"
    assert counter.n == 0


def test_retry_feedback_unavailable_stops_without_invoking(tmp_path, patch_advisor):
    counter = patch_advisor(_advice())
    out = ci.handle_ci_advice(
        _run(tmp_path, _plan()),
        _signal(available=("halt",)),
        _AUTO,
        budget_remaining=1,
    )
    assert out.outcome == "stop"
    assert out.reason == "retry_feedback_unavailable"
    assert counter.n == 0


def test_budget_exhausted_stops_without_invoking(tmp_path, patch_advisor):
    counter = patch_advisor(_advice())
    out = ci.handle_ci_advice(
        _run(tmp_path, _plan()),
        _signal(),
        _AUTO,
        budget_remaining=0,
    )
    assert out.outcome == "stop"
    assert out.state == "budget_exhausted"
    assert counter.n == 0


# ── aggregate fields always present ─────────────────────────────────────────


def test_result_always_carries_aggregate_fields(tmp_path, patch_advisor):
    patch_advisor(_advice(expected_files=("a.py",)))
    out = ci.handle_ci_advice(
        _run(tmp_path, _plan(owned=("a.py",))),
        _signal(),
        _AUTO,
        budget_remaining=1,
    )
    assert hasattr(out, "last_recommendation")
    assert hasattr(out, "last_confidence")
    assert hasattr(out, "findings_fingerprint")
    assert hasattr(out, "scope_unchecked")
