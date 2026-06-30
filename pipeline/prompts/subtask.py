"""
pipeline/prompts/subtask.py — Build the executing agent's prompt for a SubTask.

The plan's overall PRD is now decomposed into ``SubTask`` units. Each subtask
gets its own focused prompt — *not* the whole PRD — so the executing agent's
context is anchored to one chunk of work. This is the second half of the
team-lead pattern: the architect routes work via skills + DAG, the runner
materialises each routing decision into a self-contained instruction.

A subtask prompt is composed of, in order:

    1. Plugin context block (language, architecture, file hints).
    2. Skill content block — the canonical SKILL.md body wrapped via
       :func:`pipeline.skills.render_skill_block` (carries source/checksum
       attributes + resource paths the agent can ``cat`` on demand).
       Skipped when the subtask has no skill or the reference was
       unresolved.
    3. Plan-wide background on the first subtask only; subsequent subtasks get
       a compact reference instead of repeating the full contract.
    4. Compact DAG navigation and upstream receipts.
    5. The subtask itself: goal, spec, files, done criteria.

The function returns ``(prompt_text, SkillBinding | None)``. The binding
is populated when a skill was injected; the dag runner accumulates these
into :class:`pipeline.dag_runner.DagRunResult` so the implement handler
can persist them to ``state.extras["skill_bindings"]``.

Plain markdown — same shape as existing plan / implement / review_changes prompts. No
template engine: callers can swap any of these blocks via the plugin's
``build_prompt_extra`` if they need to.
"""

from __future__ import annotations

from agents.entities import SubTask
from pipeline.plugins import PluginConfig
from pipeline.prompts.builders import _plan_contract_part
from pipeline.prompts.contracts import (
    SystemPromptBlock,
    change_handoff_strategy,
    subtask_attestation_contract,
    subtask_execution_rules_strategy,
)
from pipeline.prompts.turn import PromptTurn, PromptTurnEditor
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptLayer,
    PromptPart,
    PromptStability,
)
from pipeline.skills import SkillBinding, SkillPackage, render_skill_block

#: Framing for the background plan contract — kept as its own tiny part so the
#: canonical ``plan_contract:typed_plan`` part stays byte-identical to the
#: validate_plan surface (one part id must mean one byte-string everywhere).
_PLAN_CONTRACT_BACKGROUND_NOTICE = (
    "The Plan Contract below is background delivery context for the whole "
    "plan — do not execute the whole plan from it. Only the Current "
    "Executable Subtask is your work."
)

_PLAN_CONTRACT_REFERENCE_NOTICE = (
    "The full Plan Contract was sent with the first subtask prompt for this "
    "subtask_dag execution and is intentionally not repeated here. Do not "
    "infer additional work from that contract. Use the compact Execution Plan "
    "Context, Upstream Completed, and Current Executable Subtask below; the "
    "Current Executable Subtask remains the only executable scope."
)


def _subtask_part(
    kind: str,
    name: str,
    part_id: str,
    body: str,
    *,
    layer: PromptLayer = PromptLayer.CONTEXT,
) -> PromptPart:
    """Build one turn-volatile subtask PromptPart (TURN/NONE, always re-sent).

    ``kind`` is load-bearing for the context-clearing taxonomy
    (:mod:`pipeline.observability.output_class`): the executable
    ``current_subtask`` is DECISION_BEARING (never silently cleared); the
    skill block and ``execution_plan_context`` map are RE_FETCHABLE
    (navigation/reference, regeneratable); project context/rules are
    RE_FETCHABLE ``context``. Do not collapse the executable subtask to
    ``kind="task"`` — that is RE_FETCHABLE and would mark the live
    instruction clearable.

    P2 keeps every part TURN/NONE; promoting the session-stable parts
    (project context, DAG map) into a contiguous cacheable prefix is a
    separate later optimization. The ``plan_contract`` part is NOT built
    here — it uses the canonical :func:`_plan_contract_part` factory so its
    bytes match the validate_plan/implement surfaces exactly.
    """
    return PromptPart(
        kind=kind,
        name=name,
        source="code-owned",
        body=body,
        id=part_id,
        layer=layer,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="per-subtask focused prompt; re-sent each subtask",
    )


