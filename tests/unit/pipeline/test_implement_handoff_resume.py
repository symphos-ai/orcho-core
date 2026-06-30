"""Resume-arm tests for the implement-phase handoff (ADR 0073, T10).

Covers ``apply_phase_handoff_resume`` for an ``implement`` active handoff:
accept (continue / continue_with_waiver), bare-continue waiver synthesis,
retry_feedback (incomplete-id seed + parsed-plan rehydrate), halt terminality,
and the ``profile_dispatch`` completed_phases UNION (so an accepted implement
is not re-executed after a checkpoint reload).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pipeline.plugins import PluginConfig
from pipeline.runtime import PhaseStep, PipelineState, Profile


def _seed_decision(run_dir, *, handoff_id, action, feedback=None, note=None,
                   decided_at="2026-06-04T12:00:00+00:00") -> None:
    from sdk.phase_handoff import safe_handoff_id

    decisions = run_dir / "phase_handoff_decisions"
    decisions.mkdir(parents=True, exist_ok=True)
    (decisions / f"{safe_handoff_id(handoff_id)}.json").write_text(
        json.dumps({
            "run_id":     run_dir.name,
            "handoff_id": handoff_id,
            "phase":      "implement",
            "action":     action,
            "feedback":   feedback,
            "note":       note,
            "decided_at": decided_at,
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


_HANDOFF_ID = "implement:implement_handoff:1"


def _profile() -> Profile:
    return Profile(
        name="advanced", kind="advanced", description="implement step",
        steps=(PhaseStep(phase="implement"),),
    )


def _run(run_dir, state, *, impl_entry=None):
    handoff = {
        "id": _HANDOFF_ID,
        "phase": "implement",
        "round": 1,
        "loop_max_rounds": 1,
        "artifacts": {
            "findings": ["t2 incomplete"],
            "incomplete_subtasks": ["t2"],
            "attestation_incomplete": {"t2": "criteria not closed"},
        },
        "last_output": "build log",
    }
    phases = {}
    if impl_entry is not None:
        phases["implement"] = impl_entry
    run = SimpleNamespace(
        output_dir=run_dir,
        session={
            "status": "awaiting_phase_handoff",
            "phases": phases,
            "phase_handoff": handoff,
        },
        _ckpt=None,
        _metrics=None,
        state=state,
    )
    return run


def _state(tmp_path):
    return PipelineState(task="t", project_dir=str(tmp_path), plugin=PluginConfig())


# ── accept: continue_with_waiver ───────────────────────────────────────────

def test_continue_with_waiver_marks_implement_completed_and_waived(tmp_path):
    from pipeline.project.handoff import apply_phase_handoff_resume

    run_dir = tmp_path / "20260604_120000_impl"
    run_dir.mkdir()
    _seed_decision(run_dir, handoff_id=_HANDOFF_ID,
                   action="continue_with_waiver",
                   feedback="accepted: ship with t2 stub")
    state = _state(tmp_path)
    run = _run(run_dir, state, impl_entry={
        "output": "build", "delivery_status": "incomplete",
        "delivery_clean": False,
    })

    outcome = apply_phase_handoff_resume(run, _profile(), None)

    assert outcome.paused is False
    assert outcome.completed_phases == frozenset({"implement"})
    assert outcome.profile is not None  # profile unchanged (no loop to strip)
    # Waiver applied to state.extras AND synced to session.
    waiver = state.extras["phase_handoff_waiver"]
    assert waiver["decided_by"] == "operator"
    assert waiver["waiver_text"] == "accepted: ship with t2 stub"
    assert run.session["phase_handoff_waiver"] == waiver
    # Persisted implement entry rewritten incomplete → waived.
    impl = run.session["phases"]["implement"]
    assert impl["delivery_status"] == "waived"
    assert impl["delivery_waived"] is True
    assert impl["waiver_id"] == _HANDOFF_ID
    # ADR 0073: the operator action is stamped onto the implement entry so the
    # evidence breadcrumb can distinguish a waiver from a bare continue.
    assert impl["action"] == "continue_with_waiver"
    override = state.extras["phase_handoff_override"]
    assert override["action"] == "continue_with_waiver"
    assert run.session["status"] == "running"
    assert "phase_handoff" not in run.session


def test_continue_with_waiver_requires_feedback(tmp_path):
    from pipeline.project.handoff import apply_phase_handoff_resume

    run_dir = tmp_path / "20260604_120100_impl"
    run_dir.mkdir()
    _seed_decision(run_dir, handoff_id=_HANDOFF_ID,
                   action="continue_with_waiver", feedback="   ")
    run = _run(run_dir, _state(tmp_path))
    with pytest.raises(RuntimeError, match="operator verdict"):
        apply_phase_handoff_resume(run, _profile(), None)


# ── accept: bare continue (§4d synthesis) ──────────────────────────────────

def test_bare_continue_synthesizes_waiver_text(tmp_path):
    from pipeline.project.handoff import apply_phase_handoff_resume

    run_dir = tmp_path / "20260604_120200_impl"
    run_dir.mkdir()
    _seed_decision(run_dir, handoff_id=_HANDOFF_ID, action="continue",
                   note="operator override")
    state = _state(tmp_path)
    run = _run(run_dir, state, impl_entry={
        "output": "build", "delivery_status": "incomplete",
    })

    outcome = apply_phase_handoff_resume(run, _profile(), None)

    assert outcome.completed_phases == frozenset({"implement"})
    waiver = state.extras["phase_handoff_waiver"]
    assert waiver["decided_by"] == "operator"
    # Synthesized from findings + appended note.
    assert "Operator continued without explicit waiver feedback" in waiver["waiver_text"]
    assert "t2 incomplete" in waiver["waiver_text"]
    assert "operator override" in waiver["waiver_text"]
    override = state.extras["phase_handoff_override"]
    assert override["action"] == "continue"  # NOT continue_with_waiver
    assert override["feedback"] is None
    impl = run.session["phases"]["implement"]
    assert impl["delivery_status"] == "waived"
    assert impl["action"] == "continue"  # distinguishes bare continue from waiver


# ── retry_feedback ─────────────────────────────────────────────────────────

def test_retry_feedback_seeds_incomplete_ids_and_rehydrates(tmp_path):
    from pipeline.plan_artifacts import write_parsed_plan_artifact
    from pipeline.project.handoff import apply_phase_handoff_resume

    run_dir = tmp_path / "20260604_120300_impl"
    run_dir.mkdir()
    # Persisted plan on disk; in-memory state.parsed_plan is None (cold resume).
    from agents.entities import SubTask
    from pipeline.plan_parser import ParsedPlan
    plan = ParsedPlan(
        short_summary="s", planning_context="c",
        subtasks=(SubTask(id="t1", goal="g1"),
                  SubTask(id="t2", goal="g2", depends_on=("t1",))),
        source="json",
    )
    write_parsed_plan_artifact(run_dir, plan, attempt=1)

    _seed_decision(run_dir, handoff_id=_HANDOFF_ID, action="retry_feedback",
                   feedback="please finish t2's criteria")
    state = _state(tmp_path)
    assert state.parsed_plan is None
    run = _run(run_dir, state)

    outcome = apply_phase_handoff_resume(run, _profile(), None)

    assert outcome.paused is False
    assert outcome.completed_phases == frozenset()  # implement re-runs
    assert outcome.invalidated_phases == frozenset({"implement"})
    retry = state.extras["implement_retry"]
    assert retry["incomplete_ids"] == ["t2"]
    assert retry["feedback"] == "please finish t2's criteria"
    # parsed_plan rehydrated from disk for the cold-process retry.
    assert state.parsed_plan is not None
    assert {s.id for s in state.parsed_plan.subtasks} == {"t1", "t2"}


def test_retry_feedback_requires_feedback(tmp_path):
    from pipeline.project.handoff import apply_phase_handoff_resume

    run_dir = tmp_path / "20260604_120400_impl"
    run_dir.mkdir()
    _seed_decision(run_dir, handoff_id=_HANDOFF_ID, action="retry_feedback",
                   feedback="  ")
    run = _run(run_dir, _state(tmp_path))
    with pytest.raises(RuntimeError, match="feedback string"):
        apply_phase_handoff_resume(run, _profile(), None)


# ── halt ───────────────────────────────────────────────────────────────────

def test_halt_on_implement_is_terminal(tmp_path):
    from pipeline.project.handoff import (
        PhaseHandoffHaltedError,
        apply_phase_handoff_resume,
    )

    run_dir = tmp_path / "20260604_120500_impl"
    run_dir.mkdir()
    _seed_decision(run_dir, handoff_id=_HANDOFF_ID, action="halt")
    run = _run(run_dir, _state(tmp_path))
    with pytest.raises(PhaseHandoffHaltedError):
        apply_phase_handoff_resume(run, _profile(), None)


# ── rename: rehydrate_parsed_plan default no-op ────────────────────────────

def test_rehydrate_parsed_plan_noop_when_plan_in_state(tmp_path):
    from pipeline.project.handoff import rehydrate_parsed_plan

    state = _state(tmp_path)
    state.parsed_plan = object()  # already present → no-op
    run = SimpleNamespace(state=state, output_dir=tmp_path)
    assert rehydrate_parsed_plan(run) is False


# ── profile_dispatch completed_phases UNION (§5) ───────────────────────────

def test_completed_phases_union_keeps_implement(tmp_path, monkeypatch):
    """The checkpoint (written before the pause) lists plan/validate_plan but
    NOT implement; the resume outcome contributes implement. The dispatch must
    UNION them so implement is skipped on the resume pass."""
    import pipeline.lifecycle as _lc
    import pipeline.project.handoff as _ho
    import pipeline.runtime as _rt
    from pipeline.project.handoff import PhaseHandoffResumeOutcome
    from pipeline.project.profile_dispatch import dispatch_via_v2_profile

    captured: dict = {}

    prof = _profile()
    monkeypatch.setattr(
        "pipeline.project.profile_dispatch.apply_runtime_max_rounds",
        lambda p, *, max_rounds: p,
    )
    monkeypatch.setattr(_lc, "default_lifecycle_context",
                        lambda **kw: SimpleNamespace(on_checkpoint=None,
                                                     on_metrics=None))
    monkeypatch.setattr(_ho, "apply_phase_handoff_resume",
                        lambda run, profile, ctx, **kw: PhaseHandoffResumeOutcome(
                            profile=prof,
                            completed_phases=frozenset({"implement"}),
                            paused=False,
                        ))
    monkeypatch.setattr(_ho, "process_pending_phase_handoffs",
                        lambda *a, **k: SimpleNamespace(
                            paused=True, halted=False, continue_dispatch=False))

    def _capture_run_profile(profile, state, registry, **kwargs):
        captured["completed_phases"] = set(kwargs.get("completed_phases") or ())

    monkeypatch.setattr(_rt, "run_profile", _capture_run_profile)

    fake_ckpt = SimpleNamespace(
        load=lambda ts: SimpleNamespace(completed={"plan", "validate_plan"}),
    )
    run = SimpleNamespace(
        max_rounds=1,
        session={"phases": {}},
        session_ts="run-ts",
        state=_state(tmp_path),
        registry=None,
        _session_adapters=None,
        _provider=None,
        _fsm_checkpoint=None,
        _fsm_metrics=None,
        _ckpt=fake_ckpt,
        checkpoint_resume=True,  # skip hypothesis block
        do_plan=False,
        hypothesis_enabled=None,
        _presentation=None,
        _on_phase_start=None,
        _on_phase_end=None,
        _dispatch_active=False,
        _done_summary_profile=None,
        finalize=lambda: {"status": "done"},
    )

    dispatch_via_v2_profile(run, prof)

    assert captured["completed_phases"] == {"plan", "validate_plan", "implement"}


def test_retry_feedback_invalidates_checkpoint_completed_downstream(
    tmp_path,
    monkeypatch,
):
    """A retry_feedback decision must not inherit stale completed phases.

    The paused run may have checkpointed ``implement`` and later phases as
    completed before the operator asks for a retry. Resuming with those
    checkpoint entries intact skips the actual retry and jumps to DONE.
    """
    import pipeline.lifecycle as _lc
    import pipeline.project.handoff as _ho
    import pipeline.runtime as _rt
    from pipeline.project.handoff import PhaseHandoffResumeOutcome
    from pipeline.project.profile_dispatch import dispatch_via_v2_profile
    from pipeline.runtime import LoopStep

    captured: dict = {}
    prof = Profile(
        name="advanced",
        kind="advanced",
        description="implement + review + acceptance",
        steps=(
            PhaseStep(phase="implement"),
            LoopStep(
                steps=(
                    PhaseStep(phase="review_changes"),
                    PhaseStep(phase="repair_changes"),
                ),
                until="review_changes.approved",
                max_rounds=1,
                round_extras_key="repair_round",
            ),
            PhaseStep(phase="final_acceptance"),
        ),
    )
    monkeypatch.setattr(
        "pipeline.project.profile_dispatch.apply_runtime_max_rounds",
        lambda p, *, max_rounds: p,
    )
    monkeypatch.setattr(
        _lc,
        "default_lifecycle_context",
        lambda **kw: SimpleNamespace(on_checkpoint=None, on_metrics=None),
    )
    monkeypatch.setattr(
        _ho,
        "apply_phase_handoff_resume",
        lambda run, profile, ctx, **kw: PhaseHandoffResumeOutcome(
            profile=prof,
            completed_phases=frozenset(),
            paused=False,
            invalidated_phases=frozenset({
                "implement",
                "review_changes",
                "repair_changes",
                "final_acceptance",
            }),
        ),
    )

    def _capture_run_profile(profile, state, registry, **kwargs):
        captured["completed_phases"] = set(kwargs.get("completed_phases") or ())

    monkeypatch.setattr(_rt, "run_profile", _capture_run_profile)

    fake_ckpt = SimpleNamespace(
        load=lambda ts: SimpleNamespace(
            completed={
                "plan",
                "validate_plan",
                "implement",
                "review_changes",
                "repair_changes",
                "final_acceptance",
            },
        ),
    )
    run = SimpleNamespace(
        max_rounds=1,
        session={"phases": {}},
        session_ts="run-ts",
        state=_state(tmp_path),
        registry=None,
        _session_adapters=None,
        _provider=None,
        _fsm_checkpoint=None,
        _fsm_metrics=None,
        _ckpt=fake_ckpt,
        checkpoint_resume=True,
        do_plan=False,
        hypothesis_enabled=None,
        _presentation=None,
        _on_phase_start=None,
        _on_phase_end=None,
        _dispatch_active=False,
        _done_summary_profile=None,
        finalize=lambda: {"status": "done"},
    )

    dispatch_via_v2_profile(run, prof)

    assert captured["completed_phases"] == {"plan", "validate_plan"}


def test_interactive_retry_feedback_invalidates_checkpoint_completed_downstream(
    tmp_path,
    monkeypatch,
):
    """The in-process prompt loop must apply retry invalidation too.

    The checkpoint-resume path already subtracts ``invalidated_phases`` from
    stale checkpoint completions. The interactive path records a decision and
    redispatches in the same process; it must use the same rule or an implement
    retry skips the failed subtasks and jumps to DONE.
    """
    import pipeline.project.handoff as _ho
    import pipeline.runtime as _rt
    import sdk.phase_handoff as _sdk_handoff
    from pipeline.project.handoff import (
        PhaseHandoffResumeOutcome,
        process_pending_phase_handoffs,
    )
    from pipeline.runtime import LoopStep
    from pipeline.runtime.roles import PhaseHandoffAction

    captured: dict = {}
    prof = Profile(
        name="advanced",
        kind="advanced",
        description="implement + review + acceptance",
        steps=(
            PhaseStep(phase="implement"),
            LoopStep(
                steps=(
                    PhaseStep(phase="review_changes"),
                    PhaseStep(phase="repair_changes"),
                ),
                until="review_changes.approved",
                max_rounds=1,
                round_extras_key="repair_round",
            ),
            PhaseStep(phase="final_acceptance"),
        ),
    )

    monkeypatch.setattr(_ho, "apply_phase_handoff_pause", lambda run: None)
    monkeypatch.setattr(_ho, "should_prompt_for_phase_handoff", lambda **kw: True)
    monkeypatch.setattr(
        _ho,
        "prompt_phase_handoff_action",
        lambda signal, **_kw: SimpleNamespace(
            action=PhaseHandoffAction.RETRY_FEEDBACK.value,
            feedback="fix failed subtasks",
            note=None,
        ),
    )
    monkeypatch.setattr(_sdk_handoff, "phase_handoff_decide", lambda *a, **kw: None)
    monkeypatch.setattr(
        _ho,
        "apply_phase_handoff_resume_with_banners",
        lambda run, profile, ctx, **kw: PhaseHandoffResumeOutcome(
            profile=prof,
            completed_phases=frozenset(),
            paused=False,
            invalidated_phases=frozenset({
                "implement",
                "review_changes",
                "repair_changes",
                "final_acceptance",
            }),
        ),
    )

    def _capture_run_profile(profile, state, registry, **kwargs):
        captured["completed_phases"] = set(kwargs.get("completed_phases") or ())
        state.phase_handoff_request = None

    monkeypatch.setattr(_rt, "run_profile", _capture_run_profile)

    fake_ckpt = SimpleNamespace(
        load=lambda ts: SimpleNamespace(
            completed={
                "plan",
                "validate_plan",
                "implement",
                "review_changes",
                "repair_changes",
                "final_acceptance",
            },
        ),
    )
    state = _state(tmp_path)
    state.phase_handoff_request = SimpleNamespace(handoff_id=_HANDOFF_ID)
    run = SimpleNamespace(
        no_interactive=False,
        output_dir=tmp_path / "20260609_015817",
        session_ts="20260609_015817",
        session={"phases": {}},
        state=state,
        _ckpt=fake_ckpt,
        registry=None,
        _on_phase_start=None,
        _on_phase_end=None,
        _dispatch_active=True,
    )
    run.output_dir.mkdir()

    result = process_pending_phase_handoffs(run, prof, ctx=SimpleNamespace())

    assert result.paused is False
    assert captured["completed_phases"] == {"plan", "validate_plan"}


def test_resume_redispatch_arms_gate_context(monkeypatch, tmp_path) -> None:
    """A resumed run must arm the per-phase gate context before re-dispatch.

    Regression: ``_gate_profile`` / ``_gate_ctx`` were armed only in the fresh
    ``profile_dispatch`` path, never on the ``handoff`` resume re-dispatch. A run
    resumed via an operator phase-handoff (e.g. the validate_plan loop) then
    continued through ``implement`` with the gate hooks inert
    (``_gate_active`` is False without ``_gate_profile``), so its required
    post-implement verification gates never executed, their receipts were never
    materialized, and delivery falsely blocked the (green) run on "missing
    required receipts". The resume path must arm the context like the fresh
    dispatch does.
    """
    import pipeline.project.handoff as _ho
    import pipeline.runtime as _rt
    import sdk.phase_handoff as _sdk_handoff
    from pipeline.project.handoff import (
        PhaseHandoffResumeOutcome,
        process_pending_phase_handoffs,
    )
    from pipeline.runtime.roles import PhaseHandoffAction

    captured: dict = {}
    prof = _profile()
    ctx_sentinel = SimpleNamespace(token="resume-ctx")

    monkeypatch.setattr(_ho, "apply_phase_handoff_pause", lambda run: None)
    monkeypatch.setattr(_ho, "should_prompt_for_phase_handoff", lambda **kw: True)
    monkeypatch.setattr(
        _ho,
        "prompt_phase_handoff_action",
        lambda signal, **_kw: SimpleNamespace(
            action=PhaseHandoffAction.RETRY_FEEDBACK.value,
            feedback="continue",
            note=None,
        ),
    )
    monkeypatch.setattr(_sdk_handoff, "phase_handoff_decide", lambda *a, **kw: None)
    monkeypatch.setattr(
        _ho,
        "apply_phase_handoff_resume_with_banners",
        lambda run, profile, ctx, **kw: PhaseHandoffResumeOutcome(
            profile=prof,
            completed_phases=frozenset(),
            paused=False,
            invalidated_phases=frozenset(),
        ),
    )

    state = _state(tmp_path)
    state.phase_handoff_request = SimpleNamespace(handoff_id=_HANDOFF_ID)
    run = SimpleNamespace(
        no_interactive=False,
        output_dir=tmp_path / "20260627_164749",
        session_ts="20260627_164749",
        session={"phases": {}},
        state=state,
        _ckpt=None,
        registry=None,
        _on_phase_start=None,
        _on_phase_end=None,
        _dispatch_active=True,
    )
    run.output_dir.mkdir()

    def _capture_run_profile(profile, st, registry, **kwargs):
        # Read at dispatch time: the gate context must already be armed.
        captured["gate_profile"] = getattr(run, "_gate_profile", "<<unset>>")
        captured["gate_ctx"] = getattr(run, "_gate_ctx", "<<unset>>")
        st.phase_handoff_request = None

    monkeypatch.setattr(_rt, "run_profile", _capture_run_profile)

    process_pending_phase_handoffs(run, prof, ctx=ctx_sentinel)

    assert captured["gate_profile"] is prof
    assert captured["gate_ctx"] is ctx_sentinel


# ── P1b + P2: cold retry rebuilds durable upstream context + includes missing ─


def test_retry_includes_missing_receipts_and_builds_prior_context(tmp_path):
    """P2: the retry set is ``incomplete_subtasks ∪ missing_subtask_receipts``
    (a missing receipt is not done, so it re-runs). P1b: the done subtasks'
    upstream context is rebuilt from the persisted ``implementation_receipts``
    (attestation summary), not left blank."""
    from agents.entities import SubTask
    from pipeline.plan_artifacts import write_parsed_plan_artifact
    from pipeline.plan_parser import ParsedPlan
    from pipeline.project.handoff import apply_phase_handoff_resume

    run_dir = tmp_path / "20260604_120600_impl"
    run_dir.mkdir()
    plan = ParsedPlan(
        short_summary="s", planning_context="c",
        subtasks=(
            SubTask(id="a", goal="ga"),
            SubTask(id="t2", goal="g2", depends_on=("a",)),
            SubTask(id="t3", goal="g3"),
        ),
        source="json",
    )
    write_parsed_plan_artifact(run_dir, plan, attempt=1)
    _seed_decision(run_dir, handoff_id=_HANDOFF_ID, action="retry_feedback",
                   feedback="finish t2/t3")
    state = _state(tmp_path)
    run = SimpleNamespace(
        output_dir=run_dir, _ckpt=None, _metrics=None, state=state,
        session={
            "status": "awaiting_phase_handoff",
            "phase_handoff": {
                "id": _HANDOFF_ID, "phase": "implement", "round": 1,
                "loop_max_rounds": 1,
                "artifacts": {
                    "incomplete_subtasks": ["t2"],
                    "missing_subtask_receipts": ["t3"],
                },
                "last_output": "",
            },
            "phases": {"implement": {"implementation_receipts": [
                {"subtask_id": "a", "state": "done",
                 "attestation_summary": "a built base"},
                {"subtask_id": "t2", "state": "incomplete"},
            ]}},
        },
    )

    apply_phase_handoff_resume(run, _profile(), None)

    retry = state.extras["implement_retry"]
    # P2: both the incomplete and the missing-receipt subtask are re-run.
    assert set(retry["incomplete_ids"]) == {"t2", "t3"}
    # P1b: the done dependency's attestation summary is carried (not blank);
    # re-run ids are NOT treated as satisfied prior context.
    assert retry["prior_context"]["a"]["attestation_summary"] == "a built base"
    assert "t2" not in retry["prior_context"]
    assert "t3" not in retry["prior_context"]
    # parsed_plan rehydrated for the cold-process retry.
    assert state.parsed_plan is not None
