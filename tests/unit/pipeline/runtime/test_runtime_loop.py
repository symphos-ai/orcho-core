"""
LoopStep retry primitive in pipeline.runtime.

The runtime supports a declarative ``LoopStep`` so profiles can express
retry semantics in JSON (plan ↔ validate_plan, review_changes ↔
repair_changes, custom enterprise loops) without orchestrator-side
imperative code. These tests pin:

 * LoopStep dataclass validates inputs;
 * profile.validate() catches unknown phases anywhere — including nested;
 * run_profile() iterates inner phases per round, breaks on `until`,
 respects max_rounds, sets the round_extras_key on each iteration;
 * the until predicate handles "phase.field", "not phase.field", and
 list-shaped log entries (per-attempt records);
 * halt during a loop propagates back to the outer profile walker.

 removed coverage for the deleted v1 ``load_profiles``
JSON loader. v2 profile JSON parsing lives in
``pipeline/profiles/loader.py:load_profiles_v2`` and is covered by
``test_profile_loader.py``.
"""

from __future__ import annotations

import pytest

from pipeline.plugins import PluginConfig
from pipeline.runtime import (
    LoopStep,
    PhaseRegistry,
    PhaseStep,
    PipelineProfile,
    PipelineState,
    _evaluate_until,
    run_profile,
)


def _ps(*names: str) -> tuple[PhaseStep, ...]:
    """Helper: build a tuple of bare PhaseStep instances from phase names."""
    return tuple(PhaseStep(phase=n) for n in names)


# ── Test helpers ──────────────────────────────────────────────────────────────

def _state(**kw) -> PipelineState:
    return PipelineState(task="t", project_dir="/p", plugin=PluginConfig(), **kw)


def _registry_recording(seen: list[str]) -> PhaseRegistry:
    """A registry where each handler appends its name to ``seen``."""
    reg = PhaseRegistry()
    def _make(name: str):
        def handler(state: PipelineState) -> PipelineState:
            seen.append(name)
            return state
        return handler
    for n in ("plan", "validate_plan", "implement", "review_changes", "repair_changes", "final_acceptance"):
        reg.register(n, _make(n))
    return reg


# ── LoopStep dataclass ────────────────────────────────────────────────────────

class TestLoopStepDataclass:
    def test_minimal_construct(self) -> None:
        step = LoopStep(steps=_ps("plan"), until="plan.ok")
        assert step.max_rounds == 1
        assert step.round_extras_key == "loop_round"
        # Backward-compat property still readable.
        assert step.inner_phases == ("plan",)

    def test_empty_steps_rejected(self) -> None:
        with pytest.raises(ValueError, match="steps is empty"):
            LoopStep(steps=(), until="x.y")

    def test_non_phasestep_in_steps_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be PhaseStep"):
            LoopStep(steps=("plan",), until="x.y")  # raw string, not PhaseStep

    def test_zero_max_rounds_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_rounds must be"):
            LoopStep(steps=_ps("plan"), until="x.y", max_rounds=0)

    def test_empty_until_rejected(self) -> None:
        with pytest.raises(ValueError, match="until is empty"):
            LoopStep(steps=_ps("plan"), until="")

    def test_oscillation_halt_after_invalid(self) -> None:
        with pytest.raises(ValueError, match="oscillation_halt_after"):
            LoopStep(steps=_ps("plan"), until="x.y", oscillation_halt_after=1)


# ── Profile.validate ──────────────────────────────────────────────────────────

class TestProfileValidate:
    def test_unknown_phase_in_loop_is_caught(self) -> None:
        reg = PhaseRegistry()
        reg.register("plan", lambda s: s)
        profile = PipelineProfile(
            "p",
            (LoopStep(steps=_ps("plan", "ghost"), until="plan.ok"),),
        )
        with pytest.raises(ValueError, match="ghost"):
            profile.validate(reg)

    def test_unknown_top_level_phase_caught(self) -> None:
        reg = PhaseRegistry()
        reg.register("plan", lambda s: s)
        profile = PipelineProfile("p", ("plan", "missing"))
        with pytest.raises(ValueError, match="missing"):
            profile.validate(reg)

    def test_invalid_entry_type_raises(self) -> None:
        reg = PhaseRegistry()
        profile = PipelineProfile("p", (123,))  # neither str nor LoopStep
        with pytest.raises(TypeError, match="neither a phase name"):
            profile.validate(reg)


# ── _evaluate_until predicate ─────────────────────────────────────────────────

class TestEvaluateUntil:
    def test_truthy_dict_field(self) -> None:
        s = _state()
        s.phase_log["validate_plan"] = {"approved": True}
        assert _evaluate_until("validate_plan.approved", s) is True

    def test_falsy_dict_field(self) -> None:
        s = _state()
        s.phase_log["validate_plan"] = {"approved": False}
        assert _evaluate_until("validate_plan.approved", s) is False

    def test_missing_phase_treated_as_falsy(self) -> None:
        assert _evaluate_until("validate_plan.approved", _state()) is False

    def test_negated_predicate(self) -> None:
        s = _state()
        s.phase_log["review_changes"] = {"has_issues": True}
        assert _evaluate_until("not review_changes.has_issues", s) is False
        s.phase_log["review_changes"]["has_issues"] = False
        assert _evaluate_until("not review_changes.has_issues", s) is True

    def test_list_phase_log_reads_last_entry(self) -> None:
        """Some session-shape adapters store per-attempt records as a list."""
        s = _state()
        s.phase_log["validate_plan"] = [
            {"attempt": 1, "approved": False},
            {"attempt": 2, "approved": True},
        ]
        assert _evaluate_until("validate_plan.approved", s) is True

    def test_malformed_predicate_raises(self) -> None:
        with pytest.raises(ValueError, match="must be"):
            _evaluate_until("no_dot_here", _state())


