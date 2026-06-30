"""Regression tests for the Codex review findings."""
from __future__ import annotations

from pathlib import Path

from agents.protocols import SessionMode
from agents.registry import PhaseAgentConfig
from core.observability.metrics import MetricsCollector
from pipeline.plugins import PluginConfig
from pipeline.project.profile_dispatch import (
    dispatch_via_v2_profile as _dispatch_via_v2_profile,
)
from pipeline.project.run import _PipelineRun
from pipeline.runtime import (
    LoopStep,
    PhaseHandoffPolicy,
    PhaseHandoffType,
    PhaseRegistry,
    PhaseStep,
    PipelineState,
    Profile,
    run_profile,
)
from pipeline.session_adapters import SessionAdapterRegistry


class _FakeAgent:
    model = "fake-model"

    def label(self) -> str:
        return "FakeAgent"


class _FakeProvider:
    def run_tests(self, cwd: str, plugin: PluginConfig):
        return None


def _phase_config() -> PhaseAgentConfig:
    agent = _FakeAgent()
    return PhaseAgentConfig(
        plan_agent=agent,
        implement_agent=agent,
        repair_changes_agent=agent,
        repair_escalation_agent=agent,
        validate_plan_agent=agent,
        review_changes_agent=agent,
        final_acceptance_agent=agent,
    )


def _make_run(
    tmp_path: Path,
    *,
    registry: PhaseRegistry,
    state: PipelineState | None = None,
    dry_run: bool = True,
) -> _PipelineRun:
    pc = _phase_config()
    if state is None:
        state = PipelineState(
            task="t",
            project_dir=str(tmp_path),
            plugin=PluginConfig(),
            phase_config=pc,
            dry_run=dry_run,
        )
    else:
        state.phase_config = pc
    return _PipelineRun(
        task="t",
        project_path=tmp_path,
        git_cwd=str(tmp_path),
        plugin=PluginConfig(),
        output_dir=None,
        dry_run=dry_run,
        profile_name="advanced",
        session_mode=SessionMode.STATELESS,
        max_rounds=1,
        plan_model="m",
        implement_model="m",
        repair_model="m",
        repair_escalation_model="m",
        review_model="m",
        do_plan=False,
        do_build=True,
        do_review=True,
        _provider=_FakeProvider(),
        phase_config=pc,
        state=state,
        registry=registry,
        session={"phases": {}},
        session_ts="test",
        codemap="",
        _metrics=MetricsCollector(plan_model="m", implement_model="m", review_model="m"),
        _ckpt=None,
        _chain_same_model_only=True,
    )


def test_fix_adapter_uses_repair_round_when_plan_round_is_stale(tmp_path: Path) -> None:
    """A PLAN loop can finish on round 2, then REVIEW/FIX starts at round 1.

 `_on_phase_end` must pass `repair_round`, not the stale `plan_round`, to
 RoundAdapter/custom fix adapters.
 """
    reg = PhaseRegistry()

    def plan(state: PipelineState) -> PipelineState:
        state.phase_log["plan"] = {"output": "plan"}
        return state

    def validate_plan(state: PipelineState) -> PipelineState:
        approved = int(state.extras.get("plan_round", 0)) >= 2
        state.phase_log["validate_plan"] = {"approved": approved, "output": "qa"}
        return state

    def review(state: PipelineState) -> PipelineState:
        state.last_critique = "needs fix"
        state.phase_log["review_changes"] = {"output": "needs fix", "clean": False}
        state.phase_log["rounds_pending"] = {"critique": "needs fix"}
        return state

    def fix(state: PipelineState) -> PipelineState:
        state.phase_log["repair_changes"] = {"output": "fixed"}
        state.phase_log["rounds_pending"] = {
            "critique": "needs fix",
            "repair_output": "fixed",
        }
        return state

    for name, handler in (
        ("plan", plan),
        ("validate_plan", validate_plan),
        ("review_changes", review),
        ("repair_changes", fix),
    ):
        reg.register(name, handler)

    captured_rounds: list[int | None] = []

    class CaptureFixAdapter:
        def write(self, phase_name, state, session, *, round_n=None):
            captured_rounds.append(round_n)

    adapters = SessionAdapterRegistry()
    adapters.register("repair_changes", CaptureFixAdapter())

    run = _make_run(tmp_path, registry=reg, dry_run=True)
    run._session_adapters = adapters
    run.state.extras["_v2_dispatch_active"] = True

    profile = Profile(
        name="two-loops",
        kind="custom",
        steps=(
            LoopStep(
                steps=(PhaseStep(phase="plan"), PhaseStep(phase="validate_plan")),
                until="validate_plan.approved",
                max_rounds=2,
                round_extras_key="plan_round",
            ),
            LoopStep(
                steps=(PhaseStep(phase="review_changes"), PhaseStep(phase="repair_changes")),
                until="review_changes.clean",
                max_rounds=1,
                round_extras_key="repair_round",
            ),
        ),
    )

    #  adapter fires via FSM stage 8 — pass ctx
    # with session_adapter_registry. Pre-substep-4 the test relied on
    # ``_on_phase_end`` to fire the adapter; that responsibility moved
    # to FSM ctx.
    from pipeline.lifecycle import default_lifecycle_context
    ctx = default_lifecycle_context(
        phase_registry=reg,
        session_adapter_registry=adapters,
        run_config={"session": run.session},
    )
    run_profile(
        profile,
        run.state,
        reg,
        on_phase_start=run._on_phase_start,
        on_phase_end=run._on_phase_end,
        ctx=ctx,
    )

    assert run.state.extras["plan_round"] == 2
    assert run.state.extras["repair_round"] == 1
    assert captured_rounds == [1]


