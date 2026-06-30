"""per-round LoopStep callback.

 introduces ``on_round_end`` plumbing through
``run_profile`` → ``_run_loop_step``. The orchestrator's v2 dispatch
uses it to mid-loop ``save_session`` so a crash inside a long
review/fix loop leaves the most recent completed round on disk —
mirroring legacy ``run_review_fix_loop`` checkpoint behaviour.

These tests pin:

 * Callback fires once per round, AFTER the inner phases of that
 round complete;
 * Callback receives the LoopStep, the 1-based round number, and the
 current PipelineState;
 * Callback STILL fires when the round ends via halt (so the most
 recent round's partial state is checkpointable);
 * Callback STILL fires when the round ends via until-satisfied (so
 the success-completion round is checkpointable);
 * Exceptions in the callback are swallowed (observability-only;
 must not corrupt loop progression);
 * Callback NOT registered (None) is the default and works.
"""
from __future__ import annotations

from typing import Any

import pytest

from pipeline.plugins import PluginConfig
from pipeline.runtime import (
    LoopStep,
    PhaseRegistry,
    PhaseStep,
    PipelineProfile,
    PipelineState,
    run_profile,
)


def _ps(*names: str) -> tuple[PhaseStep, ...]:
    return tuple(PhaseStep(phase=n) for n in names)


def _state() -> PipelineState:
    return PipelineState(task="t", project_dir="/p", plugin=PluginConfig())


def _registry_with_phases(*names: str) -> PhaseRegistry:
    reg = PhaseRegistry()
    for n in names:
        reg.register(n, lambda s: s)
    return reg


# ── Basic callback contract ──────────────────────────────────────────────────

class TestOnRoundEndCallbackContract:
    def test_fires_once_per_round_after_inner_phases(self) -> None:
        """Inner phases of round N complete BEFORE on_round_end(N) fires."""
        seen: list[tuple[str, int]] = []
        reg = PhaseRegistry()

        def plan(state: PipelineState) -> PipelineState:
            seen.append(("plan", int(state.extras.get("loop_round", 0))))
            return state

        def validate_plan(state: PipelineState) -> PipelineState:
            seen.append(("validate_plan", int(state.extras.get("loop_round", 0))))
            return state

        reg.register("plan", plan)
        reg.register("validate_plan", validate_plan)

        loop = LoopStep(
            steps=_ps("plan", "validate_plan"),
            until="validate_plan.approved",   # never satisfied here
            max_rounds=2,
        )
        profile = PipelineProfile("p", (loop,))

        def on_round_end(ls: LoopStep, round_n: int, _state: PipelineState) -> None:
            assert ls is loop
            seen.append(("ROUND_END", round_n))

        run_profile(profile, _state(), reg, on_round_end=on_round_end)

        # Each round: plan, validate_plan, then ROUND_END. Two rounds total.
        assert seen == [
            ("plan", 1), ("validate_plan", 1), ("ROUND_END", 1),
            ("plan", 2), ("validate_plan", 2), ("ROUND_END", 2),
        ]

    def test_callback_receives_loopstep_round_n_state(self) -> None:
        captured: list[tuple[Any, int, Any]] = []
        reg = _registry_with_phases("plan")
        loop = LoopStep(
            steps=_ps("plan"),
            until="plan.never",
            max_rounds=3,
        )
        profile = PipelineProfile("p", (loop,))

        def cb(ls, round_n, state):
            captured.append((ls, round_n, state))

        st = _state()
        run_profile(profile, st, reg, on_round_end=cb)

        # Three calls for three rounds; each carries (loop, round_n, same state).
        assert len(captured) == 3
        assert all(c[0] is loop for c in captured)
        assert [c[1] for c in captured] == [1, 2, 3]
        assert all(c[2] is st for c in captured)

    def test_no_callback_default_is_safe(self) -> None:
        """Default ``on_round_end=None`` works — no callback invoked."""
        reg = _registry_with_phases("plan", "validate_plan")
        loop = LoopStep(
            steps=_ps("plan", "validate_plan"),
            until="validate_plan.never",
            max_rounds=2,
        )
        profile = PipelineProfile("p", (loop,))
        # Should not raise — just runs the loop.
        result = run_profile(profile, _state(), reg)
        assert result.halt is False


# ── Callback fires on early termination paths ─────────────────────────────────

