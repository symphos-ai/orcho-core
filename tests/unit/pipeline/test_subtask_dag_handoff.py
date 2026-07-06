"""Unit / mock tests for ``pipeline.phases.builtin.subtask_dag_handoff`` (ADR 0073).

Exercises every branch of the implement substance-repair handoff handler with
a fake ``repair_pass`` (no agents): successful repair → ``repaired``;
exhaustion → ``PhaseHandoffRequested`` pause with the full action set; eligible
auto-waiver → in-process synthetic decision via the T9 API (no public
``phase_handoff_decide``); ineligible flag → pause; retry-mode forcing a pass.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.entities import SubTask
from pipeline.dag_runner import DagRunResult, ImplementationReceipt, SubTaskResult
from pipeline.phases.builtin.subtask_dag_handoff import (
    AUTO_WAIVER_DECIDED_BY,
    IMPLEMENT_HANDOFF_ID,
    IMPLEMENT_HANDOFF_ROUND_KEY,
    handle_subtask_dag_handoff,
)
from pipeline.plan_parser import ParsedPlan
from pipeline.runtime import PhaseHandoffPolicy, PhaseHandoffType, PipelineState
from sdk.phase_handoff import safe_handoff_id


def _state(tmp_path: Path | None = None, **extras) -> PipelineState:
    st = PipelineState(task="t", project_dir="/p", plugin=None)
    if tmp_path is not None:
        st.output_dir = tmp_path
        st.extras["run_id"] = tmp_path.name
    st.extras.update(extras)
    return st


def _plan(*subs: SubTask) -> ParsedPlan:
    return ParsedPlan(subtasks=tuple(subs), source="json", short_summary="s")


def _st(sid: str, deps: tuple[str, ...] = ()) -> SubTask:
    return SubTask(id=sid, goal=f"g-{sid}", depends_on=deps)


def _done_result(sid: str) -> SubTaskResult:
    return SubTaskResult(
        subtask_id=sid, runtime="claude", model="m", skill=None,
        output="ok", duration=0.1,
    )


def _receipt(sid: str, state: str) -> ImplementationReceipt:
    return ImplementationReceipt(
        subtask_id=sid, state=state, runtime="claude", model="m", skill=None,
    )


def _policy(on_exhausted: str = "halt", repair_attempts: int = 1) -> PhaseHandoffPolicy:
    return PhaseHandoffPolicy(
        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
        repair_attempts=repair_attempts,
        on_exhausted=on_exhausted,
    )


def _decisions_dir(run_dir: Path) -> Path:
    return run_dir / "phase_handoff_decisions"


def _seed_decision(run_dir: Path, handoff_id: str, action: str = "retry_feedback") -> None:
    decisions = _decisions_dir(run_dir)
    decisions.mkdir(parents=True, exist_ok=True)
    (decisions / f"{safe_handoff_id(handoff_id)}.json").write_text(
        json.dumps({
            "run_id": run_dir.name,
            "handoff_id": handoff_id,
            "phase": "implement",
            "action": action,
            "feedback": "retry",
            "note": "test",
            "decided_at": "2026-06-14T00:00:00+00:00",
        }),
        encoding="utf-8",
    )


# ── successful repair ──────────────────────────────────────────────────────

def test_successful_repair_marks_repaired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(_st("b"))
    sections: list[tuple[str, str]] = []

    def _record_section(label: str, content: str = "", **_kwargs) -> None:
        sections.append((label, content))

    monkeypatch.setattr(
        "agents.stream_log.write_agent_log_section",
        _record_section,
    )

    def repair_pass(repair_plan, prior):
        return DagRunResult(
            completed=(_done_result("b"),), receipts=(_receipt("b", "done"),),
        )

    out = handle_subtask_dag_handoff(
        _state(),
        policy=_policy(repair_attempts=1),
        parsed_plan=plan,
        incomplete_ids=("b",),
        missing_ids=(),
        attestation_incomplete={"b": "criteria not closed"},
        findings=None,
        done_context={},
        repair_pass=repair_pass,
    )
    assert out.delivery_status == "repaired"
    assert out.paused is False
    assert out.repaired_ids == ("b",)
    assert out.signal is None
    assert sections
    label, content = sections[0]
    assert label == "ORCHO subtask attestation auto-fix"
    assert "mode: auto_repair" in content
    assert "repair_subtasks: b" in content
    assert "repair_attempts_budget: 1" in content


# ── exhaustion → pause ──────────────────────────────────────────────────────

def test_exhaustion_emits_full_signal(tmp_path: Path) -> None:
    plan = _plan(_st("b"))

    def repair_pass(repair_plan, prior):
        return DagRunResult(completed=(), receipts=(_receipt("b", "incomplete"),))

    state = _state(tmp_path)
    out = handle_subtask_dag_handoff(
        state,
        policy=_policy(on_exhausted="halt", repair_attempts=1),
        parsed_plan=plan,
        incomplete_ids=("b",),
        missing_ids=(),
        attestation_incomplete={"b": "missing attestation"},
        findings=["finding-1"],
        done_context={},
        repair_pass=repair_pass,
        last_output="build log",
    )
    assert out.paused is True
    assert out.delivery_status == "incomplete"
    sig = out.signal
    assert sig is state.phase_handoff_request
    assert sig.handoff_id == IMPLEMENT_HANDOFF_ID
    assert sig.round == 1
    assert sig.loop_max_rounds == 1
    assert sig.round_extras_key == IMPLEMENT_HANDOFF_ROUND_KEY
    assert sig.trigger == "incomplete"
    assert sig.verdict == "INCOMPLETE"
    assert sig.approved is False
    assert set(sig.available_actions) == {
        "continue", "retry_feedback", "continue_with_waiver", "halt",
    }
    assert sig.artifacts["incomplete_subtasks"] == ["b"]
    assert sig.artifacts["attestation_incomplete"] == {"b": "missing attestation"}
    assert sig.artifacts["findings"] == ["finding-1"]
    # No decision artifact: the public decide path was not taken.
    assert not _decisions_dir(tmp_path).exists()


def test_repause_after_retry_uses_fresh_handoff_id(tmp_path: Path) -> None:
    plan = _plan(_st("b"))
    _seed_decision(tmp_path, IMPLEMENT_HANDOFF_ID)

    def repair_pass(repair_plan, prior):
        return DagRunResult(completed=(), receipts=(_receipt("b", "incomplete"),))

    state = _state(tmp_path, implement_retry={"ids": ["b"]})
    out = handle_subtask_dag_handoff(
        state,
        policy=_policy(on_exhausted="halt", repair_attempts=0),
        parsed_plan=plan,
        incomplete_ids=("b",),
        missing_ids=(),
        attestation_incomplete={"b": "still incomplete"},
        findings=None,
        done_context={},
        repair_pass=repair_pass,
    )

    assert out.paused is True
    assert state.phase_handoff_request is not None
    assert state.phase_handoff_request.handoff_id == "implement:implement_handoff:2"
    assert state.phase_handoff_request.round == 2
    assert (
        _decisions_dir(tmp_path)
        / f"{safe_handoff_id(state.phase_handoff_request.handoff_id)}.json"
    ).exists() is False


def test_ineligible_auto_waiver_flag_false_pauses(tmp_path: Path) -> None:
    plan = _plan(_st("b"))

    def repair_pass(repair_plan, prior):
        return DagRunResult(completed=(), receipts=(_receipt("b", "incomplete"),))

    # on_exhausted=auto_waiver but the operator opt-in is absent → pause.
    state = _state(tmp_path)  # no auto_waiver_allowed
    out = handle_subtask_dag_handoff(
        state,
        policy=_policy(on_exhausted="auto_waiver", repair_attempts=1),
        parsed_plan=plan,
        incomplete_ids=("b",),
        missing_ids=(),
        attestation_incomplete={"b": "x"},
        findings=None,
        done_context={},
        repair_pass=repair_pass,
    )
    assert out.paused is True
    assert out.delivery_status == "incomplete"
    assert state.phase_handoff_request is not None
    assert not _decisions_dir(tmp_path).exists()


# ── eligible auto-waiver ────────────────────────────────────────────────────

def test_eligible_auto_waiver_records_synthetic_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Guard: the public decide must NEVER be called on a running run.
    import sdk.phase_handoff as _ph

    def _boom(*a, **k):
        raise AssertionError("public phase_handoff_decide must not be called")

    monkeypatch.setattr(_ph, "phase_handoff_decide", _boom)

    plan = _plan(_st("b"))

    def repair_pass(repair_plan, prior):
        return DagRunResult(completed=(), receipts=(_receipt("b", "incomplete"),))

    state = _state(tmp_path, auto_waiver_allowed=True)
    out = handle_subtask_dag_handoff(
        state,
        policy=_policy(on_exhausted="auto_waiver", repair_attempts=1),
        parsed_plan=plan,
        incomplete_ids=("b",),
        missing_ids=(),
        attestation_incomplete={"b": "criteria not closed"},
        findings=["f1"],
        done_context={},
        repair_pass=repair_pass,
    )
    assert out.paused is False
    assert out.delivery_status == "waived"
    assert out.decided_by == AUTO_WAIVER_DECIDED_BY
    assert out.action == "continue_with_waiver"
    assert out.waiver_id == IMPLEMENT_HANDOFF_ID
    # Durable state waiver applied via the T9 API.
    waiver = state.extras["phase_handoff_waiver"]
    assert waiver["decided_by"] == AUTO_WAIVER_DECIDED_BY
    assert waiver["waiver_text"]
    # Synthetic decision artifact written via record_decision_artifact.
    artifact = _decisions_dir(tmp_path) / f"{safe_handoff_id(IMPLEMENT_HANDOFF_ID)}.json"
    assert artifact.is_file()


def test_auto_waiver_after_retry_uses_fresh_handoff_id(tmp_path: Path) -> None:
    plan = _plan(_st("b"))
    _seed_decision(tmp_path, IMPLEMENT_HANDOFF_ID)

    def repair_pass(repair_plan, prior):
        return DagRunResult(completed=(), receipts=(_receipt("b", "incomplete"),))

    state = _state(tmp_path, auto_waiver_allowed=True, implement_retry={"ids": ["b"]})
    out = handle_subtask_dag_handoff(
        state,
        policy=_policy(on_exhausted="auto_waiver", repair_attempts=0),
        parsed_plan=plan,
        incomplete_ids=("b",),
        missing_ids=(),
        attestation_incomplete={"b": "criteria not closed"},
        findings=None,
        done_context={},
        repair_pass=repair_pass,
    )

    assert out.paused is False
    assert out.delivery_status == "waived"
    assert out.waiver_id == "implement:implement_handoff:2"
    assert state.extras["phase_handoff_waiver"]["handoff_id"] == out.waiver_id
    assert (
        _decisions_dir(tmp_path)
        / f"{safe_handoff_id('implement:implement_handoff:2')}.json"
    ).is_file()


def test_eligible_auto_waiver_is_idempotent(tmp_path: Path) -> None:
    plan = _plan(_st("b"))

    def repair_pass(repair_plan, prior):
        return DagRunResult(completed=(), receipts=(_receipt("b", "incomplete"),))

    kwargs = dict(
        policy=_policy(on_exhausted="auto_waiver", repair_attempts=1),
        parsed_plan=plan,
        incomplete_ids=("b",),
        missing_ids=(),
        attestation_incomplete={"b": "criteria not closed"},
        findings=None,
        done_context={},
        repair_pass=repair_pass,
    )
    out1 = handle_subtask_dag_handoff(_state(tmp_path, auto_waiver_allowed=True), **kwargs)
    out2 = handle_subtask_dag_handoff(_state(tmp_path, auto_waiver_allowed=True), **kwargs)
    assert out1.delivery_status == out2.delivery_status == "waived"
    files = list(_decisions_dir(tmp_path).glob("*.json"))
    assert len(files) == 1  # exact-payload idempotent, no conflict


# ── retry-mode ──────────────────────────────────────────────────────────────

def test_retry_mode_forces_pass_with_zero_budget(tmp_path: Path) -> None:
    plan = _plan(_st("b"))
    calls = 0

    def repair_pass(repair_plan, prior):
        nonlocal calls
        calls += 1
        return DagRunResult(
            completed=(_done_result("b"),), receipts=(_receipt("b", "done"),),
        )

    # repair_attempts=0, but implement_retry present → one forced pass.
    state = _state(tmp_path, implement_retry={"ids": ["b"]})
    out = handle_subtask_dag_handoff(
        state,
        policy=_policy(on_exhausted="halt", repair_attempts=0),
        parsed_plan=plan,
        incomplete_ids=("b",),
        missing_ids=(),
        attestation_incomplete={"b": "x"},
        findings=None,
        done_context={},
        repair_pass=repair_pass,
    )
    assert calls == 1
    assert out.retry_mode is True
    assert out.delivery_status == "repaired"
    assert out.repaired_ids == ("b",)


def test_zero_budget_no_retry_skips_repair_and_pauses(tmp_path: Path) -> None:
    plan = _plan(_st("b"))
    calls = 0

    def repair_pass(repair_plan, prior):
        nonlocal calls
        calls += 1
        return DagRunResult(completed=(_done_result("b"),), receipts=(_receipt("b", "done"),))

    state = _state(tmp_path)  # no implement_retry
    out = handle_subtask_dag_handoff(
        state,
        policy=_policy(on_exhausted="halt", repair_attempts=0),
        parsed_plan=plan,
        incomplete_ids=("b",),
        missing_ids=(),
        attestation_incomplete={"b": "x"},
        findings=None,
        done_context={},
        repair_pass=repair_pass,
    )
    assert calls == 0  # zero budget, no retry → no pass
    assert out.paused is True
    assert out.delivery_status == "incomplete"


# ── end-to-end: delivery_status reaches meta.phases.implement (T6→T7→T8) ────


class _IncompleteDev:
    """Mock developer that never closes its attestation → INCOMPLETE."""

    def __init__(self) -> None:
        self.model = "claude-opus-4-7"
        self.session_id = None
        self.runtime = "claude"

    def invoke(self, prompt, cwd, *, continue_session=False, attachments=(),
               mutates_artifacts=False) -> str:
        return "no attestation — delivery is incomplete"


def test_auto_waiver_delivery_status_persists_to_session(tmp_path: Path) -> None:
    """Full implement path: an eligible auto-waiver continues the run and the
    ``delivery_status='waived'`` lands in ``meta.phases.implement`` via the
    session adapter (T6 handler → T7 hook → T8 persistence)."""
    from types import SimpleNamespace

    from agents.registry import AgentRegistry, PhaseAgentConfig
    from pipeline.phases.builtin import _phase_implement
    from pipeline.plugins import PluginConfig
    from pipeline.session_adapters import BuildAdapter

    agent = _IncompleteDev()
    reg = AgentRegistry()
    reg.register("claude", lambda model, _e=None: agent)
    pc = PhaseAgentConfig(
        plan_agent=agent, validate_plan_agent=agent, implement_agent=agent,
        review_changes_agent=agent, repair_changes_agent=agent,
        repair_escalation_agent=agent, final_acceptance_agent=agent,
    )
    plan = ParsedPlan(
        short_summary="p", planning_context="p",
        subtasks=(SubTask(id="t1", goal="g", done_criteria=("c1",)),),
        source="test",
    )
    state = PipelineState(
        task="t", project_dir=str(tmp_path), plugin=PluginConfig(),
        parsed_plan=plan, registry=reg, phase_config=pc,
        extras={
            "run_id": tmp_path.name,
            "implementation_execution": "subtask_dag",
            "auto_waiver_allowed": True,
        },
    )
    state.output_dir = tmp_path
    policy = PhaseHandoffPolicy(
        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
        repair_attempts=1, on_exhausted="auto_waiver",
    )
    state.lifecycle_ctx = SimpleNamespace(
        active_step=SimpleNamespace(
            prompt=None, handoff=policy,
            execution_policy=SimpleNamespace(session_split=None),
        ),
    )

    _phase_implement(state)

    # Eligible auto-waiver → run continues (not halted), delivery waived.
    assert state.halt is False
    entry = state.phase_log["implement"]
    assert entry["delivery_status"] == "waived"

    # Persist via the implement adapter → meta.phases.implement.
    session: dict = {"phases": {}}
    BuildAdapter().write("implement", state, session)
    impl = session["phases"]["implement"]
    assert impl["delivery_status"] == "waived"
    assert impl["delivery_waived"] is True
    assert impl["waiver_id"] == IMPLEMENT_HANDOFF_ID
    # Synthetic decision artifact written via the T9 API (no public decide).
    artifact = _decisions_dir(tmp_path) / f"{safe_handoff_id(IMPLEMENT_HANDOFF_ID)}.json"
    assert artifact.is_file()


# ── P1a: auto-waiver durably reaches meta.phase_handoff_waiver + evidence ────


def test_auto_waiver_phase_end_sync_lands_both_fields(tmp_path: Path) -> None:
    """P1a: the implement phase-end sync mirrors the in-process auto-waiver from
    ``state.extras`` into the session, so ``meta.phases.implement.delivery_status``
    AND ``meta.phase_handoff_waiver.decided_by`` are present TOGETHER (evidence
    would otherwise lose ``decided_by``)."""
    from types import SimpleNamespace

    from pipeline.project.run import _PipelineRun
    from pipeline.runtime import PipelineState
    from pipeline.session_adapters import BuildAdapter

    state = PipelineState(task="t", project_dir="/p", plugin=None)
    # What the handler records on an eligible auto-waiver (covered separately):
    state.extras["phase_handoff_waiver"] = {
        "handoff_id":  IMPLEMENT_HANDOFF_ID,
        "phase":       "implement",
        "waiver_text": "auto-waived: b incomplete accepted",
        "decided_by":  AUTO_WAIVER_DECIDED_BY,
        "decided_at":  "2026-06-04T00:00:00+00:00",
    }
    state.phase_log["implement"] = {
        "output": "build", "delivery_status": "waived", "delivery_waived": True,
        "waiver_id": IMPLEMENT_HANDOFF_ID, "action": "continue_with_waiver",
    }

    # Implement phase-end: the real ``_on_phase_end`` wiring mirrors the waiver
    # to the session (``_dispatch_active=False`` → skips banners after the sync).
    run = SimpleNamespace(_dispatch_active=False, state=state, session={"phases": {}})
    _PipelineRun._on_phase_end(run, "implement", state)
    BuildAdapter().write("implement", state, run.session)

    waiver = run.session["phase_handoff_waiver"]
    impl = run.session["phases"]["implement"]
    # Both fields present together + consistent ids.
    assert impl["delivery_status"] == "waived"
    assert waiver["decided_by"] == AUTO_WAIVER_DECIDED_BY
    assert impl["waiver_id"] == waiver["handoff_id"]


def test_on_phase_end_waiver_sync_is_implement_scoped() -> None:
    """The waiver sync only fires at the implement phase-end — other phases
    leave the session untouched."""
    from types import SimpleNamespace

    from pipeline.project.run import _PipelineRun
    from pipeline.runtime import PipelineState

    state = PipelineState(task="t", project_dir="/p", plugin=None)
    state.extras["phase_handoff_waiver"] = {
        "handoff_id": "x", "decided_by": "operator",
    }
    run = SimpleNamespace(_dispatch_active=False, state=state, session={})
    _PipelineRun._on_phase_end(run, "review_changes", state)
    assert "phase_handoff_waiver" not in run.session


# ── P2 audit: missing-receipt ids survive the auto-waiver story ──────────────


def test_auto_waiver_with_missing_receipts_records_missing_ids(tmp_path: Path) -> None:
    """P2 audit: a delivery incomplete ONLY because a subtask produced no
    receipt is auto-waived with the missing id named in the waiver text +
    carried on the outcome — never a misleading '0 subtask(s) ... incomplete.'."""
    plan = _plan(_st("a"), _st("t3"))

    def repair_pass(repair_plan, prior):
        # Nothing to repair: no attestation-incomplete ids; t3 simply produced
        # no receipt in the original pass (repair only re-runs incomplete ids).
        return DagRunResult(completed=(), receipts=())

    state = _state(tmp_path, auto_waiver_allowed=True)
    out = handle_subtask_dag_handoff(
        state,
        policy=_policy(on_exhausted="auto_waiver", repair_attempts=1),
        parsed_plan=plan,
        incomplete_ids=(),          # no attestation-incomplete subtask
        missing_ids=("t3",),        # t3 never produced a receipt
        attestation_incomplete={},
        findings=None,
        done_context={},
        repair_pass=repair_pass,
    )
    assert out.delivery_status == "waived"
    assert out.missing_ids == ("t3",)
    waiver_text = state.extras["phase_handoff_waiver"]["waiver_text"]
    assert "t3" in waiver_text
    assert "no delivery receipt" in waiver_text
    # The headline must not claim zero issues when a receipt was missing.
    assert "0 subtask(s) remained incomplete." not in waiver_text


def test_buildadapter_persists_blocking_ids_for_audit() -> None:
    """The durable audit (meta.phases.implement) keeps WHICH subtasks blocked
    delivery — incomplete + missing-receipt ids — so a waiver is traceable."""
    from pipeline.runtime import PipelineState
    from pipeline.session_adapters import BuildAdapter

    state = PipelineState(task="t", project_dir="/p", plugin=None)
    state.phase_log["implement"] = {
        "output": "build", "delivery_status": "waived", "delivery_waived": True,
        "waiver_id": IMPLEMENT_HANDOFF_ID, "action": "continue_with_waiver",
        "missing_subtask_receipts": ["t3"],
        "incomplete_subtasks": ["t2"],
        "attestation_incomplete": {"t2": "criteria not closed"},
    }
    session: dict = {"phases": {}}
    BuildAdapter().write("implement", state, session)
    impl = session["phases"]["implement"]
    assert impl["missing_subtask_receipts"] == ["t3"]
    assert impl["incomplete_subtasks"] == ["t2"]
    assert impl["attestation_incomplete"] == {"t2": "criteria not closed"}
