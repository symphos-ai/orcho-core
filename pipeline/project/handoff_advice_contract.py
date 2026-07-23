# SPDX-License-Identifier: Apache-2.0
"""Authoritative plan-contract snapshot for the handoff advisor.

This module deliberately reads only the typed ``state.parsed_plan`` and the
``PhaseHandoffRequested`` signal.  It does not infer a contract from plan
markdown, reviewer prose, or filesystem state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from core.infra.paths import PROMPTS_DIR


@dataclass(frozen=True, slots=True)
class ContractInvariant:
    """One deterministic, advisor-addressable contract requirement."""

    id: str
    text: str


@dataclass(frozen=True, slots=True)
class SubtaskContractSnapshot:
    """The accepted contract facts owned by one parsed-plan subtask."""

    id: str
    goal: str
    done_criteria: tuple[ContractInvariant, ...] = ()
    owned_files: tuple[str, ...] = ()
    allowed_modifications: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdviceContractSnapshot:
    """Immutable, lossless boundary between a handoff and its accepted plan."""

    raw_task: str
    task_sha256: str
    parsed_plan_available: bool
    goal: str = ""
    acceptance_criteria: tuple[ContractInvariant, ...] = ()
    owned_files: tuple[str, ...] = ()
    allowed_modifications: tuple[str, ...] = ()
    subtasks: tuple[SubtaskContractSnapshot, ...] = ()
    phase: str = ""
    handoff_id: str = ""
    trigger: str = ""
    available_actions: tuple[str, ...] = ()
    round: int = 1
    max_rounds: int = 1
    gate_set: str = ""
    gate_command: str = ""
    failure_kind: str = ""
    correction_context: str = ""

    @property
    def aggregate_owned_files(self) -> tuple[str, ...]:
        """Plan and subtask scope in accepted declaration order, de-duplicated."""
        return _aggregate_scope(self.owned_files, *(task.owned_files for task in self.subtasks))

    @property
    def aggregate_allowed_modifications(self) -> tuple[str, ...]:
        """Plan and subtask companion scope in accepted declaration order."""
        return _aggregate_scope(
            self.allowed_modifications,
            *(task.allowed_modifications for task in self.subtasks),
        )


def build_advice_contract_snapshot(run: Any, signal: Any) -> AdviceContractSnapshot:
    """Build a snapshot from authoritative runtime state and the handoff signal."""
    state = getattr(run, "state", None)
    raw_task = getattr(state, "task", "") if state is not None else ""
    raw_task = raw_task if isinstance(raw_task, str) else str(raw_task or "")
    plan = getattr(state, "parsed_plan", None) if state is not None else None
    artifacts = getattr(signal, "artifacts", {}) or {}
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    findings = artifacts.get("findings")
    first_finding = (
        next((item for item in findings if isinstance(item, Mapping)), {})
        if isinstance(findings, (list, tuple))
        else {}
    )

    acceptance = tuple(
        ContractInvariant(f"acceptance:{index}", str(text))
        for index, text in enumerate(getattr(plan, "acceptance_criteria", ()) or (), 1)
    )
    subtasks = tuple(
        SubtaskContractSnapshot(
            id=str(getattr(subtask, "id", "")),
            goal=str(getattr(subtask, "goal", "") or ""),
            done_criteria=tuple(
                ContractInvariant(
                    f"task:{getattr(subtask, 'id', '')}:done:{index}", str(text)
                )
                for index, text in enumerate(
                    getattr(subtask, "done_criteria", ()) or (), 1
                )
            ),
            owned_files=tuple(str(item) for item in getattr(subtask, "owned_files", ()) or ()),
            allowed_modifications=tuple(
                str(item) for item in getattr(subtask, "allowed_modifications", ()) or ()
            ),
        )
        for subtask in getattr(plan, "subtasks", ()) or ()
    )
    return AdviceContractSnapshot(
        raw_task=raw_task,
        task_sha256=sha256(raw_task.encode("utf-8")).hexdigest(),
        parsed_plan_available=plan is not None,
        goal=str(getattr(plan, "goal", "") or ""),
        acceptance_criteria=acceptance,
        owned_files=tuple(str(item) for item in getattr(plan, "owned_files", ()) or ()),
        allowed_modifications=tuple(
            str(item) for item in getattr(plan, "allowed_modifications", ()) or ()
        ),
        subtasks=subtasks,
        phase=str(getattr(signal, "phase", "") or ""),
        handoff_id=str(getattr(signal, "handoff_id", "") or ""),
        trigger=str(getattr(signal, "trigger", "") or ""),
        available_actions=tuple(getattr(signal, "available_actions", ()) or ()),
        round=int(getattr(signal, "round", 1) or 1),
        max_rounds=int(getattr(signal, "loop_max_rounds", 1) or 1),
        gate_set=str(artifacts.get("gate_set") or artifacts.get("verification_gate_set") or ""),
        gate_command=str(artifacts.get("gate_command") or artifacts.get("verification_command") or ""),
        failure_kind=str(artifacts.get("failure_kind") or first_finding.get("failure_kind") or ""),
        correction_context=str(artifacts.get("correction_context") or ""),
    )


def render_accepted_plan_contract(snapshot: AdviceContractSnapshot) -> str:
    """Render the accepted typed contract without using plan markdown or prose."""
    lines = ["# Accepted plan contract", "", f"parsed_plan_available: {str(snapshot.parsed_plan_available).lower()}"]
    lines += [
        "",
        "## Handoff identity",
        f"phase: {snapshot.phase}",
        f"handoff_id: {snapshot.handoff_id}",
        f"trigger: {snapshot.trigger}",
        f"actions: {', '.join(snapshot.available_actions)}",
        f"round: {snapshot.round}/{snapshot.max_rounds}",
    ]
    if snapshot.gate_set or snapshot.gate_command:
        lines += ["", "## Verification identity", f"gate_set: {snapshot.gate_set}", f"gate_command: {snapshot.gate_command}"]
    if snapshot.failure_kind:
        lines += ["", "## Failure kind", snapshot.failure_kind]
    if snapshot.correction_context:
        lines += ["", "## Correction boundary", snapshot.correction_context]
    if not snapshot.parsed_plan_available:
        return "\n".join(lines)
    if snapshot.goal:
        lines += ["", "## Goal", snapshot.goal]
    _render_invariants(lines, "Plan acceptance criteria", snapshot.acceptance_criteria)
    _render_values(lines, "Plan owned files", snapshot.owned_files)
    _render_values(lines, "Plan allowed modifications", snapshot.allowed_modifications)
    for task in snapshot.subtasks:
        lines += ["", f"## Subtask {task.id}"]
        if task.goal:
            lines.append(f"goal: {task.goal}")
        _render_invariants(lines, "Done criteria", task.done_criteria)
        _render_values(lines, "Owned files", task.owned_files)
        _render_values(lines, "Allowed modifications", task.allowed_modifications)
    return "\n".join(lines)


def _render_invariants(lines: list[str], heading: str, values: tuple[ContractInvariant, ...]) -> None:
    if values:
        lines += ["", f"### {heading}"]
        lines.extend(f"- [{value.id}] {value.text}" for value in values)


def _render_values(lines: list[str], heading: str, values: tuple[str, ...]) -> None:
    if values:
        lines += ["", f"### {heading}"]
        lines.extend(f"- {value}" for value in values)


def _aggregate_scope(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for group in groups for value in group))


def _advice_task_body() -> str:
    try:
        return (PROMPTS_DIR / "tasks" / "handoff_advice.md").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def render_handoff_findings(ctx: Any) -> str:
    lines = ["# Recorded handoff context", "", f"- phase: {ctx.phase}", f"- trigger: {ctx.trigger}", f"- verdict: {ctx.verdict}", f"- round: {ctx.prior_retry_round}/{ctx.loop_max_rounds}"]
    if ctx.available_actions:
        lines.append(f"- available actions: {', '.join(ctx.available_actions)}")
    if ctx.last_phase_summary:
        lines += ["", "## Reviewer summary", ctx.last_phase_summary]
    if ctx.findings:
        lines += ["", "## Findings"]
        for finding in ctx.findings:
            head = " ".join(part for part in (finding.get("severity"), finding.get("id"), finding.get("title")) if part).strip()
            lines.append(f"- {head or '(finding)'}")
            if finding.get("required_fix"):
                lines.append(f"    required_fix: {finding['required_fix']}")
            if finding.get("body"):
                lines.append(f"    {finding['body']}")
    if ctx.last_output:
        lines += ["", "## Last output", ctx.last_output]
    if ctx.correction_context:
        lines += ["", "## Correction context", ctx.correction_context]
    if ctx.diff_summary:
        lines += ["", "## Working tree (git status --short)", ctx.diff_summary]
    return "\n".join(lines)


def render_language_policy(ctx: Any) -> str:
    if not ctx.response_language:
        return ""
    return "\n".join(("# Response language", f"Write human-readable JSON string values (`rationale`, `retry_feedback`, `risks`, `operator_note`) in {ctx.response_language}.", "JSON keys, protocol enum values, file paths, identifiers, command names, and code symbols stay in their original language."))


def build_advice_turn(ctx: Any, *, marker: str, response_contract: str):
    """Build the advisor turn with code-owned stable parts and dynamic facts."""
    from pipeline.prompts.composer import assemble_cache_first_segments
    from pipeline.prompts.types import PromptCacheScope, PromptLayer, PromptPart, PromptStability

    snapshot = ctx.contract_snapshot
    language = render_language_policy(ctx)
    parts = (
        PromptPart(kind="task", name="handoff_advice_procedure", source="core", body="\n\n".join(part for part in (marker, _advice_task_body()) if part), layer=PromptLayer.PHASE),
        PromptPart(kind="system_tail", name="handoff_advice_response", source="code-owned", body=response_contract, layer=PromptLayer.CONTRACT),
        PromptPart(kind="turn_input", name="raw_task", source="artifact", body="# Full raw task\n\n" + (snapshot.raw_task if snapshot else ""), layer=PromptLayer.TURN, stability=PromptStability.TURN, cache_scope=PromptCacheScope.NONE, volatile_reason="raw task is specific to this handoff run"),
        PromptPart(kind="handoff_contract", name="accepted_plan_contract", source="artifact", body=render_accepted_plan_contract(snapshot) if snapshot else "# Accepted plan contract\n\nparsed_plan_available: false", layer=PromptLayer.TURN, stability=PromptStability.TURN, cache_scope=PromptCacheScope.NONE, volatile_reason="accepted plan contract is specific to this handoff run"),
        PromptPart(kind="artifact", name="handoff_findings", source="artifact", body=render_handoff_findings(ctx), layer=PromptLayer.TURN, stability=PromptStability.TURN, cache_scope=PromptCacheScope.NONE, volatile_reason="handoff findings are specific to this handoff"),
    )
    if language:
        parts += (PromptPart(kind="context", name="response_language", source="code-owned", body=language, layer=PromptLayer.TURN, stability=PromptStability.TURN, cache_scope=PromptCacheScope.NONE, volatile_reason="response language follows this handoff surface"),)
    return assemble_cache_first_segments(parts)


def build_advice_prompt(ctx: Any, *, marker: str, response_contract: str) -> str:
    return build_advice_turn(ctx, marker=marker, response_contract=response_contract).text


__all__ = [
    "AdviceContractSnapshot",
    "ContractInvariant",
    "SubtaskContractSnapshot",
    "build_advice_contract_snapshot",
    "build_advice_prompt",
    "build_advice_turn",
    "render_handoff_findings",
    "render_language_policy",
    "render_accepted_plan_contract",
]
