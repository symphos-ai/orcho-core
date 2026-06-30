"""Unit tests for ``pipeline.cross_project.profile_projection``."""
from __future__ import annotations

import pytest

from core.infra.paths import CONFIG_DIR as _CONFIG_DIR
from pipeline.cross_project.profile_projection import (
    CrossProjectionError,
    project_cross_profile,
)
from pipeline.profiles.loader import load_profiles_v2
from pipeline.runtime import (
    CrossScope,
    CrossStepPolicy,
    LoopStep,
    PhaseStep,
    Profile,
    ProfileKind,
)

_PROFILES_PATH = _CONFIG_DIR / "pipeline_profiles_v2.json"


def _step(phase: str, scope: CrossScope | None = None, handler: str | None = None) -> PhaseStep:
    cross = CrossStepPolicy(scope=scope, handler=handler) if scope is not None else None
    return PhaseStep(phase=phase, cross=cross)


def _custom(name: str, steps: tuple) -> Profile:
    return Profile(name=name, kind=ProfileKind.CUSTOM, steps=steps)


class TestProjectionErrors:
    def test_missing_cross_on_phasestep_raises(self) -> None:
        prof = _custom("nocross", (_step("plan"),))
        with pytest.raises(CrossProjectionError, match="no cross policy"):
            project_cross_profile(prof)

    def test_global_step_without_handler_raises(self) -> None:
        prof = _custom("h", (_step("plan", CrossScope.GLOBAL),))
        with pytest.raises(CrossProjectionError, match="must declare cross.handler"):
            project_cross_profile(prof)

    def test_global_step_with_unknown_handler_raises(self) -> None:
        prof = _custom(
            "h",
            (_step("plan", CrossScope.GLOBAL, "my_alt_planner"),),
        )
        with pytest.raises(CrossProjectionError, match="unknown cross.handler"):
            project_cross_profile(prof)

    def test_contract_check_as_profile_step_rejected(self) -> None:
        """``contract_check`` is the cross runner's terminal gate, NOT a
        profile step. A profile declaring it would (a) break mono runs
        with an unknown phase and (b) duplicate the gate in cross mode.
        Projection must reject it explicitly so the error surfaces at
        profile authoring time.
        """
        prof = _custom(
            "bad",
            (
                _step("plan", CrossScope.GLOBAL, "cross_plan"),
                _step("contract_check", CrossScope.GLOBAL, "contract_check"),
            ),
        )
        with pytest.raises(CrossProjectionError, match="reserved for the cross runner"):
            project_cross_profile(prof)

    def test_contract_check_as_project_scoped_step_rejected(self) -> None:
        """Same reservation applies to ``scope=project``: the runner owns
        contract_check end-to-end. Authoring a project-scoped one would
        crash the child sub-pipeline with an unregistered phase handler.
        """
        prof = _custom(
            "bad2",
            (
                _step("plan", CrossScope.GLOBAL, "cross_plan"),
                _step("implement", CrossScope.PROJECT),
                _step("contract_check", CrossScope.PROJECT),
            ),
        )
        with pytest.raises(CrossProjectionError, match="reserved for the cross runner"):
            project_cross_profile(prof)

    def test_contract_check_in_loop_rejected(self) -> None:
        """Reserved-phase check also covers LoopStep inner steps."""
        loop = LoopStep(
            steps=(
                _step("contract_check", CrossScope.GLOBAL, "contract_check"),
            ),
            until="contract_check.approved",
            max_rounds=1,
        )
        prof = _custom("bad3", (loop,))
        with pytest.raises(CrossProjectionError, match="reserved for the cross runner"):
            project_cross_profile(prof)

    def test_cross_final_acceptance_as_profile_step_rejected(self) -> None:
        """ADR 0025 Phase 3: ``cross_final_acceptance`` is the cross
        runner's system release gate, not a profile step. A profile
        declaring it would crash mono runs (unknown phase) and would
        clash with the runner's terminal-gate invocation in cross runs.
        """
        prof = _custom(
            "bad_cfa_global",
            (
                _step("plan", CrossScope.GLOBAL, "cross_plan"),
                _step(
                    "cross_final_acceptance",
                    CrossScope.GLOBAL,
                    "cross_final_acceptance",
                ),
            ),
        )
        with pytest.raises(CrossProjectionError, match="reserved for the cross runner"):
            project_cross_profile(prof)

    def test_cross_final_acceptance_as_project_step_rejected(self) -> None:
        """Same reservation applies to project scope."""
        prof = _custom(
            "bad_cfa_project",
            (
                _step("plan", CrossScope.GLOBAL, "cross_plan"),
                _step("implement", CrossScope.PROJECT),
                _step("cross_final_acceptance", CrossScope.PROJECT),
            ),
        )
        with pytest.raises(CrossProjectionError, match="reserved for the cross runner"):
            project_cross_profile(prof)

    def test_cross_final_acceptance_in_loop_rejected(self) -> None:
        """LoopStep inner steps are also covered by the reservation."""
        loop = LoopStep(
            steps=(
                _step(
                    "cross_final_acceptance",
                    CrossScope.GLOBAL,
                    "cross_final_acceptance",
                ),
            ),
            until="cross_final_acceptance.approved",
            max_rounds=1,
        )
        prof = _custom("bad_cfa_loop", (loop,))
        with pytest.raises(CrossProjectionError, match="reserved for the cross runner"):
            project_cross_profile(prof)

    def test_loop_inner_global_step_validates_handler(self) -> None:
        # All inner steps must declare a known handler.
        loop = LoopStep(
            steps=(
                _step("plan", CrossScope.GLOBAL, "cross_plan"),
                _step("validate_plan", CrossScope.GLOBAL, "bogus_handler"),
            ),
            until="validate_plan.approved",
            max_rounds=2,
        )
        prof = _custom("h", (loop,))
        with pytest.raises(CrossProjectionError, match="unknown cross.handler"):
            project_cross_profile(prof)

    def test_mixed_loop_scopes_raises(self) -> None:
        loop = LoopStep(
            steps=(
                _step("plan", CrossScope.GLOBAL, "cross_plan"),
                _step("validate_plan", CrossScope.PROJECT),
            ),
            until="validate_plan.approved",
            max_rounds=2,
        )
        prof = _custom("mixed", (loop,))
        with pytest.raises(CrossProjectionError, match="mixed cross scopes"):
            project_cross_profile(prof)

    def test_task_profile_rejected_for_lack_of_global_plan(self) -> None:
        profiles = load_profiles_v2(_PROFILES_PATH)
        with pytest.raises(CrossProjectionError, match="no global planning step"):
            project_cross_profile(profiles["task"])


