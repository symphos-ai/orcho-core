"""Regression for the run 20260609_125615 shape (synthetic — no retained run).

That run halted mid-IMPLEMENT: subtasks T1–T4 finished and attested
(``subtask.start`` + ``subtask.end ok=True``), T5 started but never produced a
DONE/ATTESTATION (``subtask.start`` with no ``subtask.end``), the IMPLEMENT
phase emitted ``phase.end outcome='halted: ...'`` and the run ended halted.
Before the fix the halted IMPLEMENT was projected/checkpointed as completed and
resume silently skipped it straight to review.

This pins all four projections against synthetic events of that exact shape —
it never reads or mutates ``runspace/runs/20260609_125615``:

  (a) reducer            — IMPLEMENT is neither completed nor failed; run HALTED.
  (b) checkpoint/resume  — 'implement' not in ckpt.completed; should_skip False;
                           genuinely completed phases skip on resume.
  (c) summary            — the DONE chip for the halted phase reads 'halt'.
  (d) partial-diagnostic — the detector flags T5 and the subtask_dag resume
                           guard stops with the instructive message.
"""
from __future__ import annotations

from types import SimpleNamespace

from agents.entities import SubTask
from agents.registry import AgentRegistry
from core.observability.events import append_event
from pipeline.checkpoint import CheckpointStore
from pipeline.lifecycle import PhaseLifecycle, default_lifecycle_context
from pipeline.phases.builtin.subtask_dag import _run_subtask_dag_implement
from pipeline.plan_parser import ParsedPlan
from pipeline.plugins import PluginConfig
from pipeline.project.finalization import _render_done_summary
from pipeline.run_state import (
    RunStateSnapshot,
    RunStatus,
    apply_run_event,
    unfinished_subtask_ids,
)
from pipeline.runtime import PhaseRegistry, PhaseStep, PipelineState

_HALT_OUTCOME = "halted: subtask T5 stalled"
_DONE_SUBTASKS = ("T1", "T2", "T3", "T4")
_UNFINISHED_SUBTASK = "T5"

_EXPECTED_DIAGNOSTIC = (
    "Cannot resume IMPLEMENT from partial subtask DAG state: subtask T5 "
    "started but has no DONE/ATTESTATION event. Start a follow-up or rerun "
    "implement after repair."
)


def _event(kind: str, seq: int, **payload: object) -> dict:
    return {"seq": seq, "ts": "t", "kind": kind, "phase": None, "payload": payload}


def _synthetic_events() -> list[dict]:
    """The 20260609_125615 event stream: plan/validate ok, IMPLEMENT halted with
    T1–T4 done+attested and T5 started-but-never-finished, run.end halted."""
    events: list[dict] = [
        _event("run.start", 1, task="t"),
        _event("phase.start", 2, title="plan"),
        _event("phase.end", 3, title="plan", outcome="ok"),
        _event("phase.start", 4, title="validate_plan"),
        _event("phase.end", 5, title="validate_plan", outcome="ok"),
        _event("phase.start", 6, title="implement"),
    ]
    seq = 7
    for sid in _DONE_SUBTASKS:
        events.append(_event("subtask.start", seq, subtask_id=sid))
        seq += 1
        events.append(_event("subtask.end", seq, subtask_id=sid, ok=True))
        seq += 1
    # T5 starts but never reaches a DONE/ATTESTATION terminal.
    events.append(_event("subtask.start", seq, subtask_id=_UNFINISHED_SUBTASK))
    seq += 1
    events.append(_event("phase.end", seq, title="implement", outcome=_HALT_OUTCOME))
    seq += 1
    events.append(_event("run.end", seq, status="halted"))
    return events


# ── (a) reducer projection ─────────────────────────────────────────────────


def test_reducer_halted_implement_not_completed_run_halted() -> None:
    snapshot = RunStateSnapshot.initial()
    for event in _synthetic_events():
        snapshot = apply_run_event(snapshot, event)

    assert "implement" not in snapshot.completed_phases
    assert "implement" not in snapshot.failed_phases
    assert "implement" in snapshot.seen_phases
    # The genuinely completed phases still project as completed.
    assert "plan" in snapshot.completed_phases
    assert "validate_plan" in snapshot.completed_phases
    assert snapshot.status is RunStatus.HALTED
    assert snapshot.terminal is True