class TestOnRoundEndOnTermination:
    def test_callback_fires_when_until_satisfies(self) -> None:
        """Round completes inner phases, on_round_end fires, THEN until check
 breaks the loop. Most recent successful round MUST be checkpointable."""
        reg = PhaseRegistry()
        reg.register("plan", lambda s: s)

        def validate_plan(state: PipelineState) -> PipelineState:
            # Approve on round 2, not before.
            r = int(state.extras.get("loop_round", 0))
            if r >= 2:
                state.phase_log["validate_plan"] = {"approved": True}
            else:
                state.phase_log["validate_plan"] = {"approved": False}
            return state

        reg.register("validate_plan", validate_plan)

        loop = LoopStep(
            steps=_ps("plan", "validate_plan"),
            until="validate_plan.approved",
            max_rounds=5,
        )
        profile = PipelineProfile("p", (loop,))

        rounds_seen: list[int] = []
        run_profile(
            profile, _state(), reg,
            on_round_end=lambda _ls, n, _s: rounds_seen.append(n),
        )

        # Two rounds: round 1 (rejected), round 2 (approved). Callback
        # fires AFTER round 2 inner phases, then until-satisfied breaks.
        assert rounds_seen == [1, 2]

    def test_callback_fires_when_halt_triggers_inside_round(self) -> None:
        """Halt triggered inside a round still fires on_round_end so partial
 state of the round is checkpointable."""
        reg = PhaseRegistry()
        reg.register("plan", lambda s: s)

        def validate_plan(state: PipelineState) -> PipelineState:
            state.stop("test halt round 1")
            return state

        reg.register("validate_plan", validate_plan)

        loop = LoopStep(
            steps=_ps("plan", "validate_plan"),
            until="validate_plan.approved",
            max_rounds=3,
        )
        profile = PipelineProfile("p", (loop,))

        rounds_seen: list[int] = []
        run_profile(
            profile, _state(), reg,
            on_round_end=lambda _ls, n, _s: rounds_seen.append(n),
        )

        # Round 1 had validate_plan halt; on_round_end MUST fire, then loop
        # exits without entering round 2.
        assert rounds_seen == [1]


# ── Exception isolation ──────────────────────────────────────────────────────

class TestOnRoundEndExceptionIsolation:
    def test_callback_raise_does_not_crash_loop(self) -> None:
        """Callback exceptions are observability concerns, not control-flow.
 Loop must continue normally even if callback raises every round."""
        reg = _registry_with_phases("plan", "validate_plan")
        loop = LoopStep(
            steps=_ps("plan", "validate_plan"),
            until="validate_plan.never",
            max_rounds=2,
        )
        profile = PipelineProfile("p", (loop,))

        def bad_cb(_ls, _n, _s):
            raise RuntimeError("checkpoint backend offline")

        # Must not raise out of run_profile.
        result = run_profile(profile, _state(), reg, on_round_end=bad_cb)
        # Loop completed both rounds (callback failure didn't short-circuit).
        assert result.halt is False
        assert result.extras.get("loop_round") == 2


# ── Multiple LoopSteps in one profile ────────────────────────────────────────

class TestOnRoundEndMultipleLoops:
    def test_callback_distinguishes_loops_by_loopstep_identity(self) -> None:
        """Profile with two LoopSteps — callback should be called for each
 loop's rounds, with the right LoopStep identity each time."""
        reg = _registry_with_phases("plan", "validate_plan", "review_changes", "repair_changes")
        plan_loop = LoopStep(
            steps=_ps("plan", "validate_plan"),
            until="validate_plan.never",
            max_rounds=2,
            round_extras_key="plan_round",
        )
        fix_loop = LoopStep(
            steps=_ps("review_changes", "repair_changes"),
            until="review.never",
            max_rounds=3,
            round_extras_key="repair_round",
        )
        profile = PipelineProfile("p", (plan_loop, fix_loop))

        events: list[tuple[str, int]] = []

        def cb(ls, n, _s):
            tag = "PLAN" if ls is plan_loop else "FIX"
            events.append((tag, n))

        run_profile(profile, _state(), reg, on_round_end=cb)

        assert events == [
            ("PLAN", 1), ("PLAN", 2),
            ("FIX", 1),  ("FIX", 2),  ("FIX", 3),
        ]


# ── Orchestrator integration: save_session fires per round via v2 dispatch ──