class TestProjectionShape:
    def test_all_global_loop_projects_to_global_only(self) -> None:
        loop = LoopStep(
            steps=(
                _step("plan", CrossScope.GLOBAL, "cross_plan"),
                _step("validate_plan", CrossScope.GLOBAL, "cross_validate_plan"),
            ),
            until="validate_plan.approved",
            max_rounds=2,
        )
        prof = _custom("g", (loop, _step("implement", CrossScope.PROJECT)))
        proj = project_cross_profile(prof)
        assert len(proj.global_steps) == 1
        assert isinstance(proj.global_steps[0], LoopStep)
        # Semantic phase names preserved.
        assert tuple(s.phase for s in proj.global_steps[0].steps) == (
            "plan", "validate_plan",
        )
        # handler survives projection on the step's cross field.
        assert proj.global_steps[0].steps[1].cross.handler == "cross_validate_plan"

    def test_all_project_loop_projects_to_project_only(self) -> None:
        loop = LoopStep(
            steps=(
                _step("review_changes", CrossScope.PROJECT),
                _step("repair_changes", CrossScope.PROJECT),
            ),
            until="review_changes.clean",
            max_rounds=1,
        )
        prof = _custom(
            "p",
            (_step("plan", CrossScope.GLOBAL, "cross_plan"),
             _step("implement", CrossScope.PROJECT),
             loop),
        )
        proj = project_cross_profile(prof)
        assert len(proj.global_steps) == 1
        # Project side: implement + the review/repair loop.
        assert len(proj.project_steps) == 2
        assert isinstance(proj.project_steps[1], LoopStep)

    def test_scope_both_requires_known_global_handler(self) -> None:
        """BOTH scope is part of the API surface but currently no shipped
        cross handler is registered for genuinely-fan-out steps. A BOTH
        step with an unknown handler must be rejected — confirms BOTH
        runs through the same handler validation as GLOBAL.
        """
        prof = _custom(
            "b",
            (_step("plan", CrossScope.GLOBAL, "cross_plan"),
             _step("implement", CrossScope.PROJECT),
             _step("my_fanout", CrossScope.BOTH, "unknown_handler")),
        )
        with pytest.raises(CrossProjectionError, match="unknown cross.handler"):
            project_cross_profile(prof)

    def test_scope_skip_omits_step(self) -> None:
        prof = _custom(
            "s",
            (_step("plan", CrossScope.GLOBAL, "cross_plan"),
             _step("compliance_check", CrossScope.SKIP),
             _step("implement", CrossScope.PROJECT)),
        )
        proj = project_cross_profile(prof)
        all_phases = (
            {s.phase for s in proj.global_steps if isinstance(s, PhaseStep)}
            | {s.phase for s in proj.project_steps if isinstance(s, PhaseStep)}
        )
        assert "compliance_check" not in all_phases


