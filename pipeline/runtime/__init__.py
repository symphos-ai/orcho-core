"""
pipeline.runtime — data-driven pipeline runtime subdomain.

Phase 2 of the runtime/subdomain refactor split the monolithic
``pipeline/runtime.py`` into intent-keyed submodules:

    roles.py    — agent role, execution mode, and policy ``StrEnum``s.
    steps.py    — per-step dataclasses (``PhaseStep``, ``QualityGate``,
                  ``HumanReview``, ``Attachment``); re-exports
                  ``PromptSpec`` from ``pipeline.prompts.spec``.
    profile.py  — ``Profile``, ``LoopStep``, ``PipelineProfile``.
    state.py    — ``PipelineState`` run state model.
    results.py  — ``PhaseHandler`` protocol + ``PhaseRegistry``.
    runner.py   — ``run_profile`` data-driven walker.

This module re-exports the long-standing public surface so callers
keep using ``from pipeline.runtime import ...``. New call sites are
encouraged to import from the specific submodule when scope is known.
"""

from __future__ import annotations

from pipeline.prompts.spec import PromptSpec
from pipeline.runtime.profile import (
    ContractCheckMode,
    CrossGatePolicy,
    CrossGateRunPolicy,
    CrossGateSkipPolicy,
    ExecutionPolicy,
    ExecutionSurface,
    LoopStep,
    PhaseEntry,
    PipelineProfile,
    Profile,
)
from pipeline.runtime.results import PhaseHandler, PhaseRegistry
from pipeline.runtime.roles import (
    AgentRole,
    AttachmentKind,
    ChangeHandoffMode,
    EffortLevel,
    ExecutionMode,
    FailStrategy,
    FullCycleDepth,
    GateKind,
    HumanAction,
    ImplementationExecution,
    PhaseHandoffAction,
    PhaseHandoffType,
    ProfileKind,
    ReviewTiming,
    ScopedTarget,
    ScopeExpansionSanction,
    SessionContinuity,
    SessionInvocationRole,
)
from pipeline.runtime.run_shape import (
    OperatingMode,
    OperatingModePolicy,
    RunShape,
    ScopeExpansionSanctionPolicy,
    SemanticProfile,
    coerce_operating_mode,
    operating_mode_from_state,
)

# Private runner helpers that pre-refactor monolithic ``runtime.py`` exposed
# at module top level. Kept reachable via ``pipeline.runtime`` so existing
# callers in ``pipeline.lifecycle``, ``pipeline.phases.builtin``, and a few
# tests do not have to chase a deeper module path. New call sites should
# import from ``pipeline.runtime.runner`` directly.
from pipeline.runtime.runner import (  # noqa: F401
    _PHASESTEP_EXECUTION_SUPPORTED,
    _dispatch_one,
    _dispatch_via_fsm,
    _evaluate_until,
    _fire_step_quality_gates,
    _run_loop_step,
    _stuff_legacy_test_result,
    _validate_v2_entries,
    run_profile,
)
from pipeline.runtime.scope_expansion_sanction import (
    ScopeExpansionDisposition,
    project_scope_expansion_sanction,
)
from pipeline.runtime.semantic_mode_defaults import default_operating_mode
from pipeline.runtime.session_disposition import SessionDisposition, decide
from pipeline.runtime.state import PipelineState
from pipeline.runtime.steps import (
    Attachment,
    CrossScope,
    CrossStepPolicy,
    HumanReview,
    HypothesisPrelude,
    PhaseHandoffPolicy,
    PhaseStep,
    QualityGate,
)

__all__ = [
    # Enums.
    "AgentRole",
    "AttachmentKind",
    "ChangeHandoffMode",
    "EffortLevel",
    "ExecutionMode",
    "FailStrategy",
    "FullCycleDepth",
    "GateKind",
    "HumanAction",
    "ImplementationExecution",
    "PhaseHandoffAction",
    "PhaseHandoffType",
    "ProfileKind",
    "ReviewTiming",
    "ScopedTarget",
    "ScopeExpansionSanction",
    "SessionContinuity",
    "SessionInvocationRole",
    # Semantic profile (Stage B inert types).
    "OperatingMode",
    "OperatingModePolicy",
    "RunShape",
    "ScopeExpansionSanctionPolicy",
    "SemanticProfile",
    # Semantic default-mode projection (pure helper).
    "default_operating_mode",
    # OperatingMode state-stamp readers (single sanction-mode source).
    "coerce_operating_mode",
    "operating_mode_from_state",
    # Session-disposition projection (pure policy).
    "SessionDisposition",
    "decide",
    # Scope-expansion sanction projection (pure policy, ADR 0112 §5).
    "ScopeExpansionDisposition",
    "project_scope_expansion_sanction",
    # Per-step dataclasses.
    "Attachment",
    "CrossScope",
    "CrossStepPolicy",
    "HumanReview",
    "HypothesisPrelude",
    "PhaseHandoffPolicy",
    "PhaseStep",
    "PromptSpec",
    "QualityGate",
    # Cross-gate policy.
    "ContractCheckMode",
    "CrossGatePolicy",
    "CrossGateRunPolicy",
    "CrossGateSkipPolicy",
    # Profile shapes.
    "ExecutionPolicy",
    "ExecutionSurface",
    "LoopStep",
    "PhaseEntry",
    "PipelineProfile",
    "Profile",
    # Run state + dispatch surface.
    "PhaseHandler",
    "PhaseRegistry",
    "PipelineState",
    # Runner.
    "run_profile",
]
