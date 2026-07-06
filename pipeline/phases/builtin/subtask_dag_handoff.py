# SPDX-License-Identifier: Apache-2.0
"""ADR 0073 — phase-handoff handler for the ``subtask_dag`` implement path.

When a ``subtask_dag`` implement run leaves one or more subtasks INCOMPLETE
(the invocation succeeded but the typed done-criteria attestation did not
close), the implement step's ``handoff`` policy governs what happens next. This
module owns that decision so the logic stays OUT of the already-large
``pipeline/phases/builtin/subtask_dag.py`` (the hook there just calls in).

The flow, in order:

1. **Bounded auto-repair** — re-run only the incomplete subtasks (done ones
   ride along as read-only context) for up to ``policy.repair_attempts`` rounds
   via :func:`pipeline.subtask_substance_repair.run_substance_repair`. If every
   incomplete subtask closes, delivery is ``repaired`` and the run continues.
2. **Exhaustion** — if repair attempts are spent and incomplete work remains,
   the policy's ``on_exhausted`` decides:

   * ``auto_waiver`` **and** the operator opt-in
     ``state.extras['auto_waiver_allowed']`` is True → an in-process
     auto-waiver (§2/§4): a synthetic ``continue_with_waiver`` decision is
     recorded through the T9 API (:func:`apply_waiver_to_state` +
     :func:`sdk.phase_handoff.write_synthetic_waiver_decision` with
     ``skip_status_guard=True``), ``decided_by='auto:on_exhausted'``, delivery
     is ``waived``, and the run continues. The public
     ``phase_handoff_decide`` is NEVER called (the run is still ``running`` —
     its pause-status guard would reject, and faking a pause is forbidden).
   * otherwise (``halt`` default, or ``auto_waiver`` without the opt-in) → a
     non-loop ``PhaseHandoffRequested`` signal (§1) is raised on
     ``state.phase_handoff_request`` so the orchestrator pauses the run for an
     operator decision.

A retry-mode resume (``state.extras['implement_retry']`` present) forces at
least one repair pass over the incomplete ids even when ``repair_attempts`` is
0, since the operator explicitly asked to retry.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pipeline.runtime.handoff import PhaseHandoffRequested
from pipeline.runtime.roles import PhaseHandoffAction

if TYPE_CHECKING:
    from pathlib import Path

    from pipeline.dag_runner import ImplementationReceipt
    from pipeline.plan_parser import ParsedPlan
    from pipeline.runtime.state import PipelineState
    from pipeline.runtime.steps import PhaseHandoffPolicy
    from pipeline.subtask_substance_repair import PriorContext, RepairPass

#: First-generation handoff id for the implement substance-repair handoff (§1).
IMPLEMENT_HANDOFF_ID = "implement:implement_handoff:1"
#: Round-extras key for the (single-shot) implement handoff round.
IMPLEMENT_HANDOFF_ROUND_KEY = "implement_handoff"
#: Applier-set provenance recorded on an auto-waiver (§2/§4).
AUTO_WAIVER_DECIDED_BY = "auto:on_exhausted"

#: Full action set offered on an implement-handoff pause (§1).
_IMPLEMENT_HANDOFF_ACTIONS: tuple[str, ...] = (
    PhaseHandoffAction.CONTINUE.value,
    PhaseHandoffAction.RETRY_FEEDBACK.value,
    PhaseHandoffAction.CONTINUE_WITH_WAIVER.value,
    PhaseHandoffAction.HALT.value,
)


def _implement_handoff_id(round_n: int) -> str:
    return f"implement:{IMPLEMENT_HANDOFF_ROUND_KEY}:{round_n}"


def _next_implement_handoff_round(state: PipelineState) -> int:
    """Return the first implement-handoff generation without a decision.

    Implement handoffs are single-shot, but a retry can re-enter the same
    incomplete delivery gate. Reusing ``implement:implement_handoff:1`` after an
    operator recorded ``retry_feedback`` makes a later waiver/continue/halt look
    like an attempt to overwrite the old decision. Treat each re-pause as a new
    generation keyed by the durable decision artifacts.
    """
    run_dir = state.output_dir
    if run_dir is None:
        return 1

    from sdk.phase_handoff import safe_handoff_id

    round_n = 1
    decisions_dir = run_dir / "phase_handoff_decisions"
    while True:
        decision_path = (
            decisions_dir / f"{safe_handoff_id(_implement_handoff_id(round_n))}.json"
        )
        if not decision_path.exists():
            return round_n
        try:
            payload = json.loads(decision_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return round_n
        if payload.get("action") != PhaseHandoffAction.RETRY_FEEDBACK.value:
            return round_n
        round_n += 1


def _log_subtask_auto_repair_banner(
    *,
    incomplete_ids: tuple[str, ...],
    missing_ids: tuple[str, ...],
    done_context: Mapping[str, PriorContext],
    attempts: int,
    on_exhausted: str,
    retry_mode: bool,
) -> None:
    """Show the operator that Orcho is entering automatic subtask repair."""
    if attempts <= 0 or not incomplete_ids:
        return

    from agents.stream_log import write_agent_log_section
    from core.io.ansi import C

    lines = [
        "mode: retry_feedback" if retry_mode else "mode: auto_repair",
        "reason: incomplete subtask done-criteria attestation",
        f"repair_subtasks: {', '.join(incomplete_ids)}",
        f"repair_attempts_budget: {attempts}",
        f"already_done_context: {len(done_context)}",
        f"missing_receipts: {', '.join(missing_ids) if missing_ids else '(none)'}",
        f"on_exhausted: {on_exhausted}",
    ]
    write_agent_log_section(
        "ORCHO subtask attestation auto-fix",
        "\n".join(lines),
        label_codes=(C.YELLOW, C.BOLD),
        content_key_codes=(C.GREY,),
        separator_codes=(C.GREY,),
        exit_codes=(C.GREY,),
    )
    # Summary mode: an additional compact one-line card next to the durable
    # section above. The section's stdout echo is off in summary (only the
    # file sink runs), so there is no double print; live/debug skip this
    # branch entirely and keep the full multi-line block byte-identical.
    from core.observability.logging import get_output_mode
    if get_output_mode() == "summary":
        from core.io import summary_lines
        mode = "retry_feedback" if retry_mode else "auto_repair"
        print(f"  {summary_lines.autofix_line(mode, incomplete_ids, attempts, on_exhausted)}")


@dataclass(frozen=True)
class SubtaskDagHandoffOutcome:
    """Result of :func:`handle_subtask_dag_handoff`.

    Exactly one delivery resolution per call:

    * ``delivery_status='repaired'`` + ``paused=False`` — auto-repair closed
      every incomplete subtask.
    * ``delivery_status='waived'`` + ``paused=False`` — eligible auto-waiver
      fired; ``waiver`` / ``waiver_id`` / ``decided_by`` / ``action`` are set
      and a synthetic decision artifact was recorded.
    * ``delivery_status='incomplete'`` + ``paused=True`` — exhausted and not
      eligible for auto-waiver; ``signal`` carries the PhaseHandoffRequested
      raised on ``state.phase_handoff_request``.
    """
    delivery_status: str
    paused: bool
    repaired_ids: tuple[str, ...] = ()
    still_incomplete_ids: tuple[str, ...] = ()
    missing_ids: tuple[str, ...] = ()
    attempts_used: int = 0
    retry_mode: bool = False
    waiver: dict[str, Any] | None = None
    waiver_id: str | None = None
    decided_by: str | None = None
    action: str | None = None
    signal: PhaseHandoffRequested | None = None
    repair_receipts: tuple[ImplementationReceipt, ...] = field(
        default_factory=tuple
    )


def _synthesize_waiver_text(
    still_incomplete_ids: tuple[str, ...],
    attestation_incomplete: Mapping[str, str],
    missing_ids: tuple[str, ...],
    attempts_used: int,
) -> str:
    """Build a non-empty waiver rationale for an auto-waiver.

    :func:`pipeline.project.handoff_waiver.apply_waiver_to_state` rejects an
    empty rationale, so the auto-waiver path always synthesizes a concrete one
    from the incomplete ids (and their attestation-gate reasons) AND the
    missing-receipt ids — a delivery can be waived because subtasks never
    produced a receipt, not only because their criteria did not close.
    """
    n_inc = len(still_incomplete_ids)
    n_miss = len(missing_ids)
    headline = (
        f"Auto-waived after {attempts_used} substance-repair attempt(s) "
        f"(on_exhausted=auto_waiver): {n_inc} subtask(s) remained incomplete"
    )
    headline += f", {n_miss} produced no delivery receipt." if n_miss else "."
    parts = [headline]
    for sid in still_incomplete_ids:
        reason = attestation_incomplete.get(sid) or "criteria not closed"
        parts.append(f"- {sid}: {reason}")
    for sid in missing_ids:
        parts.append(f"- {sid}: no delivery receipt")
    return "\n".join(parts)


def _build_handoff_signal(
    state: PipelineState,
    policy: PhaseHandoffPolicy,
    still_incomplete_ids: tuple[str, ...],
    missing_ids: tuple[str, ...],
    attestation_incomplete: Mapping[str, str],
    findings: Any,
    last_output: str,
) -> PhaseHandoffRequested:
    """Build the §1 non-loop PhaseHandoffRequested for an exhausted run."""
    round_n = _next_implement_handoff_round(state)
    return PhaseHandoffRequested(
        handoff_id=_implement_handoff_id(round_n),
        phase="implement",
        type=policy.type,
        trigger="incomplete",
        verdict="INCOMPLETE",
        approved=False,
        round_extras_key=IMPLEMENT_HANDOFF_ROUND_KEY,
        round=round_n,
        loop_max_rounds=1,
        available_actions=_IMPLEMENT_HANDOFF_ACTIONS,
        artifacts={
            "findings": findings,
            "incomplete_subtasks": list(still_incomplete_ids),
            "attestation_incomplete": dict(attestation_incomplete),
            "missing_subtask_receipts": list(missing_ids),
        },
        last_output=last_output,
    )


def handle_subtask_dag_handoff(
    state: PipelineState,
    *,
    policy: PhaseHandoffPolicy,
    parsed_plan: ParsedPlan,
    incomplete_ids: tuple[str, ...],
    missing_ids: tuple[str, ...],
    attestation_incomplete: Mapping[str, str],
    findings: Any,
    done_context: Mapping[str, PriorContext],
    repair_pass: RepairPass,
    last_output: str = "",
) -> SubtaskDagHandoffOutcome:
    """Resolve an incomplete ``subtask_dag`` delivery per the implement policy.

    See the module docstring for the full flow. ``repair_pass`` is the bound
    DAG executor (the hook in ``subtask_dag.py`` binds it to
    ``run_dag_sequential`` with the live project/registry config; tests inject
    a fake), so this function performs no agent/session I/O itself beyond the
    waiver-artifact write on the eligible auto-waiver branch.
    """
    from pipeline.subtask_substance_repair import run_substance_repair

    incomplete_ids = tuple(incomplete_ids)
    missing_ids = tuple(missing_ids)
    retry_mode = bool(state.extras.get("implement_retry"))

    attempts = policy.repair_attempts
    if retry_mode and attempts < 1:
        # An operator-directed retry explicitly asks for another pass even when
        # the automatic budget is 0.
        attempts = 1

    _log_subtask_auto_repair_banner(
        incomplete_ids=incomplete_ids,
        missing_ids=missing_ids,
        done_context=done_context,
        attempts=attempts,
        on_exhausted=policy.on_exhausted,
        retry_mode=retry_mode,
    )

    repair = run_substance_repair(
        parsed_plan=parsed_plan,
        incomplete_ids=incomplete_ids,
        done_context=done_context,
        repair_attempts=attempts,
        repair_pass=repair_pass,
    )

    # Repaired iff every blocking subtask closed: nothing incomplete remains
    # AND there were no missing receipts (repair only re-runs incomplete ids).
    if incomplete_ids and repair.all_repaired and not missing_ids:
        return SubtaskDagHandoffOutcome(
            delivery_status="repaired",
            paused=False,
            repaired_ids=repair.repaired_ids,
            attempts_used=repair.attempts_used,
            retry_mode=retry_mode,
            repair_receipts=repair.receipts,
        )

    still = repair.still_incomplete_ids or incomplete_ids

    eligible = (
        policy.on_exhausted == "auto_waiver"
        and bool(state.extras.get("auto_waiver_allowed"))
    )
    if eligible:
        return _record_auto_waiver(
            state,
            still_incomplete_ids=still,
            missing_ids=missing_ids,
            attestation_incomplete=attestation_incomplete,
            findings=findings,
            last_output=last_output,
            attempts_used=repair.attempts_used,
            repaired_ids=repair.repaired_ids,
            retry_mode=retry_mode,
            repair_receipts=repair.receipts,
        )

    # Ineligible (halt, or auto_waiver without the opt-in) → pause for operator.
    signal = _build_handoff_signal(
        state, policy, still, missing_ids, attestation_incomplete, findings,
        last_output,
    )
    state.phase_handoff_request = signal
    return SubtaskDagHandoffOutcome(
        delivery_status="incomplete",
        paused=True,
        repaired_ids=repair.repaired_ids,
        still_incomplete_ids=still,
        missing_ids=missing_ids,
        attempts_used=repair.attempts_used,
        retry_mode=retry_mode,
        signal=signal,
        repair_receipts=repair.receipts,
    )


def _record_auto_waiver(
    state: PipelineState,
    *,
    still_incomplete_ids: tuple[str, ...],
    missing_ids: tuple[str, ...],
    attestation_incomplete: Mapping[str, str],
    findings: Any,
    last_output: str,
    attempts_used: int,
    repaired_ids: tuple[str, ...],
    retry_mode: bool,
    repair_receipts: tuple[ImplementationReceipt, ...],
) -> SubtaskDagHandoffOutcome:
    """In-process auto-waiver (§2/§4): durable state waiver + synthetic artifact.

    Goes through the ready T9 API — never the public ``phase_handoff_decide``
    (the run is still ``running``; its pause-status guard would reject and a
    fake pause is forbidden). The state waiver is mirrored to the session at
    phase-end by the existing sync; the synthetic decision artifact is written
    here, idempotently, with ``skip_status_guard=True``.
    """
    from pipeline.project.handoff_waiver import apply_waiver_to_state
    from sdk.phase_handoff import write_synthetic_waiver_decision

    handoff_id = _implement_handoff_id(_next_implement_handoff_round(state))
    waiver_text = _synthesize_waiver_text(
        still_incomplete_ids, attestation_incomplete, missing_ids, attempts_used,
    )
    waiver = apply_waiver_to_state(
        state,
        handoff_id=handoff_id,
        phase="implement",
        waiver_text=waiver_text,
        decided_by=AUTO_WAIVER_DECIDED_BY,
        findings=findings,
        critique=last_output,
    )

    run_dir: Path | None = state.output_dir
    if run_dir is not None:
        run_id = state.extras.get("run_id") or run_dir.name
        write_synthetic_waiver_decision(
            run_dir,
            run_id=run_id,
            handoff_id=handoff_id,
            phase="implement",
            feedback=waiver_text,
            decided_at=waiver["decided_at"],
        )

    return SubtaskDagHandoffOutcome(
        delivery_status="waived",
        paused=False,
        repaired_ids=repaired_ids,
        still_incomplete_ids=still_incomplete_ids,
        missing_ids=missing_ids,
        attempts_used=attempts_used,
        retry_mode=retry_mode,
        waiver=waiver,
        waiver_id=handoff_id,
        decided_by=AUTO_WAIVER_DECIDED_BY,
        action=PhaseHandoffAction.CONTINUE_WITH_WAIVER.value,
        repair_receipts=repair_receipts,
    )
