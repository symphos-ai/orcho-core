# SPDX-License-Identifier: Apache-2.0
"""Lifecycle, handoff and trace leaf helpers for the builtin phase handlers.

Pure resolvers that read run policy off ``PipelineState`` (handoff
strategy, implementation-execution mode, project dir, cross-handoff
contract), build/return the FSM ``LifecycleContext``, carry trace
metadata across a handler's phase-log overwrite, and classify guardrail
output. No back-import into ``pipeline.phases.builtin`` — heavy or
order-sensitive imports stay lazy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agents.command_guard import ORCHO_GUARDRAIL_BLOCKED
from core.infra.config import AppConfig
from pipeline.runtime import PhaseRegistry

if TYPE_CHECKING:
    from pipeline.runtime import PipelineState, PromptSpec


def _ensure_lifecycle_ctx(state: PipelineState):
    """Phase 5e-5 substep 6b + 6c: return the FSM-populated
    ``LifecycleContext`` from the typed ``state.lifecycle_ctx`` field,
    auto-building a default one when absent.

    Production callers always reach handlers via ``run_profile`` /
    ``_run_loop_step`` / ``PhaseLifecycle.execute_step`` — ctx is
    populated before the handler runs and cleared afterwards.

    Direct unit-test callers (e.g. ``_resolve_fix_runtime_config(state)``
    invoked outside a profile dispatch) historically used lazy imports
    of ``pipeline.project_orchestrator``. This helper builds the same
    default ctx those imports produced and stashes it on the same
    typed field, so handler bodies can read off ``ctx.*_helpers`` /
    ``ctx.*_resolver`` without branching on ``ctx is None``.

    The auto-built ctx is intentionally minimal: no
    ``session_adapter_registry``, no ``on_metrics`` /
    ``on_checkpoint`` callbacks, no provider — only Protocol helpers
    + execution-mode registry. Tests that need real callbacks must
    construct a richer ctx and stash it themselves.

    Phase 5e-5 substep 6c: replaced the substep-4 ad-hoc
    ``state.extras["_lifecycle_ctx"]`` string-keyed channel with the
    typed ``state.lifecycle_ctx`` field — closes Codex review #3
    recommendation #3 (ad-hoc channel hardening).
    """
    if state.lifecycle_ctx is not None:
        return state.lifecycle_ctx
    from pipeline.lifecycle import default_lifecycle_context
    state.lifecycle_ctx = default_lifecycle_context(
        phase_registry=PhaseRegistry(),
    )
    return state.lifecycle_ctx


def _change_handoff_for(state: PipelineState) -> str:
    """Resolved run-level handoff strategy (profile override → config fallback)."""
    return str(state.extras.get("change_handoff") or "uncommitted")


def _implementation_execution_for(state: PipelineState) -> str:
    """Resolved implement executor policy.

    Semantic Profiles will eventually materialise this on RunShape. Until that
    resolver lands, the value is read from state extras (tests / future bridge)
    or the pipeline config section.
    """
    from pipeline.runtime import ImplementationExecution

    raw = (
        state.extras.get("implementation_execution")
        or AppConfig.load().pipeline.get("implementation_execution")
        or ImplementationExecution.WHOLE_PLAN.value
    )
    value = str(raw).strip().lower()
    try:
        return ImplementationExecution(value).value
    except ValueError as exc:
        valid = ", ".join(e.value for e in ImplementationExecution)
        raise ValueError(
            f"unknown implementation_execution={raw!r}; expected one of {valid}"
        ) from exc


def _prompt_from_active_step(ctx) -> PromptSpec | None:
    """Extract ``PromptSpec`` from the active ``PhaseStep.prompt``.

    Phase handlers consume the optional ``PhaseStep.prompt`` to drive
    composable prompt rendering. A5.2a: profile steps no longer carry
    a runtime-role fallback for prompt rendering — profile authors must
    declare ``prompt.role`` explicitly (or rely on the builder's
    prompt-taxonomy default when ``prompt`` is omitted).

    Returns ``None`` for legacy direct-invocation paths where no FSM
    step is active or the step has no ``prompt`` block.
    """
    step = getattr(ctx, "active_step", None)
    if step is None:
        return None
    return step.prompt


def _carry_trace_metadata(state: PipelineState, phase: str) -> dict[str, Any]:
    """Preserve trace metadata when a handler overwrites
    ``state.phase_log[phase]``.

    Several handlers (plan, replan, validate_plan, implement,
    review_changes, repair_changes, final_acceptance) rebuild their
    ``phase_log[phase]`` entry from scratch after parsing the
    agent output. ``_session_aware_invoke`` stamped trace metadata
    under that same key before parsing ran (M12 ``prompt_render``,
    M14.1 ``context_growth``); without explicit preservation the
    handler's overwrite would wipe it.

    Returns a dict suitable for ``**spread`` into the new entry —
    contains only the keys that were actually present and non-None.
    When M14.3+ adds new trace keys (``context_clearing``,
    ``context_pressure`` etc.), extend this helper instead of every
    handler.
    """
    log = state.phase_log.get(phase) or {}
    if not isinstance(log, dict):
        return {}
    out: dict[str, Any] = {}
    for key in (
        "prompt_render",
        "context_growth",
        "context_clearing",
        "context_pressure",
        "runtime_compaction",
    ):
        value = log.get(key)
        if value is not None:
            out[key] = value
    return out


def _guardrail_blocked(output: str) -> bool:
    return ORCHO_GUARDRAIL_BLOCKED in (output or "")


def _handoff_contract_for(state: PipelineState) -> str:
    """Cross-aware handoff block — rendered by the orchestrator and stored
    on ``state.extras['cross_handoff']``.

    Returns ``""`` for mono runs (no cross context). Phase handlers pass
    the result through to the prompt builder which prepends it before the
    plan contract so the agent reads cross-level context first.
    """
    return str(state.extras.get("cross_handoff", "") or "")


def _agent_project_dir(state: PipelineState) -> str:
    """Return the filesystem root agents should treat as the project.

    Worktree isolation routes agent ``cwd`` through ``extras["git_cwd"]``.
    Prompt-visible project anchors must follow that same path; otherwise
    agents can write absolute paths in the user's source checkout while the
    review phases inspect the isolated checkout and see an empty diff.
    """
    return str(state.extras.get("git_cwd") or state.project_dir)
