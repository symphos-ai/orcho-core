"""T7 wiring tests: subtask_dag delegates an INCOMPLETE delivery to the
ADR 0073 repair→handoff handler when a policy is configured, keeps the legacy
hard stop otherwise, and narrows execution to incomplete ids in retry-mode.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agents.entities import SubTask
from agents.registry import AgentRegistry, PhaseAgentConfig
from agents.runtimes._strategy import _mock_subtask_attestation
from pipeline.phases.builtin import _phase_implement
from pipeline.plan_parser import ParsedPlan
from pipeline.plugins import PluginConfig
from pipeline.runtime import PhaseHandoffPolicy, PhaseHandoffType, PipelineState


class _Dev:
    def __init__(self, *, close: bool) -> None:
        self.model = "claude-opus-4-7"
        self.session_id = None
        self.runtime = "claude"
        self.close = close
        self.calls: list[str] = []

    def invoke(self, prompt, cwd, *, continue_session=False, attachments=(),
               mutates_artifacts=False) -> str:
        self.calls.append(prompt)
        if self.close:
            return "done" + _mock_subtask_attestation(prompt)
        return "no attestation — delivery is incomplete"


def _registry(agent: _Dev) -> AgentRegistry:
    reg = AgentRegistry()
    reg.register("claude", lambda model, _effort=None: agent)
    return reg


def _phase_config(agent: _Dev) -> PhaseAgentConfig:
    return PhaseAgentConfig(
        plan_agent=agent, validate_plan_agent=agent, implement_agent=agent,
        review_changes_agent=agent, repair_changes_agent=agent,
        repair_escalation_agent=agent, final_acceptance_agent=agent,
    )


def _plan(*subs: SubTask) -> ParsedPlan:
    return ParsedPlan(short_summary="p", planning_context="p",
                      subtasks=tuple(subs), source="test")


def _state(plan, agent, *, handoff=None, tmp_path: Path | None = None, **extras):
    base = {"run_id": "run-p1", "implementation_execution": "subtask_dag"}
    base.update(extras)
    st = PipelineState(
        task="t", project_dir="/p", plugin=PluginConfig(),
        parsed_plan=plan, registry=_registry(agent),
        phase_config=_phase_config(agent), extras=base,
    )
    if tmp_path is not None:
        st.output_dir = tmp_path
    # ADR 0113: the implement step declares same_zone_continue continuity; the
    # resolver reads it for the implement subtask role (companion subtasks are
    # auxiliary and resolve to fresh from their shape, never from this field).
    active = SimpleNamespace(
        prompt=None,
        execution_policy=SimpleNamespace(
            session_split=None, session_continuity="same_zone_continue"
        ),
    )
    if handoff is not None:
        active.handoff = handoff
    st.lifecycle_ctx = SimpleNamespace(active_step=active)
    return st


def _impl(state) -> dict:
    _phase_implement(state)
    return state.phase_log["implement"]


def test_unconfigured_profile_hard_stops() -> None:
    # active_step has no handoff → legacy hard stop preserved.
    agent = _Dev(close=False)
    plan = _plan(SubTask(id="t1", goal="g", done_criteria=("c1",)))
    state = _state(plan, agent)  # no handoff
    entry = _impl(state)
    assert state.halt is True
    assert "delivery blocked" in state.halt_reason
    assert "delivery_status" not in entry


def test_configured_policy_auto_waiver_continues(tmp_path: Path) -> None:
    agent = _Dev(close=False)
    plan = _plan(SubTask(id="t1", goal="g", done_criteria=("c1",)))
    policy = PhaseHandoffPolicy(
        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
        repair_attempts=1, on_exhausted="auto_waiver",
    )
    state = _state(plan, agent, handoff=policy, tmp_path=tmp_path,
                   auto_waiver_allowed=True)
    entry = _impl(state)
    # Eligible auto-waiver → no hard stop, delivery waived, run continues.
    assert state.halt is False
    assert entry["delivery_status"] == "waived"
    assert entry["delivery_waived"] is True
    assert entry["decided_by"] == "auto:on_exhausted"


def test_configured_policy_halt_pauses_not_stops(tmp_path: Path) -> None:
    agent = _Dev(close=False)
    plan = _plan(SubTask(id="t1", goal="g", done_criteria=("c1",)))
    policy = PhaseHandoffPolicy(
        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
        repair_attempts=0, on_exhausted="halt",
    )
    state = _state(plan, agent, handoff=policy, tmp_path=tmp_path)
    entry = _impl(state)
    # Exhausted + not eligible → pause (signal), NOT a hard stop.
    assert state.halt is False
    assert state.phase_handoff_request is not None
    assert state.phase_handoff_request.handoff_id == "implement:implement_handoff:1"
    assert entry["delivery_status"] == "incomplete"


def test_retry_mode_runs_only_incomplete_ids() -> None:
    agent = _Dev(close=True)  # attestation closes → delivery clean
    plan = _plan(
        SubTask(id="t1", goal="g1", done_criteria=("c1",)),
        SubTask(id="t2", goal="g2", done_criteria=("c2",)),
    )
    state = _state(plan, agent, implement_retry={"ids": ["t2"]})
    entry = _impl(state)
    # Only t2 was re-invoked; t1 (done) was not.
    assert len(agent.calls) == 1
    assert "`t2`" in agent.calls[0]
    assert "`t1`" not in agent.calls[0]
    assert entry["delivery_clean"] is True
    assert entry["meta"]["subtask_count"] == 1


def test_retry_mode_renders_operator_banner(capsys) -> None:
    from agents.stream import set_stdout_echo
    from core.io.ansi import strip_ansi

    agent = _Dev(close=True)
    plan = _plan(
        SubTask(id="t1", goal="g1", done_criteria=("c1",)),
        SubTask(id="t2", goal="g2", done_criteria=("c2",)),
    )
    state = _state(
        plan,
        agent,
        implement_retry={
            "incomplete_ids": ["t2"],
            "feedback": "finish the build attestation",
            "prior_context": {"t1": {"attestation_summary": "done t1"}},
        },
    )

    set_stdout_echo(True)
    try:
        _impl(state)
    finally:
        set_stdout_echo(False)

    plain = strip_ansi(capsys.readouterr().out)
    assert "ORCHO implement retry: re-running incomplete subtasks" in plain
    assert "mode: retry_feedback" in plain
    assert "retry_subtasks: t2" in plain
    assert "scheduled_subtasks: 1" in plain
    assert "prior_done_context: 1" in plain
    assert "operator_feedback: finish the build attestation" in plain
    assert plain.index("ORCHO implement retry") < plain.index(
        "ORCHO subtask 1/1 START: t2"
    )


class _ContDev(_Dev):
    """``_Dev`` that also records the ``continue_session`` per invoke."""
    def __init__(self) -> None:
        super().__init__(close=True)
        self.continue_flags: list[bool] = []

    def invoke(self, prompt, cwd, *, continue_session=False, attachments=(),
               mutates_artifacts=False) -> str:
        self.continue_flags.append(continue_session)
        return super().invoke(
            prompt, cwd, continue_session=continue_session,
            attachments=attachments, mutates_artifacts=mutates_artifacts,
        )


def test_fresh_subtask_is_fresh_and_carries_handoff(tmp_path: Path) -> None:
    # ADR 0113: a subtask with no same-write-zone predecessor (no seeded
    # session) is FRESH under the implement role, and its compact handoff
    # (goal + execution-plan context, plan contract) rides the per-subtask
    # prompt — no amnesia despite the fresh session.
    agent = _ContDev()
    plan = _plan(SubTask(id="t1", goal="build-the-widget", done_criteria=("c1",)))
    state = _state(plan, agent, tmp_path=tmp_path)
    _impl(state)
    assert agent.continue_flags == [False]
    prompt = agent.calls[0]
    assert "build-the-widget" in prompt
    assert "Current Executable Subtask" in prompt


class _SeededDev(_ContDev):
    """``_ContDev`` whose invoke captures a live provider session id.

    The first invoke commits ``session_id`` into the shared prompt-session
    state, so a later same-write-zone subtask on this agent can resume it.
    """
    def __init__(self) -> None:
        super().__init__()
        self.session_id = "sess-implement-seed"


def test_subtask_invocation_role_classifies_zone() -> None:
    # ADR 0113 (F2): the per-agent seeded zone classifier. No seed yet → the
    # subtask seeds it as IMPLEMENT; an overlapping zone is a same-write-zone
    # implement follow-on (IMPLEMENT). Fresh-by-default: a disjoint zone OR an
    # undeclared (empty) seed/current zone cannot DEMONSTRATE same-write-zone,
    # so it is new/companion work (COMPANION → policy forces FRESH).
    from pipeline.phases.builtin.subtask_dag import _subtask_invocation_role
    from pipeline.runtime.roles import SessionInvocationRole

    seed = SubTask(id="t1", goal="g", owned_files=("a.py",))
    assert _subtask_invocation_role(None, seed) is SessionInvocationRole.IMPLEMENT
    same_zone = SubTask(id="t2", goal="g", owned_files=("a.py",))
    assert (
        _subtask_invocation_role(frozenset({"a.py"}), same_zone)
        is SessionInvocationRole.IMPLEMENT
    )
    new_zone = SubTask(id="t3", goal="g", owned_files=("z.py",))
    assert (
        _subtask_invocation_role(frozenset({"a.py"}), new_zone)
        is SessionInvocationRole.COMPANION
    )
    # Undeclared current zone cannot demonstrate same-write-zone → fresh-by-
    # default → COMPANION (NOT a silently-continued implement follow-on).
    undeclared = SubTask(id="t4", goal="g")
    assert (
        _subtask_invocation_role(frozenset({"a.py"}), undeclared)
        is SessionInvocationRole.COMPANION
    )
    # Undeclared (empty) seed zone also cannot prove same-write-zone → COMPANION.
    assert (
        _subtask_invocation_role(frozenset(), new_zone)
        is SessionInvocationRole.COMPANION
    )


def test_subtask_invocation_role_glob_overlap() -> None:
    # ADR 0113 (F2): same-write-zone overlap honours glob semantics in both
    # directions, so a declared ``owned_files`` glob and a concrete path are
    # matched rather than treated as disjoint by a bare set-intersection.
    from pipeline.phases.builtin.subtask_dag import _subtask_invocation_role
    from pipeline.runtime.roles import SessionInvocationRole

    # 'src/**' (seed glob) covers 'src/foo.py' (current path) → same zone.
    covered = SubTask(id="t2", goal="g", owned_files=("src/foo.py",))
    assert (
        _subtask_invocation_role(frozenset({"src/**"}), covered)
        is SessionInvocationRole.IMPLEMENT
    )
    # Reverse direction: seed path under a current glob also overlaps.
    glob_current = SubTask(id="t3", goal="g", owned_files=("src/**",))
    assert (
        _subtask_invocation_role(frozenset({"src/foo.py"}), glob_current)
        is SessionInvocationRole.IMPLEMENT
    )
    # A glob that does not cover the other zone stays disjoint → COMPANION.
    elsewhere = SubTask(id="t4", goal="g", owned_files=("lib/bar.py",))
    assert (
        _subtask_invocation_role(frozenset({"src/**"}), elsewhere)
        is SessionInvocationRole.COMPANION
    )


def test_companion_subtask_after_seeded_implement_is_fresh(tmp_path: Path) -> None:
    # ADR 0113 (F2): a seeded implement session (t1) is resumed by a same-zone
    # follow-on (t2 → CONTINUE), but a new-zone/companion subtask reusing the
    # SAME agent must NOT drag that transcript in — it goes FRESH (t3) even
    # though the stored session id is still live. depends_on forces the order
    # t1 → t2 → t3 so the seed exists before t2/t3 run.
    agent = _SeededDev()
    plan = _plan(
        SubTask(id="t1", goal="g1", done_criteria=("c1",), owned_files=("a.py",)),
        SubTask(id="t2", goal="g2", done_criteria=("c2",),
                owned_files=("a.py",), depends_on=("t1",)),
        SubTask(id="t3", goal="g3", done_criteria=("c3",),
                owned_files=("z.py",), depends_on=("t2",)),
    )
    state = _state(plan, agent, tmp_path=tmp_path)
    _impl(state)
    # t1 seeds FRESH; t2 (same zone) resumes; t3 (new zone) is COMPANION → FRESH.
    assert agent.continue_flags == [False, True, False]


class _ProviderSessionDev(_ContDev):
    """Runtime that mints a new provider session on every FRESH invoke and
    keeps the current one on a RESUME.

    Lets a test prove *which* session a same-write-zone follow-on actually
    resumed: ``resumed_ids[i]`` is the live ``session_id`` at the start of
    invoke ``i`` when it continued, else ``None`` for a fresh invoke. Orcho
    aligns ``session_id`` to the stored same-zone session just before a resume,
    so the recorded id is the one the runtime would ``--resume``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.session_id = None
        self._minted = 0
        self.resumed_ids: list[str | None] = []

    def invoke(self, prompt, cwd, *, continue_session=False, attachments=(),
               mutates_artifacts=False) -> str:
        if continue_session:
            self.resumed_ids.append(self.session_id)
        else:
            self._minted += 1
            self.session_id = f"sess-{self._minted}"
            self.resumed_ids.append(None)
        return super().invoke(
            prompt, cwd, continue_session=continue_session,
            attachments=attachments, mutates_artifacts=mutates_artifacts,
        )


