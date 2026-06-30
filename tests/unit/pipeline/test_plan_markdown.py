"""Typed task-decomposition renderer for ``validate_plan``.

Pins the contract that :func:`pipeline.plan_markdown.render_validate_plan_tasks`
produces a stable, narrow view of a ``ParsedPlan`` suitable for
attaching to a reviewer prompt as a typed decomposition part:

* Includes per-subtask fields: id, goal, spec, files, depends_on,
  skill, model, done_criteria.
* Excludes anything that does not belong on the decomposition surface:
  plan-level short_summary / planning_context, the typed
  ``## Plan Contract`` section, any artefact path or markdown
  preamble.

The renderer must be deterministic: rendering the same plan twice
produces byte-identical output (load-bearing for any future cache
layout that includes the view in a stable-prefix partition).
"""
from __future__ import annotations

from agents.entities import SubTask
from pipeline.plan_markdown import (
    render_plan_markdown,
    render_subtask_dag_map,
    render_validate_plan_tasks,
)
from pipeline.plan_parser import ParsedPlan


def _plan(*, with_contract: bool = True) -> ParsedPlan:
    """Plan fixture with rich plan-level metadata plus two subtasks
    so the "exclude plan-level / include task-level" contract is
    visible to assertions."""
    kwargs = dict(
        short_summary="The plan exists to fix the X bug.",
        planning_context=(
            "Found X reproduces with the documented input. The repo "
            "already has a similar handler in foo.py — symmetric path."
        ),
        subtasks=(
            SubTask(
                id="t1",
                goal="Investigate the surface",
                spec="Read existing handler, list call sites.",
                files=("src/handler.py",),
                skill="backend-python",
                model="claude-opus-4-7",
                done_criteria=("List of call sites is captured",),
            ),
            SubTask(
                id="t2",
                goal="Apply the fix",
                spec="Implement the minimal change.",
                files=("src/handler.py", "tests/test_handler.py"),
                depends_on=("t1",),
                done_criteria=(
                    "Fix is in place",
                    "Tests still pass",
                ),
            ),
        ),
        source="json",
    )
    if with_contract:
        kwargs.update(
            goal="Fix X without breaking Y",
            acceptance_criteria=(
                "X no longer reproduces",
                "Y still works",
            ),
            owned_files=("src/handler.py", "tests/test_handler.py"),
            commands_to_run=("pytest tests/test_handler.py -q",),
            risks=("Could regress nearby code path",),
            review_focus=("Symmetry with existing handler",),
        )
    return ParsedPlan(**kwargs)


# ── Inclusion: every per-subtask decomposition field appears ─────────────────


class TestIncludes:
    def test_task_ids_and_goals_appear(self) -> None:
        out = render_validate_plan_tasks(_plan())
        assert "Task t1" in out
        assert "Investigate the surface" in out
        assert "Task t2" in out
        assert "Apply the fix" in out

    def test_files_appear(self) -> None:
        out = render_validate_plan_tasks(_plan())
        assert "src/handler.py" in out
        assert "tests/test_handler.py" in out

    def test_depends_on_appears(self) -> None:
        out = render_validate_plan_tasks(_plan())
        assert "Depends on" in out
        assert "t1" in out  # carried as the dependency of t2

    def test_skill_and_model_appear_when_set(self) -> None:
        out = render_validate_plan_tasks(_plan())
        assert "backend-python" in out
        assert "claude-opus-4-7" in out

    def test_spec_appears(self) -> None:
        out = render_validate_plan_tasks(_plan())
        assert "Read existing handler, list call sites." in out
        assert "Implement the minimal change." in out

    def test_done_criteria_appear(self) -> None:
        out = render_validate_plan_tasks(_plan())
        assert "Done Criteria" in out
        assert "List of call sites is captured" in out
        assert "Fix is in place" in out


# ── Exclusion: nothing plan-level / contract-level leaks in ──────────────────


