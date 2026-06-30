"""
pipeline/prompts/minimal_intents.py — code-owned minimal phase intents.

The ``MINIMAL`` arm of the professional-prompt ablation mode (see
:mod:`pipeline.prompts.modes`) renders these intents instead of the
composed ``_prompts/{roles,tasks,formats}/*.md`` parts. Each function
preserves the required phase inputs and states the task verb in the
plainest possible way. They are not professional methods; they are
the baseline against which the professional prompt layer is measured.

These intents are **code-owned**:

- not user-editable;
- not project-overridable;
- short, operational phrasing only;
- agent vocabulary only — no Orcho internals, no uppercase phase
  names, no system-tail block names, no parser schema details, no
  language policy, no handoff policy, no runtime topology.

System-tail contracts (parser shape, language posture, handoff /
review-target policy, cross-project grammar) are appended by the
builder around the rendered intent and are unaffected by mode.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _join(*parts: str) -> str:
    """Join non-empty parts with blank lines between them."""
    return "\n\n".join(p for p in parts if p)


def _section(label: str, body: str) -> str:
    """Render a ``LABEL:\\nbody`` section, or empty when body is empty."""
    body = body.strip("\n")
    return f"{label}:\n{body}" if body else ""


# ---------------------------------------------------------------------------
# Architect surfaces.
# ---------------------------------------------------------------------------


def plan_intent(
    task: str,
    *,
    ma_artifacts_dir: str = "",
    extra_step: str = "",
) -> str:
    """Minimal intent for PLAN — architect produces an implementation plan."""
    artifact_line = (
        f"Plan documents may be available in {ma_artifacts_dir}/."
        if ma_artifacts_dir
        else ""
    )
    return _join(
        _section("TASK TO PLAN", task),
        artifact_line,
        "Create an implementation plan for the task.",
        extra_step,
    )


def replan_intent(task: str, critique: str, human_feedback: str = "") -> str:
    """Minimal intent for REPLAN — revise a prior plan from critique / operator feedback.

    Either ``critique`` (reviewer findings) or ``human_feedback`` (operator
    instruction) may be empty; emitted sections track non-empty inputs only.
    """
    return _join(
        _section("TASK", task),
        _section("REVIEWER CRITIQUE", critique),
        _section("HUMAN FEEDBACK", human_feedback),
        "Revise the plan. Apply human feedback as authoritative; "
        "address reviewer critique where applicable.",
    )


def decompose_intent(
    task: str,
    *,
    skill_roster_block: str = "",
    extra_step: str = "",
) -> str:
    """Minimal intent for DECOMPOSE — emit a graph of subtasks.

    ``skill_roster_block`` is the project's available-skills text
    (always supplied by the builder; ``""`` only in eval calls). It
    survives in minimal mode because it is required phase input, not
    professional posture.
    """
    return _join(
        _section("TASK", task),
        skill_roster_block.strip(),
        "Decompose the task into a graph of subtasks.",
        extra_step,
    )


def hypothesis_intent(task: str, *, codemap: str = "") -> str:
    """Minimal intent for HYPOTHESIS — short pre-plan root-cause guess."""
    codemap_section = f"REPO MAP:\n{codemap}" if codemap else ""
    return _join(
        _section("TASK", task),
        codemap_section,
        "Produce a short hypothesis about the root cause and approach.",
    )


def readonly_plan_intent(task: str, *, codemap: str = "") -> str:
    """Minimal intent for READONLY PLAN — produce a plan, do not edit files."""
    codemap_section = f"REPO MAP:\n{codemap}" if codemap else ""
    return _join(
        _section("TASK", task),
        codemap_section,
        "Produce an implementation plan in markdown. Do not modify any files.",
    )


# ---------------------------------------------------------------------------
# Implementer surfaces.
# ---------------------------------------------------------------------------


def build_intent(
    task: str,
    *,
    ma_artifacts_dir: str = "",
    extra_step: str = "",
) -> str:
    """Minimal intent for implement — execute the task."""
    artifact_line = (
        f"Plan documents may be available in {ma_artifacts_dir}/."
        if ma_artifacts_dir
        else ""
    )
    return _join(
        _section("TASK", task),
        artifact_line,
        "Implement the task.",
        extra_step,
    )


def fix_intent(task: str, body: str) -> str:
    """Minimal intent for repair_changes — address feedback (review findings, tests)."""
    return _join(
        _section("TASK", task),
        _section("Feedback", body),
        "Address the feedback above.",
    )


# ---------------------------------------------------------------------------
# Reviewer surfaces.
# ---------------------------------------------------------------------------


def review_focus_intent(task: str, *, extra_checks: str = "") -> str:
    """Minimal intent for review_changes / final_acceptance — review task-relevant changes."""
    return _join(
        _section("TASK", task),
        "Review the task-relevant changes.",
        extra_checks.strip(),
    )


def plan_review_focus_intent(task: str, *, extra_checks: str = "") -> str:
    """Minimal intent for validate_plan — review the proposed plan."""
    return _join(
        _section("TASK", task),
        "Review the proposed implementation plan.",
        extra_checks.strip(),
    )


def hypothesis_review_focus_intent(task: str) -> str:
    """Minimal intent for HYPOTHESIS_QA — validate the hypothesis."""
    return _join(
        _section("TASK", task),
        "Validate the proposed implementation hypothesis.",
    )


def runtime_review_uncommitted_intent(focus: str = "") -> str:
    """Minimal intent for runtime review of the configured change target."""
    return _join(
        "Review the configured code-change target.",
        _section("FOCUS", focus),
    )


# ---------------------------------------------------------------------------
# Cross-project surface.
# ---------------------------------------------------------------------------


def cross_plan_intent(
    task: str,
    *,
    paths_list: str = "",
    cross_artifacts_dir: str = "",
) -> str:
    """Minimal intent for CROSS PLAN — multi-project planning surface.

    Both ``paths_list`` and ``cross_artifacts_dir`` are required phase
    inputs that the cross orchestrator needs to route subtasks back to
    individual projects; they survive in minimal mode.
    """
    artifact_line = (
        f"Write the cross-project plan to {cross_artifacts_dir}/."
        if cross_artifacts_dir
        else ""
    )
    return _join(
        _section("TASK", task),
        _section("PROJECTS", paths_list),
        artifact_line,
        "Create a cross-project implementation plan.",
    )


def cross_plan_review_focus_intent(
    task: str,
    *,
    aliases: str = "",
    artifact_block: str = "",
) -> str:
    """Minimal intent for CROSS_VALIDATE_PLAN — review the cross-project plan.

    Cross-plan review checks consistency *across* the listed sub-projects:
    that each alias actually owns a slice of the change, that the wire
    contracts line up producer-to-consumer, and that persistence /
    schema gaps aren't being glossed over by a payload-only diagnosis.
    """
    return _join(
        _section("TASK", task),
        _section("PROJECTS", aliases),
        _section("DOCUMENT", artifact_block),
        "Validate the cross-project plan as a coherent multi-repo change.",
    )


def cross_replan_intent(
    task: str,
    critique: str,
    *,
    aliases: str = "",
) -> str:
    """Minimal intent for CROSS_REPLAN — architect revises the cross plan
    after CROSS_VALIDATE_PLAN rejection.

    The critique body lands inline so the architect knows precisely which
    findings to address without resuming a stale session.
    """
    return _join(
        _section("TASK", task),
        _section("PROJECTS", aliases),
        _section("REVIEWER CRITIQUE", critique),
        "Revise the cross-project plan to address every finding above.",
    )


__all__ = [
    "plan_intent",
    "replan_intent",
    "decompose_intent",
    "hypothesis_intent",
    "readonly_plan_intent",
    "build_intent",
    "fix_intent",
    "review_focus_intent",
    "plan_review_focus_intent",
    "hypothesis_review_focus_intent",
    "runtime_review_uncommitted_intent",
    "cross_plan_intent",
    "cross_plan_review_focus_intent",
    "cross_replan_intent",
]
