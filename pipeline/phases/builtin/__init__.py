"""pipeline/phases/builtin/ — builtin phase handlers for the linear profile.

This package is the façade for Orcho's built-in ``orcho.phases`` handlers.
Each handler is a single-arg callable ``(state) -> state`` that pulls its
args from :class:`~pipeline.runtime.PipelineState`, dispatches to the
adapter runners, and stashes results back on the state — same prompts,
same agents, same output; only the call shape differs.

Structure (decomposed from the original monolithic module):

- ``handlers/*`` — one module per ``orcho.phases`` entry point (plan,
  validate_plan, implement, review_changes, repair_changes,
  final_acceptance, compliance_check). The entry-point paths stay
  ``pipeline.phases.builtin:_phase_*`` because the handlers are re-exported
  here.
- ``session_invoke`` / ``session_keys`` / ``session_observability`` — the
  session-aware invocation boundary, physical session addressing, and the
  post-invoke ADR-0029 trace stampers.
- ``plan_artifact`` / ``review_support`` / ``subtask_dag`` / ``prompt_parts``
  / ``lifecycle`` / ``registry`` — focused helper modules.

Halt semantics: ``final_acceptance`` may set ``state.halt`` on a rejected
verdict; ``validate_plan`` defers pause semantics to the generic
phase-handoff machinery (Phase 3 cutover). Loop control lives in the
orchestrator + active profile, not in these adapters.

``__all__`` is the public façade contract: the registry constructors, the
seven phase handlers (entry-point targets), and the handful of internal
helpers white-box tests import from here. All other helper logic lives in
(and is imported from) its real home module, never re-exported.
"""

from __future__ import annotations

from pipeline.phases.builtin.handlers.compliance_check import _phase_compliance_check
from pipeline.phases.builtin.handlers.correction_triage import _phase_correction_triage
from pipeline.phases.builtin.handlers.final_acceptance import _phase_final_acceptance
from pipeline.phases.builtin.handlers.implement import _phase_implement
from pipeline.phases.builtin.handlers.plan import _phase_plan
from pipeline.phases.builtin.handlers.repair_changes import _phase_repair_changes
from pipeline.phases.builtin.handlers.review_changes import _phase_review_changes
from pipeline.phases.builtin.handlers.validate_plan import _phase_validate_plan
from pipeline.phases.builtin.lifecycle import (
    _carry_trace_metadata,
    _ensure_lifecycle_ctx,
)
from pipeline.phases.builtin.plan_artifact import _review_plan_artifact
from pipeline.phases.builtin.registry import default_registry, register_builtin_phases
from pipeline.phases.builtin.review_support import (
    _capture_phase_baseline,
    _print_implement_summary,
    _resolve_fix_runtime_config,
)
from pipeline.phases.builtin.session_invoke import _session_aware_invoke
from pipeline.phases.builtin.session_keys import (
    _compute_session_key,
    _should_continue_prompt_session,
    _should_resume,
    decide_session_continuation,
)
from pipeline.phases.builtin.subtask_dag import _aggregate_subtask_prompt_render

__all__ = [
    # Registry construction (external API; also re-exported by
    # pipeline.phases.__init__).
    "register_builtin_phases",
    "default_registry",
    # Phase-handler entry points (orcho.phases targets).
    "_phase_plan",
    "_phase_validate_plan",
    "_phase_implement",
    "_phase_review_changes",
    "_phase_repair_changes",
    "_phase_final_acceptance",
    "_phase_compliance_check",
    "_phase_correction_triage",
    # Internal helpers white-box tests import from the façade.
    "_aggregate_subtask_prompt_render",
    "_capture_phase_baseline",
    "_carry_trace_metadata",
    "_compute_session_key",
    "_ensure_lifecycle_ctx",
    "_print_implement_summary",
    "_resolve_fix_runtime_config",
    "_review_plan_artifact",
    "_session_aware_invoke",
    "_should_continue_prompt_session",
    "_should_resume",
    "decide_session_continuation",
]
