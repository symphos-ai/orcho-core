"""pipeline.cross_project — cross-project orchestration subdomain.

The main layers are:

- ``types.py`` — type contracts (``CrossProjectProfile``,
  ``CrossPlanStep``, ``ProjectStep``, ``ProjectRunRef``,
  ``ContractValidation`` / ``ContractResult``, etc.) from the
  Milestone 13 Phase 1 work.
- ``plan_parser.py`` — cross-plan JSON parsing + rendering
  (``parse_cross_plan``, ``render_cross_plan_markdown``, ``ParsedCrossPlan``).
- ``prompts.py`` — cross-level prompt builders for planning, re-planning,
  cross-plan validation, and contract review.
- ``usage.py`` — cross-level runtime usage normalization and rollups.
- ``orchestrator.py`` — the cross-project orchestrator
  (``run_cross_pipeline``, ``parse_projects``, ``build_cross_context``).

Public surface re-exported for short call sites. The cross-plan parse/render
helpers are re-exported from ``plan_parser`` (their home module) so existing
short imports keep working.
"""
from pipeline.cross_project.orchestrator import (
    build_cross_context,
    contract_review_focus,
    cross_plan_prompt,
    cross_plan_review_focus,
    cross_replan_prompt,
    parse_projects,
    run_cross_pipeline,
)
from pipeline.cross_project.plan_parser import (
    CrossPlanParse,
    CrossPlanParseError,
    ParsedCrossPlan,
    aliasize_cross_plan,
    cross_plan_document,
    parse_cross_plan,
    render_cross_plan_markdown,
    write_cross_plan_artifacts,
)
from pipeline.cross_project.types import (
    ArtifactSelector,
    BlockedPolicy,
    ContractResult,
    ContractValidation,
    CrossPlanStep,
    CrossProjectProfile,
    ProjectRunRef,
    ProjectStatus,
    ProjectStep,
    WhenPolicy,
)

__all__ = [
    # Type contracts.
    "ArtifactSelector",
    "BlockedPolicy",
    "ContractResult",
    "ContractValidation",
    "CrossPlanStep",
    "CrossProjectProfile",
    "ProjectRunRef",
    "ProjectStatus",
    "ProjectStep",
    "WhenPolicy",
    # Orchestrator surface.
    "build_cross_context",
    "contract_review_focus",
    "cross_plan_prompt",
    "cross_plan_review_focus",
    "cross_replan_prompt",
    "parse_projects",
    "run_cross_pipeline",
    # Cross-plan parsing + rendering.
    "CrossPlanParse",
    "CrossPlanParseError",
    "ParsedCrossPlan",
    "aliasize_cross_plan",
    "cross_plan_document",
    "parse_cross_plan",
    "render_cross_plan_markdown",
    "write_cross_plan_artifacts",
]