def test_phase_handoff_pause_returns_awaiting_without_finalize(tmp_path: Path) -> None:
    """A loop-runner-driven phase-handoff pause must early-return.

    The dispatcher reads ``state.phase_handoff_request`` after
    ``run_profile`` returns and calls ``_apply_phase_handoff_pause``,
    which writes ``status=awaiting_phase_handoff`` + the ``phase_handoff``
    payload. ``finalize()`` is skipped so the awaiting state is not
    overwritten by ``done``.
    """
    reg = PhaseRegistry()

    def plan(state: PipelineState) -> PipelineState:
        state.phase_log["plan"] = {"output": "PLAN MD"}
        return state

    def validate_plan(state: PipelineState) -> PipelineState:
        # Reviewer rejects the plan on every round.
        state.phase_log["validate_plan"] = {
            "approved": False,
            "verdict": "REJECTED",
            "critique": "missing detail",
        }
        return state

    reg.register("plan", plan)
    reg.register("validate_plan", validate_plan)
    run = _make_run(
        tmp_path,
        registry=reg,
        dry_run=True,
    )
    profile = Profile(
        name="plan-loop-with-handoff",
        kind="custom",
        steps=(
            LoopStep(
                steps=(
                    PhaseStep(phase="plan"),
                    PhaseStep(
                        phase="validate_plan",
                        handoff=PhaseHandoffPolicy(
                            type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                        ),
                    ),
                ),
                until="validate_plan.approved",
                max_rounds=1,
                round_extras_key="plan_round",
            ),
        ),
    )

    result = _dispatch_via_v2_profile(run, profile)

    assert result["status"] == "awaiting_phase_handoff"
    handoff = result["phase_handoff"]
    assert handoff["phase"] == "validate_plan"
    assert handoff["trigger"] == "rejected"
    assert handoff["last_output"] == "missing detail"
    assert "metrics" not in result


def test_atexit_hook_stamps_halt_reason_on_interrupted_run(
    tmp_path: Path, monkeypatch,
) -> None:
    """The graceful-exit atexit hook in ``_init_session_with_atexit``
    flips ``status="running" → "interrupted"`` when the subprocess exits
    without reaching finalize (SIGTERM, KeyboardInterrupt, unhandled
    exception, parent-process death). Beyond the status flip, it must
    also stamp ``halt_reason`` so downstream consumers (SDK resume-gate,
    MCP wire, dashboards) that key off ``meta.halt_reason`` see something
    for this class of terminations — previously they got ``None``.
    """
    from pipeline.plugins import PluginConfig
    from pipeline.project.bootstrap import (
        init_session_with_atexit as _init_session_with_atexit,
    )

    # Capture the atexit hook closure instead of letting it run at
    # interpreter exit — the test controls when it fires.
    captured: list = []
    import atexit as _atexit
    monkeypatch.setattr(_atexit, "register", lambda fn: captured.append(fn))

    run_dir = tmp_path / "runs" / "interrupted_probe"
    run_dir.mkdir(parents=True)
    session = _init_session_with_atexit(
        task="probe",
        project_path=tmp_path,
        plugin=PluginConfig(name="Project"),
        model="claude-opus-4-7",
        profile_name="advanced",
        session_mode=SessionMode.STATELESS,
        change_handoff="uncommitted",
        output_dir=run_dir,
    )
    assert session["status"] == "running"
    assert "halt_reason" not in session  # no premature stamp
    assert captured, "atexit hook was not registered"

    # Simulate abnormal exit: pipeline never reached finalize, status
    # is still ``running`` when the hook fires.
    captured[0]()

    # Status flipped + halt_reason stamped.
    assert session["status"] == "interrupted"
    assert session.get("halt_reason") == "interrupted"
    # On-disk meta.json mirrors the in-memory session.
    import json
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["status"] == "interrupted"
    assert meta["halt_reason"] == "interrupted"


