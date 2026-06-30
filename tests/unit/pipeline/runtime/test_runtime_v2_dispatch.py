"""runtime dispatch.

``run_profile`` now accepts both legacy ``PipelineProfile`` (str + LoopStep
entries) and the redesigned ``Profile`` (top-level PhaseStep + LoopStep).
This file pins the v2 dispatch contract: top-level PhaseStep instances
fire callbacks, LoopStep entries iterate inner PhaseSteps, validation
catches unknown phase names.

 legacy entry-name composite registry
(``pipeline.execution_modes``) deleted. ``PhaseStep.execution`` now
dispatches via the lifecycle ``ExecutionModeRegistry`` only. Tests
that previously stubbed ``DagExecutionMode`` now register a stub
``PhaseStepExecutor`` directly.
"""
from __future__ import annotations

import pytest

from pipeline.lifecycle import (
    ExecutionModeRegistry as LifecycleExecutionModeRegistry,
    LinearPhaseStepExecutor,
    default_lifecycle_context,
)
from pipeline.plugins import PluginConfig
from pipeline.runtime import (
    LoopStep,
    PhaseRegistry,
    PhaseStep,
    PipelineProfile,
    PipelineState,
    Profile,
    ProfileKind,
    run_profile,
)


class _StubExecutor:
    """Minimal PhaseStepExecutor stub for substep-6 dispatch tests.

 Stamps the step's phase into ``state.phase_log`` so callers can
 assert the executor ran. Replaces the legacy ``DagExecutionMode``
 sub-handler stubs that needed to populate four sub-phase entries.
 """
    def execute(self, step, state, ctx):
        state.phase_log.setdefault(step.phase, {"ok": True, "via": "stub"})
        return state


def _state(**kw) -> PipelineState:
    kw.setdefault("plugin", PluginConfig())
    return PipelineState(task="t", project_dir="/p", **kw)


def _registry_recording(seen: list[str]) -> PhaseRegistry:
    """A registry where each handler appends its name to ``seen``."""
    reg = PhaseRegistry()

    def _make(name: str):
        def handler(state: PipelineState) -> PipelineState:
            seen.append(name)
            return state
        return handler

    for n in ("plan", "validate_plan", "implement", "review_changes", "repair_changes", "final_acceptance",
              "compliance_check"):
        reg.register(n, _make(n))
    return reg


# ── Top-level PhaseStep dispatch ──────────────────────────────────────────────

class TestPhaseStepTopLevel:
    def test_single_phasestep(self) -> None:
        seen: list[str] = []
        reg = _registry_recording(seen)
        profile = Profile(
            name="small_task",
            kind=ProfileKind.FULL_CYCLE,
            variant="lite",
            steps=(
                PhaseStep(phase="plan"),
                PhaseStep(phase="implement"),
                PhaseStep(phase="final_acceptance"),
            ),
        )
        run_profile(profile, _state(), reg)
        assert seen == ["plan", "implement", "final_acceptance"]

    def test_callbacks_fire_for_top_level_phasesteps(self) -> None:
        starts: list[str] = []
        ends: list[str] = []
        reg = _registry_recording([])
        profile = Profile(
            name="small_task", kind=ProfileKind.FULL_CYCLE, variant="lite",
            steps=(PhaseStep(phase="plan"), PhaseStep(phase="implement")),
        )
        run_profile(
            profile, _state(), reg,
            on_phase_start=lambda n, _s: starts.append(n),
            on_phase_end=lambda n, _s: ends.append(n),
        )
        assert starts == ["plan", "implement"]
        assert ends == ["plan", "implement"]

    def test_halt_stops_subsequent_phasesteps(self) -> None:
        seen: list[str] = []
        reg = PhaseRegistry()

        def plan(s: PipelineState) -> PipelineState:
            seen.append("plan")
            s.stop("manual halt")
            return s

        def build(s: PipelineState) -> PipelineState:
            seen.append("implement")
            return s

        reg.register("plan", plan)
        reg.register("implement", build)
        profile = Profile(
            name="small_task", kind=ProfileKind.FULL_CYCLE, variant="lite",
            steps=(PhaseStep(phase="plan"), PhaseStep(phase="implement")),
        )
        run_profile(profile, _state(), reg)
        assert seen == ["plan"]


# ── Mixed top-level: PhaseStep + LoopStep ────────────────────────────────────