class TestExcludes:
    def test_no_plan_level_short_summary(self) -> None:
        out = render_validate_plan_tasks(_plan())
        # ``short_summary`` describes the run, not the work to verify.
        assert "The plan exists to fix the X bug." not in out
        # The ``## Short Summary`` heading from render_plan_markdown
        # is plan-level framing and must not appear.
        assert "Short Summary" not in out

    def test_no_planning_context(self) -> None:
        out = render_validate_plan_tasks(_plan())
        assert "Found X reproduces" not in out
        assert "Planning Context" not in out

    def test_no_plan_contract_section(self) -> None:
        """The typed ``## Plan Contract`` view is its own typed prompt
        part (``plan_contract:typed_plan``). Including it here would
        re-ship the same content twice on every round."""
        out = render_validate_plan_tasks(_plan())
        assert "Plan Contract" not in out
        assert "Acceptance Criteria" not in out
        # Contract-only contents (goal text / risks / review focus)
        # must not appear in the decomposition view.
        assert "Fix X without breaking Y" not in out
        assert "Could regress nearby code path" not in out

    def test_no_markdown_preamble(self) -> None:
        """The view is attached as one of several typed prompt parts,
        not as a standalone document — no leading ``# Implementation
        Plan`` (or other H1) title."""
        out = render_validate_plan_tasks(_plan())
        assert not out.lstrip().startswith("# Implementation Plan")
        # First non-empty line must be the Tasks heading.
        first = next(line for line in out.splitlines() if line.strip())
        assert first.strip() == "## Tasks"

    def test_no_filesystem_path_leak(self) -> None:
        """Paths in the wire content are the plan-declared task files
        (e.g. ``src/handler.py``) — legitimate. But ``artifact_path``
        / ``/tmp/...`` filesystem locations are never the renderer's
        business: this view does not see them."""
        out = render_validate_plan_tasks(_plan())
        assert "/tmp/" not in out
        assert "plan_artifact_path" not in out


# ── Determinism ──────────────────────────────────────────────────────────────


class TestDeterminism:
    def test_render_is_byte_identical_across_calls(self) -> None:
        plan = _plan()
        first = render_validate_plan_tasks(plan)
        second = render_validate_plan_tasks(plan)
        assert first == second

    def test_empty_subtasks_renders_to_just_header(self) -> None:
        """A plan with no subtasks renders to just the ``## Tasks``
        header — empty but well-formed. The validate_plan reviewer
        does not encounter this in practice (the parser rejects
        empty DAGs), but the renderer must not crash."""
        plan = ParsedPlan(
            short_summary="",
            planning_context="",
            subtasks=(),
            source="json",
        )
        out = render_validate_plan_tasks(plan)
        assert out.strip() == "## Tasks"


# ── Round-trip with render_plan_markdown (no shared body) ────────────────────


class TestSeparationFromRenderPlanMarkdown:
    """``render_plan_markdown`` is the human projection; this view is
    the reviewer-only decomposition. They share ``_append_task`` for
    consistent per-task formatting, but the views as wholes do NOT
    overlap on plan-level framing."""

    def test_only_tasks_section_is_shared(self) -> None:
        plan = _plan()
        full = render_plan_markdown(plan)
        tasks_view = render_validate_plan_tasks(plan)

        # The per-task heading is in both — same shared formatter.
        assert "Task t1" in full
        assert "Task t1" in tasks_view
        # But plan-level framing only in the full view.
        assert "Short Summary" in full
        assert "Short Summary" not in tasks_view
        assert "Planning Context" in full
        assert "Planning Context" not in tasks_view


# ── render_subtask_dag_map: compact navigation, no executable detail ──────────


class TestRenderSubtaskDagMap:
    def test_lists_every_subtask_id_and_goal(self) -> None:
        out = render_subtask_dag_map(_plan())
        assert "## Execution Plan Context" in out
        assert "- t1 — Investigate the surface (depends_on: none)" in out
        assert "- t2 — Apply the fix (depends_on: t1)" in out

    def test_omits_all_sibling_executable_detail(self) -> None:
        # The whole point: spec / files / done-criteria of any subtask must
        # NOT appear — only id / goal / depends_on navigation.
        out = render_subtask_dag_map(_plan())
        assert "**Spec:**" not in out
        assert "**Files in scope:**" not in out
        assert "**Done Criteria" not in out
        assert "**Done criteria" not in out
        # Concrete leaked values from the fixture's specs/files must be absent.
        assert "Read existing handler" not in out
        assert "src/handler.py" not in out
        assert "List of call sites is captured" not in out

    def test_depends_on_none_vs_list(self) -> None:
        out = render_subtask_dag_map(_plan())
        # t1 has no deps → "none"; t2 depends on t1 → the id.
        assert "(depends_on: none)" in out
        assert "(depends_on: t1)" in out

    def test_deterministic(self) -> None:
        plan = _plan()
        assert render_subtask_dag_map(plan) == render_subtask_dag_map(plan)
