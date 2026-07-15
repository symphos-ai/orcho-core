"""Public SDK surface for Orcho.

The headless library boundary every embedder calls — `from sdk import …`.
Returns typed dataclasses, raises typed errors, never prints, never calls
`sys.exit`. JSON-serialisable through `to_jsonable` for IPC consumers.

See `docs/adr/0021-public-sdk-boundary.md` and `docs/reference/sdk_api.md`.
"""
from __future__ import annotations

# Canonical resume/terminal classification predicates. Re-exported additively
# from the control layer so MCP/CLI consume one source of truth. These are pure
# predicate functions (no dataclass payload, no wire schema); the re-export
# changes no signatures or behavior.
from pipeline.control.continuation import ContinuationDecision, resolve_continuation_decision
from pipeline.control.resume_context import (
    get_resume_intent_options,
    is_terminal_commit_decision_fix,
    is_terminal_commit_decision_halt,
    is_terminal_commit_delivery_pending,
    is_terminal_commit_delivery_scope_blocked,
    is_terminal_final_acceptance_rejected,
    is_terminal_phase_handoff_halt,
    is_terminal_resume_parent,
    is_terminal_success,
)

# Internal helpers exposed for embedders building IPC bridges
from sdk._jsonable import to_jsonable
from sdk.cost import aggregate_cost

# Errors
from sdk.errors import (
    EvidenceInvalid,
    InvalidPhaseHandoffState,
    NoWorkspace,
    OrchoError,
    PricingFetchError,
    ProfileCustomizeError,
    PromptNotFound,
    RunNotFound,
    WorkspaceInitError,
)
from sdk.events import list_events
from sdk.evidence import (
    collect_evidence,
    render_evidence_md,
    write_evidence_bundle,
)
from sdk.evidence_slices import (
    ArtifactRecord as EvidenceArtifactRecord,
    CommandRecord as EvidenceCommandRecord,
    CriterionReport,
    ErrorsAndHalt,
    Finding,
    HandoffAdviceCall,
    HandoffAdviceEvidence,
    HandoffAdviceSummary,
    HandoffAdviceUsage,
    PlanSummary,
    ProviderAccessRecovery,
    RecoveryReplacement,
    SubRunLink,
    SubtaskReceipt,
    get_errors_halt,
    get_plan_summary,
    list_artifacts as list_evidence_artifacts,
    list_commands as list_evidence_commands,
    list_findings,
    list_handoff_advice,
    list_sub_runs,
    list_subtask_receipts,
)
from sdk.fine_tune import FineTuneResult, fine_tune_project
from sdk.handoff_advice import (
    HandoffAdviceResult,
    HandoffAdviceSafety,
    request_handoff_advice,
)
from sdk.history import list_history
from sdk.metrics import get_run_metrics, list_metrics
from sdk.phase_handoff import (
    PhaseHandoffDecision,
    load_active_phase_handoff,
    load_phase_handoff_decision,
    load_phase_handoff_decisions,
    phase_handoff_decide,
    safe_handoff_id,
)
from sdk.pricing import refresh_pricing, show_pricing
from sdk.profile_customize import ProfileCustomizeResult, customize_profile
from sdk.profiles import ProfileSummary, catalogue_path, list_profiles
from sdk.prompts import list_prompts, resolve_prompt

# Run-control delivery decisions — the out-of-band post-release gate surface
from sdk.run_control.delivery import decide_delivery, delivery_decision_state

# Run-diagnosis read-model (ADR 0114) — a core-owned classifier of a run's
# resume situation. Exported additively here; it is MCP-visible and intended
# for consumption by the follow-up P1-mcp migration without a wire break.
from sdk.run_control.diagnosis import run_diagnosis
from sdk.run_control.recovery_lineage import recovery_lineage
from sdk.run_control.types import (
    DeliveryDecisionCommand,
    DeliveryDecisionResult,
    DeliveryDecisionState,
    RecoveryLineage,
    RunDiagnosis,
)
from sdk.run_diff import (
    RunDiffFileRecord,
    RunDiffRecord,
    get_run_diff,
)

# Runner — pipeline launch
from sdk.runner import (
    build_orch_argv,
    run_cross_from_args,
    run_cross_pipeline,
    run_pipeline,
    run_pipeline_from_args,
)

# Runs / status / history / evidence — the REA-4 read surface
from sdk.runs import find_run, find_runs_dir, load_meta

# CLI agentic runtime detection — used by `orcho workspace init`
from sdk.runtimes import DetectedRuntime, detect_cli_runtimes
from sdk.status import load_status

# Public dataclass shapes
from sdk.types import (
    AgentBreakdown,
    ArtefactRef,
    CostReport,
    EvidenceBundle,
    GateStatus,
    PhaseBreakdown,
    PhaseStatus,
    PricingTable,
    ProjectBreakdown,
    PromptResolution,
    RefreshResult,
    RunEvent,
    RunMeta,
    RunMetrics,
    RunRef,
    RunStatus,
    RunSummary,
)
from sdk.verification_timeline import (
    ReceiptEvidence,
    ScheduledGateEvent,
    ScheduledGateRow,
    VerificationTimelineProjection,
    get_verification_timeline,
)
from sdk.verify import (
    CommandOutcome,
    VerifyEnvError,
    VerifyEnvResult,
    VerifyListResult,
    VerifyRunResult,
    verify_env,
    verify_list,
    verify_run,
)