class TestMixedSteps:
    def test_loop_then_phasestep(self) -> None:
        """Profile mixing LoopStep + PhaseStep at top level — the
 canonical advanced shape from pipeline_profiles_v2.json."""
        seen: list[str] = []
        reg = PhaseRegistry()

        def plan(s: PipelineState) -> PipelineState:
            seen.append("plan")
            return s

        def validate_plan(s: PipelineState) -> PipelineState:
            seen.append("validate_plan")
            s.phase_log["validate_plan"] = {"approved": True}
            return s

        def build(s: PipelineState) -> PipelineState:
            seen.append("implement")
            return s

        for name, h in (("plan", plan), ("validate_plan", validate_plan), ("implement", build)):
            reg.register(name, h)

        profile = Profile(
            name="feature", kind=ProfileKind.FULL_CYCLE, variant="advanced",
            steps=(
                LoopStep(
                    steps=(PhaseStep(phase="plan"), PhaseStep(phase="validate_plan")),
                    until="validate_plan.approved",
                    max_rounds=2,
                ),
                PhaseStep(phase="implement"),
            ),
        )
        run_profile(profile, _state(), reg)
        # validate_plan approves on first round → loop exits → build runs.
        assert seen == ["plan", "validate_plan", "implement"]


# ── Validation against runtime registries ────────────────────────────────────

class TestProfileValidation:
    def test_unknown_phase_at_top_level_caught(self) -> None:
        reg = PhaseRegistry()
        reg.register("plan", lambda s: s)
        profile = Profile(
            name="bad", kind=ProfileKind.CUSTOM,
            steps=(PhaseStep(phase="plan"), PhaseStep(phase="ghost")),
        )
        with pytest.raises(ValueError, match="ghost"):
            run_profile(profile, _state(), reg)

    def test_unknown_phase_inside_loop_caught(self) -> None:
        reg = PhaseRegistry()
        reg.register("plan", lambda s: s)
        profile = Profile(
            name="bad", kind=ProfileKind.CUSTOM,
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(phase="plan"),
                        PhaseStep(phase="missing"),
                    ),
                    until="plan.ok",
                ),
            ),
        )
        with pytest.raises(ValueError, match="missing"):
            run_profile(profile, _state(), reg)

    def test_phasestep_execution_dispatches_via_lifecycle_registry(self) -> None:
        """``PhaseStep.execution`` resolves from the lifecycle
 ``ExecutionModeRegistry`` (built-in ``linear`` or a plugin-registered
 mode).
 """
        reg = PhaseRegistry()
        reg.register("implement", lambda s: (
            s.phase_log.setdefault("implement", {"ok": True}) or s
        ))
        reg.register("final_acceptance", lambda s: (
            s.phase_log.setdefault("final_acceptance", {"ok": True}) or s
        ))

        # Custom execution mode registered on the lifecycle registry,
        # passed through ctx — exactly the substep-6 surface for
        # plugin-shipped executors.
        lifecycle_modes = LifecycleExecutionModeRegistry()
        lifecycle_modes.register("linear", LinearPhaseStepExecutor())
        lifecycle_modes.register("custom_build", _StubExecutor())
        ctx = default_lifecycle_context(
            phase_registry=reg,
            execution_mode_registry=lifecycle_modes,
        )

        profile = Profile(
            name="custom_v2", kind=ProfileKind.FULL_CYCLE, variant="advanced",
            steps=(
                PhaseStep(phase="implement", execution="custom_build"),
                PhaseStep(phase="final_acceptance", execution="linear"),
            ),
        )
        # Validation must accept custom_build (registered on ctx) and
        # dispatch through it.
        result = run_profile(profile, _state(), reg, ctx=ctx)
        assert "implement" in result.phase_log
        assert result.phase_log["implement"].get("via") == "stub"
        assert "final_acceptance" in result.phase_log


# ── Backward compat: legacy PipelineProfile path still works ─────────────────

