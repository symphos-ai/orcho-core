"""Plan criteria must trace to the task (planner and validator prompt parts).

Dogfood ``20260911_120115_bc8aa7``: the planner added a ``human`` criterion
requiring a companion change in another repository and widened a grep
invariant onto a file the task never named; the validator read both as
diligence. Two handoffs and two operator waivers later, the run shipped the
work the task actually asked for. These pins keep the rule in both prompts.
"""
from __future__ import annotations

from core.io.prompt_loader import render_prompt


def test_planner_derives_criteria_only_from_the_task() -> None:
    text = render_prompt("tasks/plan", project_dir=None, task="x")
    assert "Derive acceptance criteria only from the task's own" in text
    assert "Never add a criterion the task did not" in text
    assert "is a risk or a note, not a criterion" in text
    # The old unbounded licence is gone.
    assert "Derive acceptance criteria if the task omits them" not in text


def test_validator_rejects_criteria_that_do_not_trace_to_the_task() -> None:
    text = render_prompt(
        "tasks/validate_plan", project_dir=None, task="x", extra_checks="",
    )
    assert "acceptance criteria that do not trace to the task" in text
    assert "is a defect of the plan" in text
    assert "do not treat it as\n  diligence" in text or "do not treat it as diligence" in text
