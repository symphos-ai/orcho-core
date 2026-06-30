"""Client-neutral run-state layer (Stage 0 brain).

A read-only projection of a run's lifecycle folded from its durable event
stream, a non-repairing consistency checker, and an opt-in repair layer for a
strictly limited set of known torn shapes. This package depends at most on
:mod:`core.observability.events`; it is never imported by any runtime / resume
/ finalization path and changes no on-disk schema beyond the repair audit
artifact.

The repair layer (:func:`repair_run_state`) consumes the consistency
diagnosis, defaults to dry-run, and applies only minimal, crash-safe
``meta.json`` mutations when explicitly asked.
"""
from __future__ import annotations

from pipeline.run_state.consistency import validate_run_state
from pipeline.run_state.cross import (
    CrossRunStateSnapshot,
    classify_cross_run_state,
    validate_cross_run_state,
)
from pipeline.run_state.cross_repair import repair_cross_run_state
from pipeline.run_state.handoff import (
    build_handoff_payload,
    build_human_feedback,
    build_phase_handoff_override,
    build_phase_handoff_waiver,
    clear_active_handoff,
    continue_handoff,
    continue_with_waiver_handoff,
    request_active_handoff,
    retry_feedback_handoff,
)
from pipeline.run_state.phase_outcome import is_phase_checkpoint_success
from pipeline.run_state.projector import project_events, project_run_dir
from pipeline.run_state.reducer import apply_run_event
from pipeline.run_state.repair import (
    RunStateRepairAction,
    RunStateRepairChange,
    RunStateRepairReport,
    repair_run_state,
)
from pipeline.run_state.setup_failure import (
    SETUP_FAILURE_KIND,
    detect_setup_preflight_failure,
    merged_halt_reason,
    merged_status,
    supervisor_halt_reason,
    supervisor_terminal_status,
)
from pipeline.run_state.subtask_progress import (
    unfinished_subtask_ids,
    unfinished_subtask_ids_in_run_dir,
)
from pipeline.run_state.terminal import (
    CROSS_HANDOFF_MARKER_KEYS,
    CROSS_SETTLE_RESIDUE_KEYS,
    TRANSIENT_SETTLE_KEYS,
    evict_cross_handoff_markers,
    evict_cross_settle_residue,
    evict_transient_settle_keys,
    mark_run_awaiting_review,
    mark_run_done,
    mark_run_failed,
    mark_run_halted,
    mark_run_interrupted,
    settle_cross_terminal,
)
from pipeline.run_state.terminal_outcome import (
    apply_no_diff_terminal,
    resolve_terminal_outcome,
)
from pipeline.run_state.types import (
    HandoffAction,
    HandoffRetryMode,
    HandoffTransition,
    RunEventType,
    RunStateIssue,
    RunStateSnapshot,
    RunStateValidationReport,
    RunStatus,
    RunTransitionError,
)

__all__ = [
    "CrossRunStateSnapshot",
    "HandoffAction",
    "HandoffRetryMode",
    "HandoffTransition",
    "RunEventType",
    "RunStateIssue",
    "RunStateRepairAction",
    "RunStateRepairChange",
    "RunStateRepairReport",
    "RunStateSnapshot",
    "RunStateValidationReport",
    "RunStatus",
    "RunTransitionError",
    "CROSS_HANDOFF_MARKER_KEYS",
    "CROSS_SETTLE_RESIDUE_KEYS",
    "SETUP_FAILURE_KIND",
    "TRANSIENT_SETTLE_KEYS",
    "apply_no_diff_terminal",
    "apply_run_event",
    "build_handoff_payload",
    "build_human_feedback",
    "build_phase_handoff_override",
    "build_phase_handoff_waiver",
    "classify_cross_run_state",
    "clear_active_handoff",
    "continue_handoff",
    "continue_with_waiver_handoff",
    "detect_setup_preflight_failure",
    "evict_cross_handoff_markers",
    "evict_cross_settle_residue",
    "evict_transient_settle_keys",
    "is_phase_checkpoint_success",
    "mark_run_awaiting_review",
    "mark_run_done",
    "mark_run_failed",
    "mark_run_halted",
    "mark_run_interrupted",
    "merged_halt_reason",
    "merged_status",
    "project_events",
    "project_run_dir",
    "repair_cross_run_state",
    "repair_run_state",
    "request_active_handoff",
    "resolve_terminal_outcome",
    "retry_feedback_handoff",
    "settle_cross_terminal",
    "supervisor_halt_reason",
    "supervisor_terminal_status",
    "unfinished_subtask_ids",
    "unfinished_subtask_ids_in_run_dir",
    "validate_cross_run_state",
    "validate_run_state",
]