def _system_tail_part(block: SystemPromptBlock) -> PromptPart:
    """Wrap a code-owned system-tail block as a STATIC/GLOBAL PromptPart."""
    return PromptPart(
        kind="system_tail",
        name=block.name,
        source="code-owned",
        body=block.render(),
        id=f"system_tail:{block.name}",
        layer=PromptLayer.CONTRACT,
        stability=PromptStability.STATIC,
        cache_scope=PromptCacheScope.GLOBAL,
    )


def build_subtask_prompt(
    subtask: SubTask,
    plugin: PluginConfig,
    *,
    skill: SkillPackage | None = None,
    binding: SkillBinding | None = None,
    project_dir: str = "",
    plan_contract: str = "",
    plan_contract_sent: bool = False,
    dag_map: str = "",
    upstream_receipts: str = "",
    change_handoff: str = "uncommitted",
) -> tuple[PromptTurn, SkillBinding | None]:
    """Render the prompt the executing developer agent will receive.

    P2/P3 shape — the current subtask is the only executable scope:

    1. plugin/project context (refetchable)
    2. background notice + the plan contract on the first subtask only; later
       subtasks get a compact reference to avoid re-blurring scope
    3. compact DAG map (``execution_plan_context`` — navigation only: id,
       goal, depends_on; no sibling specs/files/done-criteria)
    4. upstream receipts (P3 — bounded, sandboxed quoted output from declared
       dependencies; continuity hints, omitted when there are no deps)
    5. skill block (when resolved)
    6. ``## Current Executable Subtask`` — the one executable block
    7. project rules
    8. current-only execution rules (code-owned strategy)
    9. done-criteria attestation contract (code-owned; only when the subtask
       declares ``done_criteria`` — the developer must append one typed
       ``subtask_attestation`` object Orcho gates on)
    10. change-handoff strategy

    The full plan decomposition (``render_validate_plan_tasks``) is no longer
    sent: every sibling's spec/files/done used to leak in and the developer
    treated the whole plan as executable. The plan contract is sent only once,
    when entering the subtask DAG execution context. Subsequent subtasks carry
    a compact reference instead, because repeating plan-wide acceptance/risk
    text keeps pulling the developer back toward the whole plan.

    Args:
        subtask: the SubTask to render.
        plugin: project plugin config (used for the context block).
        skill: package resolved by ``agent_resolver`` — the body lands in a
            :func:`render_skill_block` wrapper.
        binding: audit record produced alongside ``skill`` by
            ``agent_resolver``; returned to the caller (dag runner) so the run
            can persist it to ``state.extras["skill_bindings"]``.
        project_dir: hint surfaced at the top of the prompt when set.
        plan_contract: rendered REA-1 typed plan contract (from
            :func:`pipeline.plan_contract.render_plan_contract`) — background
            delivery context, wrapped via the canonical
            :func:`_plan_contract_part` factory so its bytes match the
            validate_plan/implement surfaces.
        plan_contract_sent: ``True`` when an earlier subtask in this
            subtask_dag execution already carried ``plan_contract``. The
            prompt then emits only a compact reference notice, never the full
            contract body.
        dag_map: compact DAG navigation map (from
            :func:`pipeline.plan_markdown.render_subtask_dag_map`) — id, goal,
            depends_on per subtask, no executable detail.
        upstream_receipts: rendered ``## Upstream Completed`` section (from
            :func:`pipeline.dag_runner._render_upstream_receipts`) — bounded,
            sandboxed quoted output from declared dependencies. Continuity
            hints, not instructions/proof; empty (omitted) when no deps.

    Returns:
        ``(PromptTurn, SkillBinding | None)``. The binding mirrors the
        ``binding`` argument when a skill was injected; ``None`` otherwise.
    """
    parts: list[PromptPart] = []
    if project_dir:
        parts.append(_subtask_part(
            "context", "working_dir", "subtask_working_dir",
            f"Working directory: {project_dir}"))
    ctx = _plugin_context(plugin)
    if ctx:
        parts.append(_subtask_part(
            "context", "project_context", "subtask_project_context", ctx))
    if plan_contract:
        # Background framing as a separate part; the contract itself stays
        # byte-identical to the canonical surface (same id → same bytes).
        parts.append(_subtask_part(
            "execution_scope_notice", "plan_contract_background",
            "execution_scope_notice:plan_contract_background",
            _PLAN_CONTRACT_BACKGROUND_NOTICE))
        parts.append(_plan_contract_part(plan_contract))
    elif plan_contract_sent:
        parts.append(_subtask_part(
            "execution_scope_notice", "plan_contract_reference",
            "execution_scope_notice:plan_contract_reference",
            _PLAN_CONTRACT_REFERENCE_NOTICE))
    if dag_map:
        parts.append(_subtask_part(
            "execution_plan_context", "subtask_dag",
            "execution_plan_context:subtask_dag", dag_map.rstrip()))
    if upstream_receipts:
        # P3: bounded, sandboxed continuity hints from declared upstream deps.
        # Decision-bearing TURN input — context the current subtask reasons
        # about, never silently cleared; the body is quoted prior output.
        parts.append(_subtask_part(
            "upstream_receipt", "upstream_completed",
            f"upstream_receipt:{subtask.id}", upstream_receipts.rstrip(),
            layer=PromptLayer.TURN))
    if skill is not None:
        parts.append(_subtask_part(
            "skill", skill.name, f"subtask_skill:{skill.name}",
            render_skill_block(skill, subtask_id=subtask.id)))
    # The one executable block — decision-bearing TURN input.
    parts.append(_subtask_part(
        "current_subtask", "current_subtask", f"current_subtask:{subtask.id}",
        _subtask_block(subtask), layer=PromptLayer.TURN))
    if plugin.build_prompt_extra:
        parts.append(_subtask_part(
            "context", "project_rules", "subtask_project_rules",
            f"Project rules for execution:\n{plugin.build_prompt_extra}".rstrip()))

    # System-tail strategies, in order: current-only execution rules, the
    # done-criteria attestation contract (only when the subtask declares
    # criteria to close), then change handoff. All code-owned (boundary
    # discipline). P7: a criteria-less subtask emits no attestation contract —
    # there is nothing to attest, and the runner skips the gate symmetrically.
    parts.append(_system_tail_part(subtask_execution_rules_strategy()))
    if subtask.done_criteria:
        parts.append(_system_tail_part(subtask_attestation_contract()))
    parts.append(_system_tail_part(change_handoff_strategy(mode=change_handoff)))

    editor = PromptTurnEditor()
    for part in parts:
        editor.append(part)
    turn = editor.build()
    return turn, (binding if skill is not None else None)


