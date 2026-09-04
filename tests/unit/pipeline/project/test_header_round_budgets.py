# SPDX-License-Identifier: Apache-2.0
"""The run headers must not report one retry budget as if it were the other.

``--max-rounds`` / ``max_rounds`` caps only the implement/review/repair
loop. The plan/validate_plan budget is a per-profile
``LoopStep.max_rounds`` and has no runtime override — ADR 0031 rejected
global round overrides, and ``apply_runtime_max_rounds`` deliberately
skips every loop whose ``round_extras_key`` is not ``"repair_round"``.

The header used to render a bare ``rounds=<max_rounds>`` on the same
State line as ``plan=yes``, so an operator who passed ``--max-rounds 4``
read the planning budget as 4 and then watched the run pause at
``validate_plan automatic round 2/2``. The value was right; the label
lied. These tests pin the labelling, not the loop behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import core
from agents.protocols import SessionMode
from core.io.ansi import strip_ansi
from core.io.transcript import render_cross_run_header, render_run_header
from pipeline.cross_project.profile_setup import find_cross_plan_loop
from pipeline.plugins import PluginConfig
from pipeline.profiles.loader import load_profiles_v2
from pipeline.project.handoff import find_plan_loop
from pipeline.project.run_setup import print_pipeline_header
from pipeline.project.types import PresentationPolicy

pytestmark = [pytest.mark.unit, pytest.mark.project_run]


def _state_line(out: str) -> str:
    lines = [line for line in strip_ansi(out).splitlines() if "session" in line]
    assert lines, f"no session row in header output:\n{out}"
    return lines[0]


def _shipped_profile(name: str):
    path = Path(core.__file__).parent / "_config" / "pipeline_profiles_v2.json"
    return load_profiles_v2(path)[name]


class TestRenderedLabels:
    def test_repair_cap_is_labelled_and_never_a_bare_rounds(self) -> None:
        line = _state_line(render_run_header(
            run_id="R", project="/tmp/p", task="t", agents=[],
            profile="feature", session_mode="auto",
            repair_rounds=4, plan=True, plan_rounds=2,
        ))
        assert "repair_rounds=4" in line
        # The exact misread this fixes: no unqualified ``rounds=`` token.
        assert "rounds=4" not in line.replace("repair_rounds=4", "")

    def test_plan_budget_rides_on_the_plan_field(self) -> None:
        line = _state_line(render_run_header(
            run_id="R", project="/tmp/p", task="t", agents=[],
            profile="feature", session_mode="auto",
            repair_rounds=4, plan=True, plan_rounds=2,
        ))
        assert "plan=yes  (2 rounds)" in line

    def test_single_plan_round_reads_singular(self) -> None:
        line = _state_line(render_run_header(
            run_id="R", project="/tmp/p", task="t", agents=[],
            profile="small_task", session_mode="auto",
            repair_rounds=1, plan=True, plan_rounds=1,
        ))
        assert "plan=yes  (1 round)" in line

    def test_unresolved_profile_omits_the_plan_budget(self) -> None:
        """``None`` means "unknown" — the header must not invent a number."""
        line = _state_line(render_run_header(
            run_id="R", project="/tmp/p", task="t", agents=[],
            profile="feature", session_mode="auto",
            repair_rounds=4, plan=True, plan_rounds=None,
        ))
        assert "plan=yes" in line
        assert "round" not in line.replace("repair_rounds=4", "")

    def test_skip_labels_carry_no_plan_budget(self) -> None:
        line = _state_line(render_run_header(
            run_id="R", project="/tmp/p", task="t", agents=[],
            profile="task", session_mode="auto",
            repair_rounds=1, plan=False, plan_rounds=None,
        ))
        assert "plan=skip" in line
        assert "repair_rounds=1" in line


class TestBudgetsComeFromTheirRealOwners:
    """Writer-to-reader: the profile owns the plan budget, the caller the cap."""

    def test_plan_budget_is_read_off_the_active_profile(self, capsys) -> None:
        profile = _shipped_profile("feature")
        declared = find_plan_loop(profile).max_rounds
        assert declared == 2, "fixture drift: 'feature' no longer declares 2"

        print_pipeline_header(
            presentation=PresentationPolicy.TERMINAL,
            project_path=Path("proj"), task="t",
            plan_model="m", implement_model="m", review_model="m",
            profile_name="feature", session_mode=SessionMode.AUTO,
            # The field-evidence case: operator passed --max-rounds 4.
            max_rounds=4, do_plan=True, plugin=PluginConfig(),
            output_dir=None, profile_obj=profile,
        )
        line = _state_line(capsys.readouterr().out)
        assert "plan=yes  (2 rounds)" in line
        assert "repair_rounds=4" in line

    def test_no_profile_object_still_renders_the_repair_cap(self, capsys) -> None:
        print_pipeline_header(
            presentation=PresentationPolicy.TERMINAL,
            project_path=Path("proj"), task="t",
            plan_model="m", implement_model="m", review_model="m",
            profile_name="feature", session_mode=SessionMode.AUTO,
            max_rounds=3, do_plan=True, plugin=PluginConfig(),
            output_dir=None, profile_obj=None,
        )
        line = _state_line(capsys.readouterr().out)
        assert "repair_rounds=3" in line
        assert "plan=yes" in line
        assert "(" not in line


# ── cross surface ───────────────────────────────────────────────────────────


def _cross_state_line(out: str) -> str:
    lines = [line for line in strip_ansi(out).splitlines() if "session" in line]
    assert lines, f"no session row in cross header output:\n{out}"
    return lines[0]


def _cross_header(**kw) -> str:
    base = dict(
        run_id="X", task="t", projects={"api": "/tmp/api"}, agents=[],
        cross_mode="full", repair_rounds=4, plan_source="cross",
    )
    base.update(kw)
    return strip_ansi(render_cross_run_header(**base))


class TestCrossHeaderBudgets:
    """`orcho cross --max-rounds 4` printed `rounds_per_project=4` and then
    bannered `CROSS-PLAN -- Round 1/2`. Same defect as the mono header, one
    surface over: the cross plan loop's budget is the projection's own
    `LoopStep.max_rounds` and `--max-rounds` never reaches it either.
    """

    def test_repair_cap_names_the_loop_it_caps(self) -> None:
        line = _cross_state_line(_cross_header(plan_rounds=2))
        assert "repair_rounds_per_project=4" in line
        assert "rounds_per_project=4" not in line.replace(
            "repair_rounds_per_project=4", "",
        )

    def test_plan_budget_rides_on_the_plan_source_row(self) -> None:
        out = _cross_header(plan_rounds=2)
        row = [ln for ln in out.splitlines() if "Plan source" in ln][0]
        assert "cross" in row and "(2 rounds)" in row
        # It must not leak onto the repair row and be misread there.
        assert "2 rounds" not in _cross_state_line(out)

    def test_single_plan_round_reads_singular(self) -> None:
        out = _cross_header(plan_rounds=1)
        assert "(1 round)" in [ln for ln in out.splitlines() if "Plan source" in ln][0]

    def test_unresolved_plan_loop_omits_the_budget(self) -> None:
        out = _cross_header(plan_rounds=None)
        row = [ln for ln in out.splitlines() if "Plan source" in ln][0]
        assert row.strip().endswith("cross")


class TestCrossPlanLoopOwner:
    """`find_cross_plan_loop` is the single owner: the run flow and the
    header both read the planning budget through it, and the header is
    assembled before the run flow resolves its own step handles.
    """

    def test_finds_the_declared_loop_in_a_real_projection(self) -> None:
        from pipeline.cross_project.profile_setup import setup_cross_profile

        setup = setup_cross_profile(profile_name="feature")
        loop = find_cross_plan_loop(setup.projection.global_steps)
        assert loop is not None, "'feature' projects no cross plan loop"
        assert loop.max_rounds >= 1
        # Same loop the mono reader finds on the unprojected profile.
        assert loop.max_rounds == find_plan_loop(_shipped_profile("feature")).max_rounds

    def test_returns_none_when_no_loop_carries_cross_plan(self) -> None:
        from pipeline.runtime import LoopStep, PhaseStep

        unrelated = LoopStep(
            steps=(PhaseStep(phase="implement"),),
            until="implement.done",
            max_rounds=3,
        )
        assert find_cross_plan_loop([unrelated]) is None
        assert find_cross_plan_loop([]) is None
        # A bare (non-loop) step must be skipped, not mistaken for the loop:
        # a projection can declare cross_plan outside any LoopStep.
        assert find_cross_plan_loop(
            [PhaseStep(phase="cross_plan"), unrelated],
        ) is None


class TestResolveGlobalPlanSteps:
    """`_resolve_global_plan_steps` reads the loop through the shared owner.

    Both shapes matter: a projection that declares the plan loop (every
    shipped cross profile) and one that declares bare cross_plan /
    cross_validate_plan steps outside any loop. The second is the branch
    the previous in-order scan reached only when it found no loop.
    """

    @staticmethod
    def _ctx(global_steps):
        from types import SimpleNamespace

        from pipeline.cross_project.session_run import _resolve_global_plan_steps

        ctx = SimpleNamespace(
            projection=SimpleNamespace(global_steps=list(global_steps)),
            global_plan_step=None, global_validate_step=None,
            global_plan_loop=None, has_global_plan=False,
            has_global_validate=False, effective_plan_rounds=1,
        )
        _resolve_global_plan_steps(ctx)
        return ctx

    @staticmethod
    def _cross_step(phase: str, handler: str):
        from pipeline.runtime import CrossScope, CrossStepPolicy, PhaseStep

        return PhaseStep(
            phase=phase,
            cross=CrossStepPolicy(scope=CrossScope.GLOBAL, handler=handler),
        )

    def test_loop_shape_takes_its_budget_from_the_loop(self) -> None:
        from pipeline.runtime import LoopStep

        loop = LoopStep(
            steps=(
                self._cross_step("plan", "cross_plan"),
                self._cross_step("validate_plan", "cross_validate_plan"),
            ),
            until="validate_plan.approved",
            max_rounds=3,
        )
        ctx = self._ctx([loop])
        assert ctx.global_plan_loop is loop
        assert ctx.effective_plan_rounds == 3
        assert ctx.has_global_plan and ctx.has_global_validate

    def test_bare_steps_without_a_loop_resolve_and_budget_one_round(self) -> None:
        plan = self._cross_step("plan", "cross_plan")
        validate = self._cross_step("validate_plan", "cross_validate_plan")
        ctx = self._ctx([plan, validate])
        assert ctx.global_plan_loop is None
        assert ctx.global_plan_step is plan
        assert ctx.global_validate_step is validate
        # No loop means no declared budget; the run flow falls back to 1.
        assert ctx.effective_plan_rounds == 1