class TestV2DispatchSaveSessionPerRound:
    """End-to-end: ``_dispatch_via_v2_profile`` registers ``on_round_end``
 that calls ``save_session(...)``. Prove that a v2 LoopStep with N
 rounds yields N save_session calls (legacy parity with
 ``run_review_fix_loop`` per-round checkpoint)."""

    def test_save_session_called_per_round(self, tmp_path, monkeypatch) -> None:
        from pipeline.project.profile_dispatch import (
            dispatch_via_v2_profile as _dispatch_via_v2_profile,
        )
        from pipeline.runtime import PhaseStep as _PhaseStep, Profile

        reg = _registry_with_phases("plan", "validate_plan")
        profile = Profile(
            name="loopy",
            kind="custom",
            description="N-round plan loop for save_session parity check",
            steps=(
                LoopStep(
                    steps=(_PhaseStep(phase="plan"), _PhaseStep(phase="validate_plan")),
                    until="validate_plan.never",
                    max_rounds=3,
                    round_extras_key="plan_round",
                ),
            ),
        )

        # Spy on save_session
        save_calls: list[Any] = []

        def fake_save(out_dir, session):
            save_calls.append((str(out_dir), dict(session)))

        from pipeline.project import profile_dispatch as _pd
        monkeypatch.setattr(_pd, "save_session", fake_save)

        # Build a minimal _PipelineRun stand-in with the attributes
        # _dispatch_via_v2_profile reads.
        class _Run:
            output_dir = tmp_path
            session = {"phases": {}}
            registry = reg
            state = _state()
            do_plan = False
            max_rounds = 3
            _ckpt = None
            _provider = None
            _session_adapters = None
            _metrics = None
            project_path = None
            plugin = None
            session_ts = "test"
            dry_run = False

            def _agent_for_phase(self, name): return None
            def _model_for_phase(self, name): return None
            def _on_phase_start(self, *_a, **_kw): pass
            def _on_phase_end(self, *_a, **_kw): pass
            def _fsm_metrics(self, *_a, **_kw): pass
            def _fsm_checkpoint(self, *_a, **_kw): pass

            def _on_phase_start(self, *_a, **_kw): pass
            def _on_phase_end(self, *_a, **_kw): pass

            def finalize(self):
                return {"status": "done", "phases": dict(self.session.get("phases", {}))}

        run = _Run()
        result = _dispatch_via_v2_profile(run, profile)

        # 3 rounds → 3 save_session calls during the loop. Note: legacy
        # also saves at finalize (orchestrator concern, not loop concern);
        # _dispatch_via_v2_profile's finalize stub above does not save.
        assert len(save_calls) == 3
        assert all(c[0] == str(tmp_path) for c in save_calls)
        #  v2 dispatch initialises ``rounds: []`` upfront
        # (mirrors legacy ``run_review_fix_loop`` line 1242). Stub
        # finalize echoes session.phases, so result reflects that init.
        assert result == {"status": "done", "phases": {"rounds": []}}

    def test_checkpoint_load_failure_fails_closed_before_dispatch(
        self,
        tmp_path,
    ) -> None:
        from pipeline.project.profile_dispatch import (
            dispatch_via_v2_profile as _dispatch_via_v2_profile,
        )
        from pipeline.runtime import PhaseStep as _PhaseStep, Profile

        seen: list[str] = []
        reg = PhaseRegistry()

        def implement(state: PipelineState) -> PipelineState:
            seen.append("implement")
            return state

        reg.register("implement", implement)
        profile = Profile(
            name="task",
            kind="custom",
            steps=(_PhaseStep(phase="implement"),),
        )

        class _BadCheckpoint:
            def load(self, _run_id: str):
                raise OSError("checkpoint unreadable")

        class _Metrics:
            def add_round(self) -> None:
                raise AssertionError("round metrics should not run")

        class _Run:
            output_dir = tmp_path
            session = {"phases": {}}
            registry = reg
            state = _state()
            _dispatch_active = False
            do_plan = False
            max_rounds = 1
            _ckpt = _BadCheckpoint()
            _provider = None
            _session_adapters = None
            _metrics = _Metrics()
            session_ts = "broken-resume"
            hypothesis_enabled = False
            failures: list[tuple[Exception, str]] = []

            def _on_phase_start(self, *_a, **_kw): pass
            def _on_phase_end(self, *_a, **_kw): pass
            def _fsm_metrics(self, *_a, **_kw): pass
            def _fsm_checkpoint(self, *_a, **_kw): pass
            def _record_phase_failure(self, exc, fallback_phase):
                self.failures.append((exc, fallback_phase))
            def finalize(self): return {"status": "done"}

        run = _Run()
        with pytest.raises(RuntimeError, match="Cannot safely resume"):
            _dispatch_via_v2_profile(run, profile)

        assert seen == []
        assert run._dispatch_active is False
        assert len(run.failures) == 1
        failure, fallback_phase = run.failures[0]
        assert "checkpoint state could not be loaded" in str(failure)
        assert fallback_phase == "<v2-dispatch>"

    def test_checkpoint_resume_skips_hypothesis_before_loop_resume_guard(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        from pipeline.project.profile_dispatch import (
            dispatch_via_v2_profile as _dispatch_via_v2_profile,
        )
        from pipeline.runtime import (
            HypothesisPrelude,
            PhaseStep as _PhaseStep,
            Profile,
        )

        reg = _registry_with_phases("plan", "validate_plan")
        profile = Profile(
            name="plan",
            kind="custom",
            steps=(
                LoopStep(
                    steps=(
                        _PhaseStep(
                            phase="plan",
                            hypothesis=HypothesisPrelude(attempts=1),
                        ),
                        _PhaseStep(phase="validate_plan"),
                    ),
                    until="validate_plan.approved",
                    max_rounds=2,
                    round_extras_key="plan_round",
                ),
            ),
        )

        def fail_hypothesis(*_a, **_kw):
            raise AssertionError("checkpoint resume must not run hypothesis")

        from pipeline.project import profile_dispatch as _pd
        monkeypatch.setattr(_pd, "maybe_run_hypothesis", fail_hypothesis)

        class _LoadedCheckpoint:
            completed = ("plan", "validate_plan")

        class _Checkpoint:
            def load(self, _run_id: str):
                return _LoadedCheckpoint()

        class _Metrics:
            def add_round(self) -> None:
                raise AssertionError("round metrics should not run")

        class _Run:
            output_dir = tmp_path
            session = {"phases": {}}
            registry = reg
            state = _state()
            _dispatch_active = False
            do_plan = True
            max_rounds = 1
            _ckpt = _Checkpoint()
            _provider = None
            _session_adapters = None
            _metrics = _Metrics()
            session_ts = "resumed-plan-loop"
            hypothesis_enabled = None
            checkpoint_resume = True
            failures: list[tuple[Exception, str]] = []

            def _on_phase_start(self, *_a, **_kw): pass
            def _on_phase_end(self, *_a, **_kw): pass
            def _fsm_metrics(self, *_a, **_kw): pass
            def _fsm_checkpoint(self, *_a, **_kw): pass
            def _record_phase_failure(self, exc, fallback_phase):
                self.failures.append((exc, fallback_phase))
            def finalize(self): return {"status": "done"}

        run = _Run()
        # Resume contract: when EVERY inner phase of a LoopStep is in
        # the checkpoint's completed set, the loop already finished
        # cleanly in a prior dispatch — resume now skips it (rather
        # than panicking, which broke the post-handoff ``continue``
        # path). The load-bearing invariant for this test is unchanged:
        # whatever the resume path does, it must NOT call hypothesis
        # for an already-completed loop, and must NOT count a fresh
        # round in metrics.
        _dispatch_via_v2_profile(run, profile)
        assert run._dispatch_active is False
        assert run.failures == []

    def test_save_session_failure_isolated_from_loop(self, tmp_path, monkeypatch) -> None:
        """If save_session blows up mid-loop, the loop must complete all
 rounds anyway (checkpoint failure is observability concern)."""
        from pipeline.project.profile_dispatch import (
            dispatch_via_v2_profile as _dispatch_via_v2_profile,
        )
        from pipeline.runtime import PhaseStep as _PhaseStep, Profile

        reg = _registry_with_phases("plan", "validate_plan")
        profile = Profile(
            name="loopy",
            kind="custom",
            description="loop that survives save_session failures",
            steps=(
                LoopStep(
                    steps=(_PhaseStep(phase="plan"), _PhaseStep(phase="validate_plan")),
                    until="validate_plan.never",
                    max_rounds=2,
                    round_extras_key="plan_round",
                ),
            ),
        )

        def boom(_out, _sess):
            raise OSError("disk full")

        from pipeline.project import profile_dispatch as _pd
        monkeypatch.setattr(_pd, "save_session", boom)

        class _Run:
            output_dir = tmp_path
            session = {"phases": {}}
            registry = reg
            state = _state()
            do_plan = False
            max_rounds = 2
            _ckpt = None
            _provider = None
            _session_adapters = None
            _metrics = None
            project_path = None
            plugin = None
            session_ts = "test"
            dry_run = False

            def _agent_for_phase(self, name): return None
            def _model_for_phase(self, name): return None
            def _on_phase_start(self, *_a, **_kw): pass
            def _on_phase_end(self, *_a, **_kw): pass
            def _fsm_metrics(self, *_a, **_kw): pass
            def _fsm_checkpoint(self, *_a, **_kw): pass

            def _on_phase_start(self, *_a, **_kw): pass
            def _on_phase_end(self, *_a, **_kw): pass
            def finalize(self): return {"status": "done"}

        run = _Run()
        # Must NOT raise even though save_session blows up every round.
        out = _dispatch_via_v2_profile(run, profile)
        assert out == {"status": "done"}
        # Loop fully completed both rounds (state.extras records last round).
        assert run.state.extras.get("plan_round") == 2