# ── run_profile with LoopStep ─────────────────────────────────────────────────

class TestRunLoopStep:
    def test_exits_on_first_satisfied_until(self) -> None:
        seen: list[str] = []
        reg = _registry_recording(seen)

        # Make validate_plan flip approved=True after the first round.
        def validate_plan(state: PipelineState) -> PipelineState:
            seen.append("validate_plan")  # mirror _registry_recording
            state.phase_log["validate_plan"] = {"approved": True}
            return state
        reg.register("validate_plan", validate_plan)
        # Reset 'seen' since calling reg.get() did not append; remove the
        # extra entry the original handler would have added on first call.

        profile = PipelineProfile("p", (
            LoopStep(steps=_ps("plan", "validate_plan"),
                     until="validate_plan.approved", max_rounds=3),
        ))
        run_profile(profile, _state(), reg)

        assert seen == ["plan", "validate_plan"]  # only one round before exit

    def test_runs_max_rounds_when_never_satisfied(self) -> None:
        seen: list[str] = []
        reg = _registry_recording(seen)

        # validate_plan never approves.
        def validate_plan(state: PipelineState) -> PipelineState:
            seen.append("validate_plan")
            state.phase_log["validate_plan"] = {"approved": False}
            return state
        reg.register("validate_plan", validate_plan)

        profile = PipelineProfile("p", (
            LoopStep(steps=_ps("plan", "validate_plan"),
                     until="validate_plan.approved", max_rounds=3),
        ))
        run_profile(profile, _state(), reg)
        # 3 rounds × 2 phases = 6 calls.
        assert seen == ["plan", "validate_plan", "plan", "validate_plan", "plan", "validate_plan"]

    def test_round_extras_key_set_per_iteration(self) -> None:
        seen_rounds: list[int] = []
        reg = PhaseRegistry()

        def plan(state: PipelineState) -> PipelineState:
            seen_rounds.append(state.extras["plan_round"])
            return state
        def validate_plan(state: PipelineState) -> PipelineState:
            state.phase_log["validate_plan"] = {"approved": False}
            return state
        reg.register("plan", plan)
        reg.register("validate_plan", validate_plan)

        profile = PipelineProfile("p", (
            LoopStep(steps=_ps("plan", "validate_plan"),
                     until="validate_plan.approved", max_rounds=3,
                     round_extras_key="plan_round"),
        ))
        run_profile(profile, _state(), reg)
        assert seen_rounds == [1, 2, 3]

    def test_halt_inside_loop_stops_outer_profile(self) -> None:
        seen: list[str] = []
        reg = PhaseRegistry()
        def plan(state: PipelineState) -> PipelineState:
            seen.append("plan")
            return state
        def validate_plan(state: PipelineState) -> PipelineState:
            seen.append("validate_plan")
            state.stop("manual halt for test")
            return state
        def build(state: PipelineState) -> PipelineState:
            seen.append("implement")
            return state
        reg.register("plan", plan)
        reg.register("validate_plan", validate_plan)
        reg.register("implement", build)

        profile = PipelineProfile("p", (
            LoopStep(steps=_ps("plan", "validate_plan"),
                     until="validate_plan.approved", max_rounds=5),
            "implement",
        ))
        result = run_profile(profile, _state(), reg)
        # Halt fires inside validate_plan → loop breaks → outer also breaks → build
        # never runs.
        assert seen == ["plan", "validate_plan"]
        assert result.halt is True

    def test_callbacks_fire_for_inner_phases(self) -> None:
        reg = PhaseRegistry()
        reg.register("plan", lambda s: s)
        def validate_plan(state: PipelineState) -> PipelineState:
            state.phase_log["validate_plan"] = {"approved": True}
            return state
        reg.register("validate_plan", validate_plan)

        profile = PipelineProfile("p", (
            LoopStep(steps=_ps("plan", "validate_plan"),
                     until="validate_plan.approved", max_rounds=2),
            "plan",  # plain phase after the loop
        ))
        starts: list[str] = []
        ends: list[str] = []
        run_profile(
            profile, _state(), reg,
            on_phase_start=lambda name, _s: starts.append(name),
            on_phase_end  =lambda name, _s: ends.append(name),
        )
        assert starts == ["plan", "validate_plan", "plan"]
        assert ends == starts


# ── Integration: end-to-end LoopStep dispatch ────────────────────────────────

def test_loop_profile_end_to_end() -> None:
    """Build a PipelineProfile with a LoopStep in code, run it, verify rounds
 happen as declared. original test loaded the profile
 from JSON via the deleted v1 ``load_profiles``; v2 JSON parsing is
 covered by ``test_profile_loader.py`` instead."""
    profile = PipelineProfile(
        "retry_demo",
        (
            LoopStep(
                steps=_ps("plan", "validate_plan"),
                until="validate_plan.approved",
                max_rounds=2,
            ),
        ),
    )

    seen: list[str] = []
    reg = PhaseRegistry()
    reg.register("plan", lambda s: (seen.append("plan") or s))
    def validate_plan(state: PipelineState) -> PipelineState:
        seen.append("validate_plan")
        # Approve only on round 2.
        state.phase_log["validate_plan"] = {
            "approved": state.extras.get("loop_round") == 2,
        }
        return state
    reg.register("validate_plan", validate_plan)

    run_profile(profile, _state(), reg)
    assert seen == ["plan", "validate_plan", "plan", "validate_plan"]