class TestLegacyPipelineProfileShape:
    def test_legacy_str_profile_unchanged(self) -> None:
        seen: list[str] = []
        reg = _registry_recording(seen)
        legacy = PipelineProfile(name="small_task", phases=("plan", "implement"))
        run_profile(legacy, _state(), reg)
        assert seen == ["plan", "implement"]

    def test_legacy_with_loopstep_unchanged(self) -> None:
        seen: list[str] = []
        reg = PhaseRegistry()

        def plan(s: PipelineState) -> PipelineState:
            seen.append("plan")
            return s

        def validate_plan(s: PipelineState) -> PipelineState:
            seen.append("validate_plan")
            s.phase_log["validate_plan"] = {"approved": True}
            return s

        for n, h in (("plan", plan), ("validate_plan", validate_plan)):
            reg.register(n, h)

        legacy = PipelineProfile(
            name="legacy",
            phases=(
                LoopStep(
                    steps=(PhaseStep(phase="plan"), PhaseStep(phase="validate_plan")),
                    until="validate_plan.approved",
                    max_rounds=2,
                ),
            ),
        )
        run_profile(legacy, _state(), reg)
        assert seen == ["plan", "validate_plan"]


# ── Integration: shipped v2 profile dispatched end-to-end ────────────────────

class TestShippedAdvancedProfileE2E:
    """Load the shipped advanced profile from
 ``_config/pipeline_profiles_v2.json`` and dispatch it through
 ``run_profile`` with stub handlers. Catches schema-vs-dispatch
 drift on shipped profiles."""

    def test_advanced_profile_dispatches(self) -> None:
        from core.infra.paths import CONFIG_DIR
        from pipeline.profiles.loader import load_profiles_v2

        v2_path = CONFIG_DIR / "pipeline_profiles_v2.json"
        profiles = load_profiles_v2(v2_path)
        advanced = profiles["feature"]

        seen: list[str] = []

        def stub(name: str):
            def h(s: PipelineState) -> PipelineState:
                seen.append(name)
                # Approve QA / mark review clean to short-circuit loops
                # to single rounds (faster, deterministic).
                if name == "validate_plan":
                    s.phase_log["validate_plan"] = {"approved": True}
                elif name == "review_changes":
                    s.phase_log["review_changes"] = {"clean": True}
                return s
            return h

        reg = PhaseRegistry()
        for n in ("plan", "validate_plan", "implement", "review_changes", "repair_changes", "final_acceptance"):
            reg.register(n, stub(n))

        # The shipped advanced profile uses ``execution: "linear"`` for the
        # implement step; subtask delivery is the implementation_execution
        # policy, not an execution mode. Dispatch routes through PhaseRegistry
        # directly — no modes_registry stubbing needed.
        run_profile(advanced, _state(), reg)

        # LoopStep semantics: ALL inner steps run before ``until`` is
        # evaluated. So review→fix runs once even though review marked
        # clean — the until check exits the loop after round 1.
        # The legacy ``_PipelineRun.run_review_fix_loop`` did a
        # critique-empty short-circuit between review and fix; that
        # behaviour migrates in (either through inter-step
        # until checking or a fix handler that no-ops when critique
        # is empty).
        assert seen == [
            "plan", "validate_plan", "implement", "review_changes", "repair_changes", "final_acceptance",
        ]


# ── Resume contract — Commit 2: skip completed phases ────────────────────────