# Workspace bootstrap — `orcho workspace init` user-facing surface
from sdk.workspace import (
    DetectedProject,
    ExtraProject,
    UndetectedCandidate,
    WorkspaceInitResult,
    discover_undetected_candidates,
    init_workspace,
    preflight_workspace_target,
)

__all__ = [
    # Errors
    "OrchoError",
    "NoWorkspace",
    "RunNotFound",
    "PricingFetchError",
    "PromptNotFound",
    "EvidenceInvalid",
    "InvalidPhaseHandoffState",
    "WorkspaceInitError",
    "ProfileCustomizeError",
    "VerifyEnvError",
    # Serialisation
    "to_jsonable",
    # Read surface
    "find_runs_dir",
    "find_run",
    "load_meta",
    "load_status",
    "list_history",
    "collect_evidence",
    "render_evidence_md",
    "write_evidence_bundle",
    "get_run_metrics",
    "list_metrics",
    "list_events",
    "list_prompts",
    "resolve_prompt",
    "list_profiles",
    "catalogue_path",
    "customize_profile",
    "ProfileCustomizeResult",
    "show_pricing",
    "refresh_pricing",
    "aggregate_cost",
    # Canonical resume/terminal classification predicates (additive re-export)
    "is_terminal_resume_parent",
    "is_terminal_success",
    "is_terminal_phase_handoff_halt",
    "is_terminal_commit_decision_halt",
    "is_terminal_commit_decision_fix",
    "is_terminal_commit_delivery_pending",
    "is_terminal_commit_delivery_scope_blocked",
    "is_terminal_final_acceptance_rejected",
    "get_resume_intent_options",
    "ContinuationDecision",
    "resolve_continuation_decision",
    # Generic phase handoff
    "phase_handoff_decide",
    "load_active_phase_handoff",
    "load_phase_handoff_decision",
    "load_phase_handoff_decisions",
    "safe_handoff_id",
    "PhaseHandoffDecision",
    # Read-only handoff advisory accessor (Stage 0/1 advisor)
    "request_handoff_advice",
    "HandoffAdviceResult",
    "HandoffAdviceSafety",
    # Post-release delivery decisions (ADR 0100)
    "decide_delivery",
    "delivery_decision_state",
    "DeliveryDecisionCommand",
    "DeliveryDecisionResult",
    "DeliveryDecisionState",
    # Run-diagnosis read-model (ADR 0114) — MCP-visible, additive
    "run_diagnosis",
    "RunDiagnosis",
    "recovery_lineage",
    "RecoveryLineage",
    # Evidence inspection slices (REA-4.3)
    "Finding",
    "PlanSummary",
    "EvidenceCommandRecord",
    "EvidenceArtifactRecord",
    "ErrorsAndHalt",
    "ProviderAccessRecovery",
    "RecoveryReplacement",
    "SubRunLink",
    "SubtaskReceipt",
    "CriterionReport",
    "list_findings",
    "get_plan_summary",
    "list_evidence_commands",
    "list_evidence_artifacts",
    "get_errors_halt",
    "list_sub_runs",
    "list_subtask_receipts",
    # Handoff-advice evidence projection (Stage 0/1 advisor surface)
    "list_handoff_advice",
    "HandoffAdviceEvidence",
    "HandoffAdviceCall",
    "HandoffAdviceSummary",
    "HandoffAdviceUsage",
    # Run diff viewer
    "RunDiffFileRecord",
    "RunDiffRecord",
    "get_run_diff",
    # Runner
    "run_pipeline",
    "run_cross_pipeline",
    "build_orch_argv",
    "run_pipeline_from_args",
    "run_cross_from_args",
    # Workspace bootstrap
    "DetectedProject",
    "DetectedRuntime",
    "ExtraProject",
    "UndetectedCandidate",
    "detect_cli_runtimes",
    "discover_undetected_candidates",
    "WorkspaceInitResult",
    "init_workspace",
    "preflight_workspace_target",
    # Verify
    "verify_env",
    "VerifyEnvResult",
    "verify_list",
    "verify_run",
    "VerifyListResult",
    "VerifyRunResult",
    "CommandOutcome",
    # Verification timeline projection (read-only, durable)
    "get_verification_timeline",
    "VerificationTimelineProjection",
    "ReceiptEvidence",
    "ScheduledGateEvent",
    "ScheduledGateRow",
    # Fine-tune
    "fine_tune_project",
    "FineTuneResult",
    # Types
    "ProfileSummary",
    "RunRef",
    "RunMeta",
    "GateStatus",
    "PhaseStatus",
    "RunStatus",
    "RunSummary",
    "RunMetrics",
    "RunEvent",
    "ArtefactRef",
    "PhaseBreakdown",
    "AgentBreakdown",
    "ProjectBreakdown",
    "CostReport",
    "EvidenceBundle",
    "PromptResolution",
    "PricingTable",
    "RefreshResult",
]
