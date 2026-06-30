"""pipeline.prompts — prompt subdomain.

Re-exports the public surface of the prompt subsystem so the rest of
the codebase can keep importing from ``pipeline.prompts`` without
caring about the internal split into ``builders``, ``composer``,
``contracts``, and ``subtask`` submodules.

Layout:

    builders.py   — high-level prompt builders for each phase
                    (plan / build / review / fix / ...).
    spec.py       — ``PromptSpec`` (pure data declaring which composable
                    role/task/format parts a phase uses).
    composer.py   — ``render_composed_prompt`` / ``render_prompt_parts``
                    for ADR 0009 composable role/task/format parts.
    contracts.py  — code-owned system-tail blocks
                    (``change_handoff``, ``review_target``,
                    ``review_json``, ``plan_json``,
                    ``skill_routing``, ``authoring_language``) +
                    ``compose_prompt`` / ``SystemPromptBlock`` primitives.
    subtask.py    — DAG subtask prompt builder.

Submodules can still be imported directly (e.g.
``from pipeline.prompts.contracts import change_handoff_strategy``)
when callers need a narrower surface.
"""
from __future__ import annotations

from pipeline.prompts.builders import (
    build_fix_prompt,
    build_prompt,
    decompose_plan_prompt,
    fix_prompt,
    hypothesis_file_review_prompt,
    hypothesis_prompt,
    hypothesis_review_focus,
    plan_file_review_prompt,
    plan_prompt,
    plan_review_focus,
    readonly_plan_prompt,
    replan_prompt,
    review_focus,
    runtime_review_uncommitted_prompt,
)
from pipeline.prompts.composer import (
    render_composed_prompt,
    render_prompt_parts,
)
from pipeline.prompts.contracts import (
    PromptEnvelope,
    SystemPromptBlock,
    SystemPromptContract,
    append_system_contract,
    authoring_language_strategy,
    change_handoff_strategy,
    compose_prompt,
    cross_plan_json_contract,
    plan_json_contract,
    release_json_contract,
    review_json_contract,
    review_target_strategy,
    skill_routing_strategy,
)
from pipeline.prompts.spec import PromptSpec
from pipeline.prompts.subtask import build_subtask_prompt

__all__ = [
    # builders
    "build_fix_prompt",
    "build_prompt",
    "decompose_plan_prompt",
    "fix_prompt",
    "hypothesis_prompt",
    "hypothesis_file_review_prompt",
    "hypothesis_review_focus",
    "plan_file_review_prompt",
    "plan_prompt",
    "plan_review_focus",
    "readonly_plan_prompt",
    "replan_prompt",
    "review_focus",
    "runtime_review_uncommitted_prompt",
    # composer
    "PromptSpec",
    "render_composed_prompt",
    "render_prompt_parts",
    # contracts
    "PromptEnvelope",
    "SystemPromptBlock",
    "SystemPromptContract",
    "append_system_contract",
    "authoring_language_strategy",
    "change_handoff_strategy",
    "compose_prompt",
    "cross_plan_json_contract",
    "plan_json_contract",
    "release_json_contract",
    "review_json_contract",
    "review_target_strategy",
    "skill_routing_strategy",
    # subtask
    "build_subtask_prompt",
]