class TestResumeSkipsCompletedPhases:
    """``run_profile`` accepts a ``completed_phases`` set that
    short-circuits top-level PhaseStep dispatch. The contract guards
    --resume from re-executing write phases (e.g. ``implement``) that
    already finished in a prior run. Without ``completed_phases``,
    dispatch is byte-for-byte unchanged from fresh-run behaviour.
    """

    def test_completed_implement_is_not_redispatched_on_resume(
        self,
    ) -> None:
        seen: list[str] = []
        reg = _registry_recording(seen)
        profile = Profile(
            name="small_task",
            kind=ProfileKind.FULL_CYCLE,
            variant="lite",
            steps=(
                PhaseStep(phase="plan"),
                PhaseStep(phase="implement"),
                PhaseStep(phase="final_acceptance"),
            ),
        )
        run_profile(
            profile, _state(), reg,
            completed_phases={"implement"},
        )
        # Implement must NOT re-run — that's the load-bearing
        # invariant for --resume safety on write phases.
        assert seen == ["plan", "final_acceptance"]

    def test_skipped_phase_records_marker_in_phase_log(self) -> None:
        reg = _registry_recording([])
        profile = Profile(
            name="small_task", kind=ProfileKind.FULL_CYCLE, variant="lite",
            steps=(
                PhaseStep(phase="plan"),
                PhaseStep(phase="implement"),
            ),
        )
        state = _state()
        run_profile(
            profile, state, reg,
            completed_phases={"implement"},
        )
        # The skip leaves a structured marker so downstream consumers
        # (session adapters, dashboards) can render "completed in
        # earlier in this run (resumed)" instead of an empty slot.
        log = state.phase_log.get("implement")
        assert isinstance(log, dict)
        assert log.get("skipped") == "completed earlier in this run (resumed)"

    def test_skipped_phase_still_fires_lifecycle_callbacks(self) -> None:
        starts: list[str] = []
        ends: list[str] = []
        reg = _registry_recording([])
        profile = Profile(
            name="small_task", kind=ProfileKind.FULL_CYCLE, variant="lite",
            steps=(
                PhaseStep(phase="plan"),
                PhaseStep(phase="implement"),
            ),
        )
        run_profile(
            profile, _state(), reg,
            on_phase_start=lambda n, _s: starts.append(n),
            on_phase_end=lambda n, _s: ends.append(n),
            completed_phases={"implement"},
        )
        # Trace continuity: operators expect a coherent start/end
        # pair for every step in the profile, even when the step is
        # short-circuited on resume.
        assert starts == ["plan", "implement"]
        assert ends == ["plan", "implement"]

    def test_fresh_run_completed_phases_empty_is_byte_identical(
        self,
    ) -> None:
        # Without ``completed_phases`` (the default), dispatch must
        # match the no-resume path exactly — same handler invocation
        # order, no skipped markers.
        seen: list[str] = []
        reg = _registry_recording(seen)
        profile = Profile(
            name="small_task", kind=ProfileKind.FULL_CYCLE, variant="lite",
            steps=(
                PhaseStep(phase="plan"),
                PhaseStep(phase="implement"),
            ),
        )
        state = _state()
        run_profile(profile, state, reg)  # no completed_phases
        assert seen == ["plan", "implement"]
        for phase in ("plan", "implement"):
            log = state.phase_log.get(phase, {})
            assert not (
                isinstance(log, dict) and log.get("skipped")
            ), f"fresh run must not mark {phase} as skipped"

    def test_completed_loop_member_fails_closed_instead_of_replaying(
        self,
    ) -> None:
        seen: list[str] = []
        reg = _registry_recording(seen)
        profile = Profile(
            name="feature",
            kind=ProfileKind.FULL_CYCLE,
            variant="advanced",
            steps=(
                PhaseStep(phase="implement"),
                LoopStep(
                    steps=(
                        PhaseStep(phase="review_changes"),
                        PhaseStep(phase="repair_changes"),
                    ),
                    until="review_changes.clean",
                    max_rounds=1,
                    round_extras_key="repair_round",
                ),
                PhaseStep(phase="final_acceptance"),
            ),
        )

        with pytest.raises(RuntimeError, match="loop-internal"):
            run_profile(
                profile,
                _state(),
                reg,
                completed_phases={"repair_changes"},
            )

        assert seen == ["implement"]

    def test_fully_completed_loop_is_skipped_on_resume(self) -> None:
        """When EVERY inner phase of a LoopStep is in ``completed_phases``
        the loop already finished cleanly in a prior dispatch — resume
        skips it (no inner dispatch, no panic) and walks on to the next
        entry. This is the post-handoff ``continue`` path: the user
        accepts the verdict, the orchestrator re-dispatches the
        remaining profile, and any *other* loops that completed before
        the handoff fired must be walked past, not re-entered.
        """
        seen: list[str] = []
        reg = _registry_recording(seen)
        # Mirrors the user-facing failure: a plan loop that already
        # finished + post-loop phases that still need to run.
        profile = Profile(
            name="feature",
            kind=ProfileKind.FULL_CYCLE,
            variant="advanced",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(phase="plan"),
                        PhaseStep(phase="validate_plan"),
                    ),
                    until="validate_plan.approved",
                    max_rounds=2,
                    round_extras_key="plan_round",
                ),
                PhaseStep(phase="implement"),
                PhaseStep(phase="final_acceptance"),
            ),
        )

        state = _state()
        run_profile(
            profile,
            state,
            reg,
            completed_phases={"plan", "validate_plan"},
        )

        # Plan-loop handlers must NOT re-run — that's the load-bearing
        # invariant for ``continue``-after-handoff resume.
        assert seen == ["implement", "final_acceptance"]
        # Each skipped inner phase gets the same marker as a top-level
        # skipped PhaseStep so dashboards render coherently.
        for phase in ("plan", "validate_plan"):
            log = state.phase_log.get(phase)
            assert isinstance(log, dict)
            assert log.get("skipped") == "completed earlier in this run (resumed)"