def test_companion_does_not_pollute_implement_session_followon(
    tmp_path: Path,
) -> None:
    # ADR 0113 (F1): t1 writes a.py and seeds the implement session; t2 writes
    # z.py and goes FRESH as a companion on the SAME reused agent; t3 writes
    # a.py again. t3 must NOT continue t2's companion transcript — it resumes
    # the ORIGINAL a.py implement session t1 seeded. The companion fresh invoke
    # must not have overwritten the shared implement session slot.
    agent = _ProviderSessionDev()
    plan = _plan(
        SubTask(id="t1", goal="g1", done_criteria=("c1",), owned_files=("a.py",)),
        SubTask(id="t2", goal="g2", done_criteria=("c2",),
                owned_files=("z.py",), depends_on=("t1",)),
        SubTask(id="t3", goal="g3", done_criteria=("c3",),
                owned_files=("a.py",), depends_on=("t2",)),
    )
    state = _state(plan, agent, tmp_path=tmp_path)
    _impl(state)
    # t1 fresh (mints sess-1), t2 companion fresh (mints sess-2), t3 same-zone
    # resumes — and it resumes sess-1 (t1's a.py session), never sess-2.
    assert agent.continue_flags == [False, False, True]
    assert agent.resumed_ids == [None, None, "sess-1"]
