"""Prompt builders for cross-project orchestration phases."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from core.infra import config
from core.infra.platform import workspace_dir as _resolve_workspace

if TYPE_CHECKING:
    from pipeline.prompts.turn import PromptTurn

# Workspace root for prompt overrides, resolved via core.platform so the
# engine works from any install location.
_ORCHESTRATOR_ROOT = _resolve_workspace() or Path(__file__).parent.parent.parent


def set_orchestrator_root(path: Path) -> None:
    """Override the prompt-resolution root for tests and embedded callers."""
    global _ORCHESTRATOR_ROOT
    _ORCHESTRATOR_ROOT = Path(path)


def _alias_path_instruction(aliases: list[str]) -> str:
    """Tell the architect to use ``[alias]/relative/path`` in the plan JSON.

    The architect needs absolute project roots to call tools (Read /
    Bash). File references inside the cross-plan JSON (each subtask's
    ``files`` array, and any path mentioned in ``interface_contract`` /
    ``spec``) must use the alias-prefixed form so downstream cross
    artefacts (handoff, validate_plan focus prompt) stay portable across
    operator machines.
    """
    if not aliases:
        return ""
    example = aliases[0]
    return (
        "When referencing files in the cross-plan JSON (subtask `files` "
        f"arrays and prose fields), use the `[alias]/relative/path` form "
        f"(e.g. `[{example}]/path/to/file`) instead of absolute filesystem "
        "paths. Tool calls (Read, Bash, etc.) still take absolute paths — "
        "only the JSON references need the alias form."
    )


def _append_path_alias_instruction(rendered: str, aliases: list[str]) -> str:
    """Tack the alias instruction onto a minimal-mode prompt body."""
    if not aliases:
        return rendered
    note = _alias_path_instruction(aliases)
    if not note or note in rendered:
        return rendered
    sep = "\n\n" if rendered and not rendered.endswith("\n\n") else ""
    return f"{rendered}{sep}{note}"


def cross_plan_prompt(
    task: str,
    projects: dict[str, Path],
    cross_artifacts_dir: Path,
    *,
    professional_prompt_mode: str | None = None,
) -> PromptTurn:
    """Ask the architect runtime to plan across all projects."""
    from pipeline.prompts import minimal_intents
    from pipeline.prompts.composer import PromptSpec, render_composed_prompt
    from pipeline.prompts.contracts import (
        authoring_language_strategy,
        cross_plan_json_contract,
        plan_artifact_boundary_contract,
    )
    from pipeline.prompts.modes import (
        ProfessionalPromptMode,
        coerce_professional_prompt_mode,
    )

    aliases = list(projects.keys())
    paths_list = "\n".join(f"  [{a}] {p}" for a, p in projects.items())
    cfg = config.AppConfig.load()
    plan_language = getattr(cfg, "plan_language", getattr(cfg, "task_language", None))

    mode = coerce_professional_prompt_mode(professional_prompt_mode)
    variables: dict[str, object] = {}
    from pipeline.prompts.builders import (
        _render_prompt_output,
        _turn_input_part,
    )
    extra_parts: tuple = ()
    if mode is ProfessionalPromptMode.FULL:
        rendered = render_composed_prompt(
            PromptSpec(role="systems_architect", task="cross_plan", format="detailed"),
            project_dir=_ORCHESTRATOR_ROOT,
            variables=variables,
        )
        dynamic_block = (
            f"TASK:\n{task}\n\n"
            f"PROJECTS INVOLVED:\n{paths_list}\n\n"
            f"ALIASES: {', '.join(aliases)}\n\n"
            f"{_alias_path_instruction(aliases)}"
        )
        ti = _turn_input_part("cross_plan_input", dynamic_block)
        extra_parts = (ti,) if ti is not None else ()
    else:
        intent = minimal_intents.cross_plan_intent(
            task,
            paths_list=paths_list,
            cross_artifacts_dir=str(cross_artifacts_dir),
        )
        if mode is ProfessionalPromptMode.MINIMAL_WITH_FORMAT:
            from pipeline.prompts.builders import (
                _append_format,
                _render_format_only,
            )
            rendered = _append_format(
                intent,
                _render_format_only(
                    "detailed",
                    project_dir=_ORCHESTRATOR_ROOT,
                    variables=variables,
                ),
            )
        else:
            rendered = intent
    rendered = _append_path_alias_instruction(rendered, aliases)
    return _render_prompt_output(
        rendered,
        system_tail=(
            plan_artifact_boundary_contract(),
            cross_plan_json_contract(
                body_language=plan_language,
                input_language=getattr(cfg, "task_language", None),
            ),
            authoring_language_strategy(task_language=plan_language),
        ),
        extra_upper_parts=extra_parts,
    )


def cross_plan_review_focus(
    task: str,
    aliases: list[str],
    *,
    plan_artifact: str = "",
    plan_artifact_path: str = "cross_plan.md",
    professional_prompt_mode: str | None = None,
) -> PromptTurn:
    """Build the focus prompt for CROSS_VALIDATE_PLAN."""
    from pipeline.prompts import minimal_intents
    from pipeline.prompts.composer import PromptSpec, render_composed_prompt
    from pipeline.prompts.contracts import review_json_contract
    from pipeline.prompts.modes import (
        ProfessionalPromptMode,
        coerce_professional_prompt_mode,
    )

    mode = coerce_professional_prompt_mode(professional_prompt_mode)
    aliases_str = ", ".join(aliases)
    variables: dict[str, object] = {}
    from pipeline.prompts.builders import (
        _render_prompt_output,
        _turn_input_part,
    )
    from pipeline.prompts.types import (
        PromptCacheScope,
        PromptLayer,
        PromptPart,
        PromptStability,
    )

    extra_parts: tuple = ()
    if mode is ProfessionalPromptMode.FULL:
        rendered = render_composed_prompt(
            PromptSpec(role="code_reviewer", task="cross_validate_plan", format="detailed"),
            project_dir=_ORCHESTRATOR_ROOT,
            variables=variables,
        )
        sections: list[str] = [
            "## Cross plan review",
            f"TASK:\n{task[:300]}",
        ]
        if aliases_str:
            sections.append(f"PROJECTS INVOLVED: {aliases_str}")
        ti = _turn_input_part(
            "cross_validate_plan_input", "\n\n".join(sections),
        )
        artifact_part = None
        if plan_artifact:
            artifact_part = PromptPart(
                kind="artifact",
                name="cross_validate_plan",
                source="artifact",
                body=plan_artifact,
                artifact_path=plan_artifact_path,
                layer=PromptLayer.TURN,
                stability=PromptStability.TURN,
                cache_scope=PromptCacheScope.NONE,
                volatile_reason="cross plan artifact under review",
                id="artifact:cross_validate_plan",
            )
        extra_parts = tuple(p for p in (ti, artifact_part) if p is not None)
    else:
        intent = minimal_intents.cross_plan_review_focus_intent(
            task[:300],
            aliases=aliases_str,
            artifact_block=plan_artifact,
        )
        if mode is ProfessionalPromptMode.MINIMAL_WITH_FORMAT:
            from pipeline.prompts.builders import (
                _append_format,
                _render_format_only,
            )
            rendered = _append_format(
                intent,
                _render_format_only(
                    "detailed",
                    project_dir=_ORCHESTRATOR_ROOT,
                    variables=variables,
                ),
            )
        else:
            rendered = intent
    cfg = config.AppConfig.load()
    return _render_prompt_output(
        rendered,
        system_tail=(review_json_contract(body_language=cfg.task_language),),
        extra_upper_parts=extra_parts,
    )


def cross_replan_prompt(
    task: str,
    critique: str,
    projects: dict[str, Path],
    cross_artifacts_dir: Path,
    *,
    professional_prompt_mode: str | None = None,
) -> PromptTurn:
    """Build the architect prompt for round 2+ of the cross-plan loop."""
    from pipeline.prompts import minimal_intents
    from pipeline.prompts.composer import PromptSpec, render_composed_prompt
    from pipeline.prompts.contracts import (
        authoring_language_strategy,
        cross_plan_json_contract,
        plan_artifact_boundary_contract,
    )
    from pipeline.prompts.modes import (
        ProfessionalPromptMode,
        coerce_professional_prompt_mode,
    )

    mode = coerce_professional_prompt_mode(professional_prompt_mode)
    cfg = config.AppConfig.load()
    plan_language = getattr(cfg, "plan_language", getattr(cfg, "task_language", None))
    paths_list = "\n".join(f"  [{a}] {p}" for a, p in projects.items())
    aliases = list(projects.keys())
    variables: dict[str, object] = {}
    from pipeline.prompts.builders import (
        _feedback_part,
        _render_prompt_output,
        _turn_input_part,
    )
    extra_parts: tuple = ()
    if mode is ProfessionalPromptMode.FULL:
        rendered = render_composed_prompt(
            PromptSpec(role="systems_architect", task="cross_replan", format="detailed"),
            project_dir=_ORCHESTRATOR_ROOT,
            variables=variables,
        )
        dynamic_block = (
            f"TASK:\n{task}\n\n"
            f"PROJECTS INVOLVED:\n{paths_list}\n\n"
            f"ALIASES: {', '.join(aliases)}\n\n"
            f"{_alias_path_instruction(aliases)}"
        )
        ti = _turn_input_part("cross_replan_input", dynamic_block)
        fb = _feedback_part(
            "cross_replan_critique",
            f"Reviewer critique on previous cross-plan:\n{critique}",
        )
        extra_parts = tuple(p for p in (ti, fb) if p is not None)
    else:
        intent = minimal_intents.cross_replan_intent(
            task, critique, aliases=", ".join(aliases),
        )
        if mode is ProfessionalPromptMode.MINIMAL_WITH_FORMAT:
            from pipeline.prompts.builders import (
                _append_format,
                _render_format_only,
            )
            rendered = _append_format(
                intent,
                _render_format_only(
                    "detailed",
                    project_dir=_ORCHESTRATOR_ROOT,
                    variables=variables,
                ),
            )
        else:
            rendered = intent
    rendered = _append_path_alias_instruction(rendered, aliases)
    return _render_prompt_output(
        rendered,
        system_tail=(
            plan_artifact_boundary_contract(),
            cross_plan_json_contract(
                body_language=plan_language,
                input_language=getattr(cfg, "task_language", None),
            ),
            authoring_language_strategy(task_language=plan_language),
        ),
        extra_upper_parts=extra_parts,
    )


def contract_review_focus(task: str, projects: dict[str, Path]) -> PromptTurn:
    """Build the cross-project contract-check focus prompt."""
    from core.infra.config import AppConfig
    from pipeline.prompts.builders import _render_prompt_output
    from pipeline.prompts.contracts import review_json_contract

    cfg = AppConfig.load()
    body = (
        f"This is a CROSS-PROJECT implementation review.\n"
        f"Task: {task[:200]}\n\n"
        f"Projects changed: "
        f"{', '.join(f'{a} ({Path(p).name})' for a, p in projects.items())}\n\n"
        "Check for CROSS-PROJECT consistency:\n"
        "- API response field names match what Unity/client expects\n"
        "- DB column names match what Stats SQL queries use\n"
        "- Event/payload field names are consistent across all projects\n"
        "- Any hardcoded values (IDs, string constants) are the same everywhere\n"
        "- Version/schema mismatches between producer and consumer"
    )
    return _render_prompt_output(
        body,
        system_tail=(review_json_contract(body_language=cfg.task_language),),
    )


__all__ = [
    "_ORCHESTRATOR_ROOT",
    "contract_review_focus",
    "cross_plan_prompt",
    "cross_plan_review_focus",
    "cross_replan_prompt",
    "set_orchestrator_root",
]