# Catalogue-shape assertions on each shipped profile (the former
# ``TestShippedProfilesProjection``) have been removed. They duplicated
# the catalogue JSON and broke on every tune. The projection logic is
# covered by ``TestProjectionShape`` / ``TestProjectionErrors`` against
# synthetic profiles; the loader smoke
# (``test_profile_loader.TestShippedProfilesV2.test_parses_without_error``)
# already verifies the shipped catalogue loads.


@pytest.mark.preserve_handoff_fail_fast
class TestPhaseHandoffCrossFailFast:
    """Cross projection narrows the historical "reject every non-bypass
    handoff" guard per ADR 0038: ``human_feedback_on_reject`` on the
    ``cross_validate_plan`` handler is now honoured end-to-end by the
    cross orchestrator (pause → ``orcho_phase_handoff_decide`` → resume
    with ``continue`` / ``retry_feedback`` / ``halt``). Every other
    non-bypass shape stays rejected so the cross run cannot silently
    drop the declared pause."""

    @staticmethod
    def _plan_loop_with_handoff_type(handoff_type_value: str):
        from pipeline.runtime import (
            PhaseHandoffPolicy,
            PhaseHandoffType,
        )
        return LoopStep(
            steps=(
                PhaseStep(
                    phase="plan",
                    cross=CrossStepPolicy(
                        scope=CrossScope.GLOBAL, handler="cross_plan",
                    ),
                ),
                PhaseStep(
                    phase="validate_plan",
                    handoff=PhaseHandoffPolicy(
                        type=PhaseHandoffType(handoff_type_value),
                    ),
                    cross=CrossStepPolicy(
                        scope=CrossScope.GLOBAL,
                        handler="cross_validate_plan",
                    ),
                ),
            ),
            until="validate_plan.approved",
            max_rounds=2,
            round_extras_key="plan_round",
        )

    def test_feature_built_in_profile_cross_projection_accepts_on_reject(
        self,
    ) -> None:
        """Real built-in ``feature`` declares
        ``human_feedback_on_reject`` on the ``cross_validate_plan``
        handler; ADR 0038 makes the cross runner honour this exactly
        like single-run, so projection must now accept it."""
        feature = load_profiles_v2(_PROFILES_PATH)["feature"]
        # Should not raise.
        project_cross_profile(feature)

    def test_complex_feature_built_in_profile_cross_projection_accepts_on_reject(
        self,
    ) -> None:
        complex_feature = load_profiles_v2(_PROFILES_PATH)["complex_feature"]
        # Should not raise — same shape as ``feature``.
        project_cross_profile(complex_feature)

    def test_planning_built_in_profile_cross_projection_fails_fast(
        self,
    ) -> None:
        """``planning`` declares ``human_feedback_always`` — not in the
        ADR 0038 supported set, still refused."""
        planning = load_profiles_v2(_PROFILES_PATH)["planning"]
        with pytest.raises(
            CrossProjectionError,
            match="only honours 'human_feedback_on_reject'",
        ):
            project_cross_profile(planning)

    def test_synthetic_human_feedback_on_reject_accepted(self) -> None:
        """ADR 0038 supported combination: ``human_feedback_on_reject``
        on a step whose ``cross.handler='cross_validate_plan'``."""
        prof = _custom(
            "syn_on_reject",
            (self._plan_loop_with_handoff_type("human_feedback_on_reject"),),
        )
        # Should not raise.
        project_cross_profile(prof)

    def test_project_review_handoff_accepted(self) -> None:
        from pipeline.runtime import (
            PhaseHandoffPolicy,
            PhaseHandoffType,
        )
        loop = LoopStep(
            steps=(
                PhaseStep(
                    phase="review_changes",
                    handoff=PhaseHandoffPolicy(
                        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                    ),
                    cross=CrossStepPolicy(scope=CrossScope.PROJECT),
                ),
                PhaseStep(
                    phase="repair_changes",
                    cross=CrossStepPolicy(scope=CrossScope.PROJECT),
                ),
            ),
            until="review_changes.clean",
            max_rounds=1,
            round_extras_key="repair_round",
        )
        prof = _custom(
            "project_review_handoff",
            (
                _step("plan", CrossScope.GLOBAL, "cross_plan"),
                _step("validate_plan", CrossScope.GLOBAL, "cross_validate_plan"),
                loop,
            ),
        )
        project_cross_profile(prof)

    def test_synthetic_human_feedback_always_rejected(self) -> None:
        prof = _custom(
            "syn_always",
            (self._plan_loop_with_handoff_type("human_feedback_always"),),
        )
        with pytest.raises(
            CrossProjectionError,
            match="only honours 'human_feedback_on_reject'",
        ):
            project_cross_profile(prof)

    def test_on_reject_on_unsupported_handler_rejected(self) -> None:
        """``human_feedback_on_reject`` outside the supported handler
        set (e.g. on a plain ``cross_plan`` step) is still refused —
        the cross orchestrator only wires the pause inside the
        ``cross_validate_plan`` exit point."""
        from pipeline.runtime import (
            PhaseHandoffPolicy,
            PhaseHandoffType,
        )
        loop = LoopStep(
            steps=(
                PhaseStep(
                    phase="plan",
                    handoff=PhaseHandoffPolicy(
                        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                    ),
                    cross=CrossStepPolicy(
                        scope=CrossScope.GLOBAL, handler="cross_plan",
                    ),
                ),
                PhaseStep(
                    phase="validate_plan",
                    cross=CrossStepPolicy(
                        scope=CrossScope.GLOBAL,
                        handler="cross_validate_plan",
                    ),
                ),
            ),
            until="validate_plan.approved",
            max_rounds=2,
            round_extras_key="plan_round",
        )
        prof = _custom("syn_on_reject_wrong_handler", (loop,))
        with pytest.raises(
            CrossProjectionError,
            match="only honours 'human_feedback_on_reject'",
        ):
            project_cross_profile(prof)

    def test_explicit_human_bypass_is_accepted(self) -> None:
        """Bypass is the explicit "no pause" signal; projection must
        keep accepting it without surprises."""
        from pipeline.runtime import (
            PhaseHandoffPolicy,
            PhaseHandoffType,
        )
        loop = LoopStep(
            steps=(
                PhaseStep(
                    phase="plan",
                    cross=CrossStepPolicy(
                        scope=CrossScope.GLOBAL, handler="cross_plan",
                    ),
                ),
                PhaseStep(
                    phase="validate_plan",
                    handoff=PhaseHandoffPolicy(
                        type=PhaseHandoffType.HUMAN_BYPASS,
                    ),
                    cross=CrossStepPolicy(
                        scope=CrossScope.GLOBAL,
                        handler="cross_validate_plan",
                    ),
                ),
            ),
            until="validate_plan.approved",
            max_rounds=1,
            round_extras_key="plan_round",
        )
        prof = _custom("bypass_ok", (loop,))
        # Should not raise.
        project_cross_profile(prof)

    def test_skip_scope_exempts_handoff_from_fail_fast(self) -> None:
        """A PhaseStep with ``cross.scope="skip"`` is dropped from both
        global_steps and project_steps during projection, so its
        declared non-bypass handoff cannot reach the cross runner.
        The fail-fast guard walks the projected output, not the raw
        profile, and therefore accepts the skip-handoff combination.
        Regression: previously the raw-profile walk over-rejected this
        shape."""
        from pipeline.runtime import (
            PhaseHandoffPolicy,
            PhaseHandoffType,
        )
        prof = _custom(
            "skipped_handoff",
            (
                _step("plan", CrossScope.GLOBAL, "cross_plan"),
                # validate_plan declares handoff but is SKIPPED in cross
                # — projection should drop it entirely.
                PhaseStep(
                    phase="validate_plan",
                    handoff=PhaseHandoffPolicy(
                        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                    ),
                    cross=CrossStepPolicy(scope=CrossScope.SKIP),
                ),
                _step("implement", CrossScope.PROJECT),
            ),
        )
        proj = project_cross_profile(prof)
        # validate_plan was skipped; not in either projected side.
        all_phases = (
            {s.phase for s in proj.global_steps if isinstance(s, PhaseStep)}
            | {s.phase for s in proj.project_steps if isinstance(s, PhaseStep)}
        )
        assert "validate_plan" not in all_phases