# ── (b) checkpoint / resume projection ─────────────────────────────────────


def _drive_phase(lifecycle: PhaseLifecycle, name: str, state: PipelineState,
                 ctx) -> None:
    lifecycle.execute_step(PhaseStep(phase=name), state, ctx)


def test_checkpoint_resume_skips_completed_not_halted_implement() -> None:
    ckpt = CheckpointStore(run_id="syn-20260609_125615")

    reg = PhaseRegistry()
    reg.register("plan", lambda s: s)
    reg.register("validate_plan", lambda s: s)
    # IMPLEMENT halts mid-DAG (T5 never finished).
    reg.register("implement", lambda s: (s.stop(_HALT_OUTCOME) or s))

    ctx = default_lifecycle_context(phase_registry=reg)
    ctx.on_checkpoint = lambda name, st: ckpt.save_phase(name, {"ok": True})

    lifecycle = PhaseLifecycle()
    state = PipelineState(task="t", project_dir="/p", plugin=PluginConfig())
    for phase in ("plan", "validate_plan", "implement"):
        _drive_phase(lifecycle, phase, state, ctx)

    loaded = ckpt.load()
    assert "implement" not in loaded.completed
    assert loaded.should_skip("implement") is False
    # Genuinely completed phases are checkpointed and skip on resume.
    assert loaded.should_skip("plan") is True
    assert loaded.should_skip("validate_plan") is True


# ── (c) summary projection ─────────────────────────────────────────────────


def test_summary_renders_halt_chip_for_halted_implement() -> None:
    profile = SimpleNamespace(
        steps=(
            PhaseStep(phase="plan"),
            PhaseStep(phase="validate_plan"),
            PhaseStep(phase="implement"),
            PhaseStep(phase="review_changes"),
        ),
    )
    phase_log = {
        "plan": {"output": "planned"},
        "validate_plan": {"output": "approved"},
        "implement": {"output": "partial: T1-T4 done, T5 stalled"},
    }

    summary = _render_done_summary(profile, phase_log, halted_phase="implement")

    assert "implement=halt" in summary
    assert summary == (
        "plan=ok | validate_plan=ok | implement=halt | review_changes=skip"
    )


# ── (d) partial-subtask diagnostic projection ──────────────────────────────


def test_detector_flags_unfinished_t5() -> None:
    assert unfinished_subtask_ids(_synthetic_events()) == {_UNFINISHED_SUBTASK}


def _plan_with_all_subtasks() -> ParsedPlan:
    subtasks = tuple(
        SubTask(id=sid, goal=f"goal {sid}")
        for sid in (*_DONE_SUBTASKS, _UNFINISHED_SUBTASK)
    )
    return ParsedPlan(
        short_summary="p", planning_context="p", subtasks=subtasks, source="test",
    )


def test_subtask_dag_resume_halts_with_diagnostic(tmp_path, monkeypatch) -> None:
    class _ReachedDag(Exception):
        pass

    monkeypatch.setattr(
        "pipeline.dag_runner.run_dag_sequential",
        lambda *a, **k: (_ for _ in ()).throw(_ReachedDag()),
    )
    # Replay the subtask portion of the fixture into a real events.jsonl.
    for sid in _DONE_SUBTASKS:
        append_event(tmp_path, "subtask.start", {"subtask_id": sid})
        append_event(tmp_path, "subtask.end", {"subtask_id": sid, "ok": True})
    append_event(tmp_path, "subtask.start", {"subtask_id": _UNFINISHED_SUBTASK})

    agent = SimpleNamespace(runtime="claude", model="claude-opus-4-7")
    registry = AgentRegistry()
    registry.register("claude", lambda model, _effort=None: agent)
    state = PipelineState(
        task="t", project_dir=str(tmp_path), plugin=PluginConfig(),
        parsed_plan=_plan_with_all_subtasks(), registry=registry,
        output_dir=tmp_path,
        extras={"implementation_execution": "subtask_dag"},
    )

    entry = _run_subtask_dag_implement(state, agent, None)

    assert state.halt is True
    assert state.halt_reason == _EXPECTED_DIAGNOSTIC
    assert entry["delivery_clean"] is False
