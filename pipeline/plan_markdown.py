"""Render parsed plans into deterministic human-readable markdown.

Architect agents emit the machine contract. Orcho owns the prose artifact so
humans, validate_plan reviewers, evidence bundles, and dashboards all read
the same validated plan without paying the model to duplicate it.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from pipeline.plan_contract import render_plan_contract

if TYPE_CHECKING:
    from agents.entities import SubTask
    from pipeline.plan_parser import ParsedPlan


def render_plan_markdown(plan: ParsedPlan, *, title: str = "Implementation Plan") -> str:
    """Return a stable markdown view of a parsed plan."""
    lines: list[str] = [f"# {title}", ""]

    short_summary = (getattr(plan, "short_summary", "") or "").strip()
    if short_summary:
        lines.extend(["## Short Summary", "", f"**{short_summary}**", ""])

    planning_context = (getattr(plan, "planning_context", "") or "").strip()
    if planning_context:
        lines.extend(["## Planning Context", "", planning_context, ""])

    contract = render_plan_contract(plan).strip()
    if contract:
        lines.extend(contract.splitlines())
        lines.append("")

    lines.append("## Tasks")
    lines.append("")
    for task in getattr(plan, "subtasks", ()) or ():
        _append_task(lines, task)

    return "\n".join(lines).rstrip() + "\n"


def _append_task(lines: list[str], task: SubTask) -> None:
    goal = (task.goal or "").strip()
    heading = f"## Task {task.id}"
    if goal:
        heading = f"{heading}: {goal}"
    lines.append(heading)
    lines.append("")

    if task.files:
        _append_list(lines, "Files", task.files)
    if task.depends_on:
        _append_list(lines, "Depends on", task.depends_on)
    if task.skill:
        lines.extend([f"**Skill:** {task.skill}", ""])
    if task.model:
        lines.extend([f"**Model:** {task.model}", ""])
    if task.spec:
        lines.extend(["**Spec:**", "", task.spec.strip(), ""])
    if task.done_criteria:
        _append_list(lines, "Done Criteria", task.done_criteria)
    if task.allowed_modifications:
        _append_list(lines, "Allowed Modifications", task.allowed_modifications)


def _append_list(lines: list[str], heading: str, items: Iterable[str]) -> None:
    lines.append(f"**{heading}:**")
    for item in items:
        lines.append(f"- {item}")
    lines.append("")


def render_validate_plan_tasks(plan: ParsedPlan) -> str:
    """Render the task-decomposition view of a parsed plan.

    Sibling of :func:`render_plan_markdown` aimed at the
    ``validate_plan`` reviewer surface. It includes ONLY the
    per-subtask decomposition fields that a reviewer needs to
    evaluate (id, goal, spec, files, depends_on, skill, model,
    done_criteria) and intentionally **excludes**:

    * the plan-level ``short_summary`` and ``planning_context`` —
      those describe the run, not the work to verify;
    * the typed ``## Plan Contract`` section — that view is
      already shipped as its own ``plan_contract:typed_plan``
      typed prompt part (see ``pipeline.plan_contract``); a
      reviewer that needs the contract reads it from there;
    * any artifact/path framing — paths live on the artifact
      PromptPart's ``artifact_path`` metadata (see the
      composition-boundary invariant in
      the over-run-plan follow-up and change-semantics planning record (internal));
    * any markdown preamble (no leading title block) — this view
      is consumed as one of several typed prompt parts, not as a
      standalone document.

    The output is deterministic and round-trip-stable: rendering
    the same :class:`ParsedPlan` twice always produces byte-identical
    text, so an envelope that includes this view in its prefix can
    be cache-safe across rounds when the plan is unchanged.
    """
    lines: list[str] = ["## Tasks", ""]
    for task in getattr(plan, "subtasks", ()) or ():
        _append_task(lines, task)
    return "\n".join(lines).rstrip() + "\n"


def render_subtask_dag_map(plan: ParsedPlan) -> str:
    """Render the compact DAG navigation map for subtask prompts (P2).

    Unlike :func:`render_validate_plan_tasks` (which carries every subtask's
    full spec/files/done-criteria for the reviewer), this view is *navigation
    only*: one line per subtask with ``id``, a one-line ``goal``, and
    ``depends_on``. It deliberately omits every executable detail of sibling
    and downstream subtasks so the developer agent treats only its own
    ``## Current Executable Subtask`` block as work.

    Deterministic and byte-stable for a given plan: the same ``ParsedPlan``
    always renders identical text, so the map is identical across every
    subtask invoke in one DAG run.
    """
    lines: list[str] = [
        "## Execution Plan Context",
        "",
        "Background only — navigation, not instructions. Do not execute "
        "sibling or downstream subtasks.",
        "",
    ]
    for task in getattr(plan, "subtasks", ()) or ():
        goal = " ".join((task.goal or "").split()).strip()
        deps = ", ".join(task.depends_on) if task.depends_on else "none"
        head = f"- {task.id}"
        if goal:
            head = f"{head} — {goal}"
        lines.append(f"{head} (depends_on: {deps})")
    return "\n".join(lines).rstrip() + "\n"
