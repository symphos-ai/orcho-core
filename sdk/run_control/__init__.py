"""Client-neutral run-control read/command model (Stage 4).

Reads durable run artifacts and expresses operator decisions as typed
values. This package never prints, renders, or imports a terminal layer.

Public surface (import as ``from sdk.run_control import ...``):

- types — :class:`RunSnapshot`, :class:`PendingOperatorAction`,
  :class:`PhaseHandoffDecisionCommand`, :class:`ResumeCommand`,
  :class:`CancelCommand`, :class:`RunControlUnsupported`, and the
  re-exported :class:`RunEvent`;
- service — :class:`RunService` (start / snapshot / events /
  decide_handoff / resume / cancel);
- snapshot — :func:`load_run_snapshot`;
- events — :func:`read_run_events`, :func:`tail_run_events`;
- commands — :func:`build_decision_command`.

The package is self-contained: it is not re-exported from the top-level
``sdk`` namespace, so importing it pulls in only the run-control surface.
"""
from __future__ import annotations

from pipeline.run_state import RunStateIssue, RunStateValidationReport
from sdk.run_control.commands import build_decision_command
from sdk.run_control.delivery import decide_delivery, delivery_decision_state
from sdk.run_control.diagnosis import run_diagnosis
from sdk.run_control.events import read_run_events, tail_run_events
from sdk.run_control.launch import (
    CancelResult,
    CorrectionFollowupLaunchRequest,
    FromRunPlanLaunchRequest,
    LaunchedRun,
    LaunchResult,
    LaunchSpec,
    cancel_run,
    launch_correction_followup,
    launch_from_run_plan,
    launch_run,
    read_launch_state,
    resume_run,
    write_launch_state,
)
from sdk.run_control.recovery_lineage import recovery_lineage
from sdk.run_control.service import RunService
from sdk.run_control.snapshots import load_run_snapshot
from sdk.run_control.types import (
    CancelCommand,
    DeliveryDecisionActionValue,
    DeliveryDecisionCommand,
    DeliveryDecisionResult,
    DeliveryDecisionState,
    PendingOperatorAction,
    PhaseHandoffActionValue,
    PhaseHandoffDecisionCommand,
    RecoveryLineage,
    ResumeCommand,
    RunControlUnsupported,
    RunDiagnosis,
    RunEvent,
    RunSnapshot,
)

__all__ = [
    "CancelCommand",
    "CancelResult",
    "CorrectionFollowupLaunchRequest",
    "FromRunPlanLaunchRequest",
    "DeliveryDecisionActionValue",
    "DeliveryDecisionCommand",
    "DeliveryDecisionResult",
    "DeliveryDecisionState",
    "LaunchResult",
    "LaunchSpec",
    "LaunchedRun",
    "PendingOperatorAction",
    "PhaseHandoffActionValue",
    "PhaseHandoffDecisionCommand",
    "RecoveryLineage",
    "ResumeCommand",
    "RunControlUnsupported",
    "RunDiagnosis",
    "RunEvent",
    "RunService",
    "RunSnapshot",
    "RunStateIssue",
    "RunStateValidationReport",
    "build_decision_command",
    "cancel_run",
    "decide_delivery",
    "delivery_decision_state",
    "launch_run",
    "launch_correction_followup",
    "launch_from_run_plan",
    "read_launch_state",
    "recovery_lineage",
    "resume_run",
    "run_diagnosis",
    "load_run_snapshot",
    "read_run_events",
    "tail_run_events",
    "write_launch_state",
]
