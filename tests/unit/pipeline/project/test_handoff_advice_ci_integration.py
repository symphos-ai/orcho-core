"""CI auto-retry integration in ``process_pending_phase_handoffs`` (T3).

Exercises the non-interactive branch end-to-end with the advisor mocked and the
SDK decide + in-process resume monkeypatched: an eligible paused handoff turns
into a ``ci_agent`` ``retry_feedback`` decision through the SAME
``apply_phase_handoff_resume_with_banners`` path a human retry uses, the
``_ci_agent_advice`` aggregate tracks retries/resolved/stopped across the bounded
loop, repeated P1/P2 + budget exhaustion stop instead of looping, and unsafe
recommendations stop without recording a retry decision. Interactive runs and a
disabled policy never reach the advisor.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import sdk.phase_handoff as _sdk_handoff
from agents.entities import SubTask
from pipeline.plan_parser import ParsedPlan
from pipeline.project import (
    handoff as handoff_mod,
    handoff_advice as _adv,
    handoff_advice_ci as _ci,
    handoff_advice_policy as _policy,
)
from pipeline.project.handoff import (
    PhaseHandoffResumeOutcome,
    process_pending_phase_handoffs,
)
from pipeline.project.handoff_advice import AdvisorResult, HandoffAdvice
from pipeline.project.handoff_advice_policy import HandoffAdvicePolicy
from pipeline.project.handoff_noninteractive import UNATTENDED_HALT_REASON

# ── helpers ─────────────────────────────────────────────────────────────────


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
        subtasks=(SubTask(id="t1", goal="g"),), source="json", owned_files=owned,
    )


def _signal(
    *,
    findings: tuple[dict, ...] = (),
    phase: str = "implement",
    trigger: str = "rejected",
    available_actions: tuple[str, ...] = ("retry_feedback",),
) -> SimpleNamespace:
    return SimpleNamespace(
        handoff_id="h1",
        phase=phase,
        trigger=trigger,
        verdict="REJECTED",
        approved=False,
        available_actions=available_actions,
        artifacts={"findings": list(findings)},
        last_output="reviewer rejected the change",
        round=1,
        loop_max_rounds=1,
    )


def _finding(fid: str, severity: str, title: str = "t") -> dict:
    return {"id": fid, "severity": severity, "title": title}


def _run(
    tmp_path,
    signal,
    *,
    parsed_plan: ParsedPlan | None,
    unattended: bool = False,
) -> SimpleNamespace:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state = SimpleNamespace(
        phase_handoff_request=signal,
        extras={},
        task="do a thing",
        parsed_plan=parsed_plan,
        phase_config=None,
        halt=False,
        halt_reason="",
    )
    return SimpleNamespace(
        state=state,
        no_interactive=True,
        unattended=unattended,
        session={"status": "awaiting_phase_handoff"},
        session_ts="20260613_1",
        output_dir=run_dir,
        git_cwd=str(run_dir),
        registry=None,
        _ckpt=None,
        _dispatch_active=False,
    )


class _ResumeScript:
    """Scripted stand-in for ``apply_phase_handoff_resume_with_banners``."""

    def __init__(self, steps, *, new_signal=None) -> None:
        self.steps = list(steps)
        self.new_signal = new_signal
        self.calls = 0

    def __call__(self, run, profile, ctx, *, on_round_end=None):
        self.calls += 1
        step = self.steps.pop(0)
        if step == "new_handoff":
            run.state.phase_handoff_request = self.new_signal
            return PhaseHandoffResumeOutcome(
                profile=profile, completed_phases=frozenset(), paused=True,
            )
        # 'resolve': caller already cleared the request; no fresh handoff.
        return PhaseHandoffResumeOutcome(
            profile=None, completed_phases=frozenset(), paused=False,
        )


class _DecideRecorder:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, session_ts, handoff_id, action, *, feedback=None,
                 note=None, runs_dir=None, cwd=None):
        self.calls.append(
            SimpleNamespace(action=action, feedback=feedback, note=note),
        )


class _Counter:
    def __init__(self) -> None:
        self.n = 0


@pytest.fixture
def wired(monkeypatch):
    """Neutralise the heavy pause/decide tail; return advisor + decide probes."""

    monkeypatch.setattr(handoff_mod, "apply_phase_handoff_pause", lambda run: None)
    decide = _DecideRecorder()
    monkeypatch.setattr(_sdk_handoff, "phase_handoff_decide", decide)
    counter = _Counter()

    def install_advice(advice):
        def fake_invoke(run, ctx, **kwargs):
            counter.n += 1
            return AdvisorResult(advice=advice, raw="{}", usage={})

        monkeypatch.setattr(_adv, "invoke_advisor", fake_invoke)

    return SimpleNamespace(
        monkeypatch=monkeypatch, decide=decide, advisor_calls=counter,
        install_advice=install_advice,
    )


def _aggregate(run) -> dict:
    return run.state.extras["_ci_agent_advice"]


# ── retry → resolved ────────────────────────────────────────────────────────


def test_eligible_retry_records_ci_agent_decision_and_resolves(tmp_path, wired):
    wired.install_advice(_advice(expected_files=("a.py",)))
    resume = _ResumeScript(["resolve"])
    wired.monkeypatch.setattr(
        handoff_mod, "apply_phase_handoff_resume_with_banners", resume,
    )
    signal = _signal()
    run = _run(tmp_path, signal, parsed_plan=_plan(owned=("a.py",)))

    result = process_pending_phase_handoffs(run, profile="P", ctx="C")

    assert len(wired.decide.calls) == 1
    assert wired.decide.calls[0].action == "retry_feedback"
    assert "feedback_source=ci_agent" in wired.decide.calls[0].note
    agg = _aggregate(run)
    assert agg["retries"] == 1
    assert agg["resolved"] == 1
    assert agg["stopped"] == 0
    assert agg["last_recommendation"] == "retry_feedback"
    assert result.continue_dispatch is True


# ── bounded loop: repeated finding + budget exhaustion ──────────────────────


def test_repeated_blocking_finding_stops_without_looping(tmp_path, wired):
    findings = (_finding("F1", "P1"),)
    wired.install_advice(_advice(expected_files=("a.py",)))
    # Widen the budget so pass two reaches the repeated gate (not budget).
    wired.monkeypatch.setattr(
        _policy, "resolve_handoff_advice_policy",
        lambda run: HandoffAdvicePolicy(auto_retry_with_agent=True, max_agent_retries=2),
    )
    new_signal = _signal(findings=findings)
    resume = _ResumeScript(["new_handoff"], new_signal=new_signal)
    wired.monkeypatch.setattr(
        handoff_mod, "apply_phase_handoff_resume_with_banners", resume,
    )
    run = _run(tmp_path, _signal(findings=findings), parsed_plan=_plan(owned=("a.py",)))

    result = process_pending_phase_handoffs(run, profile="P", ctx="C")

    agg = _aggregate(run)
    assert agg["retries"] == 1
    assert agg["resolved"] == 0
    assert agg["stopped"] == 1
    assert len(wired.decide.calls) == 1  # only the first retry wrote a decision
    assert resume.calls == 1
    assert result.paused is True


def test_budget_exhausted_stops_after_one_retry(tmp_path, wired):
    wired.install_advice(_advice(expected_files=("a.py",)))
    new_signal = _signal()  # fresh eligible handoff after the retry round
    resume = _ResumeScript(["new_handoff"], new_signal=new_signal)
    wired.monkeypatch.setattr(
        handoff_mod, "apply_phase_handoff_resume_with_banners", resume,
    )
    run = _run(tmp_path, _signal(), parsed_plan=_plan(owned=("a.py",)))

    result = process_pending_phase_handoffs(run, profile="P", ctx="C")

    agg = _aggregate(run)
    assert agg["retries"] == 1
    assert agg["stopped"] == 1
    assert len(wired.decide.calls) == 1
    assert result.paused is True


# ── unsafe recommendations stop without a retry decision ────────────────────


@pytest.mark.parametrize(
    ("kwargs", "expected_paused"),
    [
        ({"confidence": "low"}, True),
        ({"action": "continue_with_waiver"}, True),
        ({"expected_files": ("other/secret.py",)}, True),
        ({"risks": ("we may need git reset --hard",)}, True),
    ],
)
def test_unsafe_recommendation_stops_no_decision(
    tmp_path, wired, kwargs, expected_paused,
):
    wired.install_advice(_advice(**kwargs))
    resume = _ResumeScript([])
    wired.monkeypatch.setattr(
        handoff_mod, "apply_phase_handoff_resume_with_banners", resume,
    )
    run = _run(tmp_path, _signal(), parsed_plan=_plan(owned=("a.py",)))

    result = process_pending_phase_handoffs(run, profile="P", ctx="C")

    agg = _aggregate(run)
    assert agg["retries"] == 0
    assert agg["stopped"] == 1
    assert len(wired.decide.calls) == 0
    assert result.paused is expected_paused
    assert resume.calls == 0


def test_halt_recommendation_routes_through_finalize(tmp_path, wired):
    # A CI halt does NOT bypass finalize: it sets state.halt + clears the
    # request and returns continue_dispatch, so the real caller
    # (profile_dispatch) falls through to run.finalize() → HALTED summary.
    wired.install_advice(_advice(action="halt"))
    wired.monkeypatch.setattr(handoff_mod, "save_session", lambda *a, **k: None)
    resume = _ResumeScript([])
    wired.monkeypatch.setattr(
        handoff_mod, "apply_phase_handoff_resume_with_banners", resume,
    )
    run = _run(tmp_path, _signal(), parsed_plan=_plan(owned=("a.py",)))

    result = process_pending_phase_handoffs(run, profile="P", ctx="C")

    agg = _aggregate(run)
    assert agg["retries"] == 0
    assert agg["stopped"] == 1
    assert len(wired.decide.calls) == 0
    # Not a halted PhaseHandoffLoopResult — the caller's finalize() owns the
    # halt; state.halt drives _resolve_terminal_status to a HALTED finalization.
    assert result.halted is False
    assert result.continue_dispatch is True
    assert run.state.halt is True
    assert run.state.halt_reason == "phase_handoff_halt"
    assert run.state.phase_handoff_request is None
    # Aggregate mirrored into durable run meta before the halt fall-through.
    assert run.session["_ci_agent_advice"]["stopped"] == 1


def test_paused_stop_persists_aggregate_to_meta_json(tmp_path, wired):
    # A paused needs_operator stop never reaches DONE/HALTED finalization, so
    # its aggregate must be flushed to the durable run meta (meta.json) — read
    # it back from disk, not just from in-memory run.state.extras.
    import json

    wired.install_advice(_advice(expected_files=("other/secret.py",)))  # out_of_scope
    resume = _ResumeScript([])
    wired.monkeypatch.setattr(
        handoff_mod, "apply_phase_handoff_resume_with_banners", resume,
    )
    run = _run(tmp_path, _signal(), parsed_plan=_plan(owned=("a.py",)))

    result = process_pending_phase_handoffs(run, profile="P", ctx="C")
    assert result.paused is True

    meta = json.loads((run.output_dir / "meta.json").read_text(encoding="utf-8"))
    persisted = meta["_ci_agent_advice"]
    assert persisted["stopped"] == 1
    assert persisted["retries"] == 0
    assert persisted["last_recommendation"] == "retry_feedback"
    assert persisted["scope_unchecked"] is False


# ── interactive + disabled policy never reach the advisor ───────────────────


def test_interactive_run_uses_old_path_no_auto_retry(tmp_path, wired):
    from pipeline.control.handoff_prompt import _Aborted

    wired.install_advice(_advice())
    wired.monkeypatch.setattr(
        handoff_mod, "should_prompt_for_phase_handoff", lambda **k: True,
    )
    wired.monkeypatch.setattr(
        handoff_mod, "prompt_phase_handoff_action", lambda *a, **k: _Aborted(),
    )
    ci_calls = _Counter()
    wired.monkeypatch.setattr(
        _ci, "handle_ci_advice",
        lambda *a, **k: (ci_calls.__setattr__("n", ci_calls.n + 1)),
    )
    run = _run(tmp_path, _signal(), parsed_plan=_plan())
    run.no_interactive = False

    result = process_pending_phase_handoffs(run, profile="P", ctx="C")

    assert result.paused is True
    assert ci_calls.n == 0
    assert wired.advisor_calls.n == 0


def test_disabled_policy_skips_advisor_and_aggregate(tmp_path, wired):
    wired.install_advice(_advice())
    wired.monkeypatch.setattr(
        _policy, "resolve_handoff_advice_policy",
        lambda run: HandoffAdvicePolicy(auto_retry_with_agent=False),
    )
    run = _run(tmp_path, _signal(), parsed_plan=_plan())

    result = process_pending_phase_handoffs(run, profile="P", ctx="C")

    assert result.paused is True
    assert wired.advisor_calls.n == 0
    assert "_ci_agent_advice" not in run.state.extras


def test_unattended_advice_ineligible_auto_continues(tmp_path, wired):
    wired.install_advice(_advice())
    wired.monkeypatch.setattr(
        _policy, "resolve_handoff_advice_policy",
        lambda run: HandoffAdvicePolicy(auto_retry_with_agent=False),
    )
    resume = _ResumeScript(["resolve"])
    wired.monkeypatch.setattr(
        handoff_mod, "apply_phase_handoff_resume_with_banners", resume,
    )
    signal = _signal(
        phase="validate_plan",
        available_actions=("continue", "retry_feedback", "halt"),
    )
    run = _run(tmp_path, signal, parsed_plan=_plan(), unattended=True)

    result = process_pending_phase_handoffs(run, profile="P", ctx="C")

    assert result.continue_dispatch is True
    assert len(wired.decide.calls) == 1
    assert wired.decide.calls[0].action == "continue"
    assert "auto-decided by unattended policy" in wired.decide.calls[0].note
    assert wired.advisor_calls.n == 0


def test_unattended_ci_stop_auto_continues(tmp_path, wired):
    wired.install_advice(_advice(expected_files=("a.py",)))
    first = _signal(
        phase="validate_plan",
        available_actions=("continue", "retry_feedback", "halt"),
    )
    second = _signal(
        phase="validate_plan",
        available_actions=("continue", "retry_feedback", "halt"),
    )
    resume = _ResumeScript(["new_handoff", "resolve"], new_signal=second)
    wired.monkeypatch.setattr(
        handoff_mod, "apply_phase_handoff_resume_with_banners", resume,
    )
    run = _run(tmp_path, first, parsed_plan=_plan(owned=("a.py",)), unattended=True)

    result = process_pending_phase_handoffs(run, profile="P", ctx="C")

    assert result.continue_dispatch is True
    assert [call.action for call in wired.decide.calls] == [
        "retry_feedback",
        "continue",
    ]
    assert "ci_stop=budget_exhausted:budget_exhausted" in wired.decide.calls[1].note
    agg = _aggregate(run)
    assert agg["retries"] == 1
    assert agg["stopped"] == 1


def test_unattended_implement_handoff_halts_instead_of_parking(tmp_path, wired):
    wired.install_advice(_advice())
    wired.monkeypatch.setattr(
        _policy, "resolve_handoff_advice_policy",
        lambda run: HandoffAdvicePolicy(auto_retry_with_agent=False),
    )
    wired.monkeypatch.setattr(handoff_mod, "save_session", lambda *a, **k: None)
    run = _run(tmp_path, _signal(), parsed_plan=_plan(), unattended=True)

    result = process_pending_phase_handoffs(run, profile="P", ctx="C")

    assert result.continue_dispatch is True
    assert run.state.halt is True
    assert run.state.halt_reason == UNATTENDED_HALT_REASON
    assert run.state.phase_handoff_request is None
    assert run.session["phase_handoff_unattended"]["reason"] == "implement_handoff"
    assert len(wired.decide.calls) == 0