def test_atexit_hook_noop_when_status_already_terminal(
    tmp_path: Path, monkeypatch,
) -> None:
    """The hook only acts on ``status="running"``. If finalize already
    flipped the session to ``done`` / ``halted`` / ``awaiting_phase_handoff``,
    a subsequent atexit firing must not retroactively stamp
    ``halt_reason="interrupted"`` over the real terminal state.
    """
    from pipeline.plugins import PluginConfig
    from pipeline.project.bootstrap import (
        init_session_with_atexit as _init_session_with_atexit,
    )

    captured: list = []
    import atexit as _atexit
    monkeypatch.setattr(_atexit, "register", lambda fn: captured.append(fn))

    run_dir = tmp_path / "runs" / "done_probe"
    run_dir.mkdir(parents=True)
    session = _init_session_with_atexit(
        task="probe",
        project_path=tmp_path,
        plugin=PluginConfig(name="Project"),
        model="claude-opus-4-7",
        profile_name="advanced",
        session_mode=SessionMode.STATELESS,
        change_handoff="uncommitted",
        output_dir=run_dir,
    )
    # Simulate finalize having already set status (the normal path).
    session["status"] = "done"

    captured[0]()

    assert session["status"] == "done"
    assert "halt_reason" not in session
    assert "interrupted_at" not in session


def test_record_phase_failure_stamps_top_level_halt_reason(
    tmp_path: Path,
) -> None:
    """ADR 0035 invariant: every non-``done`` terminal status carries a
    non-null ``meta.halt_reason``. Before this fix, ``_record_phase_failure``
    set ``status="failed"`` and a structured ``failure`` block but left
    ``halt_reason=None`` — every downstream consumer keying off
    ``meta.halt_reason`` (SDK resume-gate, MCP wire, dashboards) saw
    null on ``failed`` runs even though the cause was right there in
    ``failure.error`` / ``failure.type``.

    The fix stamps ``halt_reason="phase_failure:<ExceptionClass>"``
    while keeping the existing ``failure`` block intact for full
    diagnostic detail.
    """
    reg = PhaseRegistry()
    reg.register("plan", lambda s: s)
    run = _make_run(tmp_path, registry=reg, dry_run=True)
    run.session["status"] = "running"

    class _BoomError(ValueError):
        pass

    run._record_phase_failure(_BoomError("kaboom"), fallback_phase="implement")

    assert run.session["status"] == "failed"
    # Top-level halt_reason — ADR 0035 invariant.
    assert run.session.get("halt_reason") == "phase_failure:_BoomError"
    # Structured failure block preserved for full diagnostic detail.
    assert run.session["failure"]["phase"] == "implement"
    assert run.session["failure"]["type"] == "_BoomError"
    assert "kaboom" in run.session["failure"]["error"]


def test_record_phase_failure_is_idempotent_on_repeat(
    tmp_path: Path,
) -> None:
    """Second call no-ops — the first failure capture wins. Asserts the
    existing idempotency guard at line 1088 still holds after the
    halt_reason addition.
    """
    reg = PhaseRegistry()
    reg.register("plan", lambda s: s)
    run = _make_run(tmp_path, registry=reg, dry_run=True)
    run.session["status"] = "running"

    run._record_phase_failure(ValueError("first"), fallback_phase="implement")
    first_reason = run.session["halt_reason"]
    first_error = run.session["failure"]["error"]

    run._record_phase_failure(RuntimeError("second"), fallback_phase="repair")

    # Idempotent — first wins on both halt_reason and failure block.
    assert run.session["halt_reason"] == first_reason
    assert run.session["failure"]["error"] == first_error
    assert run.session["failure"]["type"] == "ValueError"
