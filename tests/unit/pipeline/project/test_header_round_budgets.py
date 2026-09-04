# SPDX-License-Identifier: Apache-2.0
"""The run header must not report one retry budget as if it were the other.

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
from core.io.transcript import render_run_header
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
