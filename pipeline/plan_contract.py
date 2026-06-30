"""pipeline.plan_contract — Render REA-1 typed plan contract for prompts.

Phase prompts (implement / review_changes / repair_changes /
final_acceptance) read the structured plan contract via
:func:`render_plan_contract` and prepend it to their templates so the
executing or reviewing agent sees the same machine-validated contract
as the orchestrator.

The renderer is deliberately minimal: a markdown block with one section
per populated field, omitting empty fields entirely so prompts stay tight
when only some fields are emitted. The mock provider populates every
field for the golden scenario; real architect agents fill what they can.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.plan_parser import ParsedPlan


def render_plan_contract(plan: ParsedPlan | None) -> str:
    """Render the typed plan contract as a markdown block.

    Args:
        plan: parsed plan carrying the REA-1 contract fields. ``None``,
            an empty contract, or a non-:class:`ParsedPlan` all return
            the empty string so callers can unconditionally compose this
            block into a prompt without type-checking the plan shape.

    Returns:
        A markdown ``## Plan Contract`` block when at least one
        contract field is populated; empty string otherwise.
    """
    if plan is None or not getattr(plan, "has_contract", False):
        return ""

    lines: list[str] = ["## Plan Contract"]

    if plan.goal:
        lines.append("")
        lines.append(f"**Goal:** {plan.goal}")

    _append_bullet_section(lines, "Acceptance criteria", plan.acceptance_criteria)
    _append_bullet_section(lines, "Owned files", plan.owned_files)
    _append_allowed_modifications(lines, plan)
    _append_bullet_section(lines, "Commands to run", plan.commands_to_run)
    _append_bullet_section(lines, "Risks", plan.risks)
    _append_bullet_section(lines, "Review focus", plan.review_focus)

    if plan.mcp_context:
        lines.append("")
        lines.append("**MCP context:**")
        for entry in plan.mcp_context:
            server = entry.get("server", "?")
            tool = entry.get("tool", "?")
            args = entry.get("args", {})
            lines.append(f"- `{server}.{tool}` args={args}")

    return "\n".join(lines).rstrip() + "\n"


def _append_bullet_section(
    lines: list[str], heading: str, items: tuple[str, ...],
) -> None:
    if not items:
        return
    lines.append("")
    lines.append(f"**{heading}:**")
    for item in items:
        lines.append(f"- {item}")


def _append_allowed_modifications(lines: list[str], plan: ParsedPlan) -> None:
    """Render the aggregated allowed-companion-modifications section.

    Combines two sources into one ``**Allowed companion modifications:**``
    section, placed right after ``Owned files``:

    * plan-level ``allowed_modifications`` entries, rendered verbatim;
    * every subtask's per-task ``allowed_modifications``, each tagged with
      its owning task id as ``[<task-id>] <entry>`` so a reviewer reading a
      single Plan Contract block can tell which task each companion change
      belongs to.

    The section is omitted entirely when both levels are empty, so plans
    without the field render byte-identically to before. The
    :attr:`ParsedPlan.has_contract` guard already accounts for a plan that
    only declares per-task entries, so this section still renders for it.
    """
    entries: list[str] = list(getattr(plan, "allowed_modifications", ()) or ())
    for subtask in getattr(plan, "subtasks", ()) or ():
        for entry in subtask.allowed_modifications:
            entries.append(f"[{subtask.id}] {entry}")
    if not entries:
        return
    lines.append("")
    lines.append("**Allowed companion modifications:**")
    for entry in entries:
        lines.append(f"- {entry}")