def _plugin_context(plugin: PluginConfig) -> str:
    bits: list[str] = []
    if plugin.language:
        bits.append(f"Language: {plugin.language}")
    if plugin.architecture:
        bits.append(f"Architecture: {plugin.architecture}")
    if plugin.file_hints:
        bits.append(f"Key directories/files: {', '.join(plugin.file_hints)}")
    if not bits:
        return ""
    return "Project context:\n" + "\n".join(f"  - {b}" for b in bits)


def _subtask_block(subtask: SubTask) -> str:
    lines: list[str] = [
        f"## Current Executable Subtask `{subtask.id}`",
        "",
        f"**Goal:** {subtask.goal}",
    ]
    if subtask.spec:
        lines.extend(["", "**Spec:**", subtask.spec])
    if subtask.files:
        lines.append("")
        lines.append("**Files in scope:**")
        for f in subtask.files:
            lines.append(f"- {f}")
    if subtask.done_criteria:
        lines.append("")
        lines.append("**Done criteria (the work is not finished until each is true):**")
        for c in subtask.done_criteria:
            lines.append(f"- {c}")
    if subtask.depends_on:
        lines.append("")
        lines.append(
            "Upstream subtasks already completed: "
            + ", ".join(f"`{d}`" for d in subtask.depends_on)
        )
    return "\n".join(lines)
