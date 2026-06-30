"""``PhaseStep.execution`` validation. ``"linear"`` is the only built-in
handler-direct execution mode (plugins may register more). Subtask delivery is
policy-owned via ``implementation_execution=subtask_dag``, not a profile-step
execution mode. Profiles declaring an unsupported execution mode get a clear
ValueError.

These tests pin the validation contract so customer plugin profiles
fail fast.
"""
from __future__ import annotations

import pytest

from pipeline.lifecycle import (
    ExecutionModeRegistry,
    LifecycleContext,
    LinearPhaseStepExecutor,
)
from pipeline.plugins import PluginConfig
from pipeline.runtime import (
    LoopStep,
    PhaseRegistry,
    PhaseStep,
    PipelineState,
    Profile,
    run_profile,
)


def _state() -> PipelineState:
    return PipelineState(task="t", project_dir="/p", plugin=PluginConfig())


def _registry() -> PhaseRegistry:
    reg = PhaseRegistry()
    for n in ("plan", "validate_plan", "implement", "review_changes", "repair_changes", "final_acceptance"):
        reg.register(n, lambda s: s)
    return reg


# ── linear is the built-in PhaseStep.execution value ─────────────────────────

class TestLinearAccepted:
    def test_linear_at_top_level_passes_validation(self) -> None:
        profile = Profile(
            name="ok", kind="custom",
            steps=(PhaseStep(phase="implement", execution="linear"),),
        )
        # Should not raise.
        run_profile(profile, _state(), _registry())

    def test_linear_inside_loopstep_passes(self) -> None:
        profile = Profile(
            name="ok", kind="custom",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(phase="plan", execution="linear"),
                        PhaseStep(phase="validate_plan", execution="linear"),
                    ),
                    until="validate_plan.never",
                    max_rounds=1,
                ),
            ),
        )
        run_profile(profile, _state(), _registry())


# ── only registered execution modes dispatch ─────────────────────────────────

class TestUnknownExecutionRejected:
    """Only ``linear`` (plus plugin-registered modes) is a valid PhaseStep
    execution. Any unknown mode is rejected generically — there is no special
    legacy mode. Subtask delivery is selected via
    ``implementation_execution=subtask_dag``, not a profile-step execution."""

    def test_unknown_execution_top_level_rejected(self) -> None:
        profile = Profile(
            name="bad-exec", kind="custom",
            steps=(PhaseStep(phase="implement", execution="bogus"),),
        )
        state = _state()
        with pytest.raises(ValueError, match="execution must be one of"):
            run_profile(profile, state, _registry())

    def test_unknown_execution_inside_loopstep_rejected(self) -> None:
        profile = Profile(
            name="bad-exec-loop", kind="custom",
            steps=(
                LoopStep(
                    steps=(PhaseStep(phase="implement", execution="bogus"),),
                    until="build.never",
                    max_rounds=1,
                ),
            ),
        )
        state = _state()
        with pytest.raises(ValueError, match="execution must be one of"):
            run_profile(profile, state, _registry())


# ── Unknown execution string rejects ─────────────────────────────────────────

class TestUnknownRejected:
    def test_typo_in_execution_caught_at_load(self) -> None:
        profile = Profile(
            name="typo", kind="custom",
            steps=(PhaseStep(phase="implement", execution="liner"),),  # typo
        )
        with pytest.raises(ValueError, match="PhaseStep.execution must be"):
            run_profile(profile, _state(), _registry())

    def test_diagnostic_lists_phase_and_invalid_value(self) -> None:
        profile = Profile(
            name="x", kind="custom",
            steps=(PhaseStep(phase="implement", execution="parallel_review"),),
        )
        with pytest.raises(ValueError) as exc:
            run_profile(profile, _state(), _registry())
        msg = str(exc.value)
        assert "implement" in msg
        assert "parallel_review" in msg


class TestCustomExecutionRegistryAccepted:
    def test_custom_execution_mode_registered_on_ctx_passes_validation(self) -> None:
        """Regression: PhaseStep.execution validation must consult the
 lifecycle registry that will dispatch the step. A plugin-shipped
 execution mode should not be rejected before dispatch when ctx
 carries that registry."""

        class CustomExecutor:
            def execute(self, step, state, ctx):
                state.phase_log[step.phase] = {
                    "output": "custom",
                    "execution": step.execution,
                }
                return state

        exec_modes = ExecutionModeRegistry()
        exec_modes.register("linear", LinearPhaseStepExecutor())
        exec_modes.register("parallel_review", CustomExecutor())

        profile = Profile(
            name="custom-exec", kind="custom",
            steps=(PhaseStep(phase="review_changes", execution="parallel_review"),),
        )
        ctx = LifecycleContext(
            phase_registry=_registry(),
            execution_mode_registry=exec_modes,
        )

        state = _state()
        run_profile(profile, state, _registry(), ctx=ctx)

        assert state.phase_log["review_changes"]["execution"] == "parallel_review"


# ── session_continuity value domain mirrors the SessionContinuity enum ───────

class TestSessionContinuityValidation:
    """ADR 0113: the loader/policy value domain for ``session_continuity``
    must mirror :class:`SessionContinuity` exactly, so a profile cannot
    declare a continuity string the resolver (T3) cannot map onto the enum,
    or vice versa."""

    def test_value_domain_mirrors_enum_members(self) -> None:
        from pipeline.runtime.profile import _VALID_SESSION_CONTINUITY
        from pipeline.runtime.roles import SessionContinuity

        enum_values = {m.value for m in SessionContinuity}
        assert enum_values == _VALID_SESSION_CONTINUITY

    @pytest.mark.parametrize(
        "value", ["fresh_only", "loop_continue", "same_zone_continue"],
    )
    def test_profile_with_continuity_step_runs(self, value: str) -> None:
        from pipeline.runtime import ExecutionPolicy

        profile = Profile(
            name="cont", kind="custom",
            steps=(
                PhaseStep(
                    phase="implement",
                    execution="linear",
                    execution_policy=ExecutionPolicy(
                        mode="linear", session_continuity=value,
                    ),
                ),
            ),
        )
        # Continuity is inert profile shape at this stage — declaring it must
        # not disturb the data-driven walker.
        run_profile(profile, _state(), _registry())

    def test_invalid_continuity_rejected_at_construction(self) -> None:
        from pipeline.runtime import ExecutionPolicy

        with pytest.raises(ValueError, match="session_continuity"):
            ExecutionPolicy(mode="linear", session_continuity="resume_always")


def test_builtin_execution_registry_is_linear_only() -> None:
    # The only built-in PhaseStep execution mode is ``linear`` (plugins may
    # register more). Subtask delivery is NOT an execution mode.
    from pipeline.lifecycle import default_execution_mode_registry
    from pipeline.runtime.runner import _PHASESTEP_EXECUTION_SUPPORTED

    assert default_execution_mode_registry().names() == ["linear"]
    assert frozenset({"linear"}) == _PHASESTEP_EXECUTION_SUPPORTED
