"""One-shot retained-worktree retry for ``verification_gate_failed`` handoffs."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.io.retry import AgentCallError
from pipeline.control.handoff_routing import GateIdentity
from pipeline.run_state import (
    HandoffRetryMode,
    continue_handoff,
    continue_with_waiver_handoff,
    retry_feedback_handoff,
)
from pipeline.runtime.handoff import HUMAN_DIRECTED_FLAG_KEY


class VerificationHandoffRetryBlocked(RuntimeError):
    """A control-plane precondition failed without consuming the recovery subject."""


@dataclass(frozen=True, slots=True)
class VerificationHandoffRetryContext:
    """Canonical identity and round accounting for one human gate retry.

    The active handoff, rather than the fresh retry round, remains the source
    of the automatic loop maximum.  This makes the one-shot retry structurally
    human-directed even when it produces another gate handoff.
    """

    identity: GateIdentity
    prior_round: int
    fresh_round: int
    loop_max_rounds: int
    human_retry_ordinal: int

    @classmethod
    def from_active(
        cls, active: Mapping[str, object], identity: GateIdentity,
    ) -> VerificationHandoffRetryContext:
        prior_round = max(1, int(active.get("round", 1) or 1))
        loop_max_rounds = max(
            1, int(active.get("loop_max_rounds", prior_round) or prior_round),
        )
        fresh_round = prior_round + 1
        return cls(
            identity=identity,
            prior_round=prior_round,
            fresh_round=fresh_round,
            loop_max_rounds=loop_max_rounds,
            human_retry_ordinal=max(1, fresh_round - loop_max_rounds),
        )


def apply_verification_handoff_resume(
    *, run: Any, profile: Any, ctx: Any, active: dict[str, Any], handoff_id: str,
    action: str, feedback: str, note: str | None, decided_at: str,
    identity: GateIdentity,
) -> Any:
    """Resolve every verification-handoff action without entering a phase loop.

    A verification gate may be raised at a terminal phase, but it is a gate
    pause, not a plan or scope-expansion pause.  Closing it therefore only
    advances past the phase that published the gate handoff.
    """
    if action == "retry_feedback":
        try:
            return apply_verification_handoff_retry(
                run=run, profile=profile, ctx=ctx, active=active,
                handoff_id=handoff_id, feedback=feedback, note=note,
                decided_at=decided_at, identity=identity,
            )
        except VerificationHandoffRetryBlocked as exc:
            # A persisted decision can outlive a profile edit that removes
            # repair_changes. Re-park through the ordinary pause tail with a
            # fresh id: the old immutable decision artifact cannot be reused
            # for a different executable action.
            from pipeline.project.gate_repair import (
                repark_verification_handoff_retry_blocked,
            )

            repark_verification_handoff_retry_blocked(
                run, profile=profile, active=active, reason=str(exc),
            )
            return _outcome(profile, paused=True)

    from pipeline.project.handoff import (
        PhaseHandoffResumeOutcome,
        _persist_handoff_running_state,
    )

    phase = active.get("phase")
    completed = frozenset({phase}) if isinstance(phase, str) and phase else frozenset()
    if action == "continue":
        transition = continue_handoff(
            run.session, handoff_id=handoff_id, note=note, decided_at=decided_at,
        )
        run.state.extras["phase_handoff_override"] = transition.override
        _persist_handoff_running_state(run)
        return PhaseHandoffResumeOutcome(profile, completed, False)

    if action == "continue_with_waiver":
        if not feedback.strip():
            raise VerificationHandoffRetryBlocked(
                "verification waiver requires continue_with_waiver feedback",
            )
        artifacts = active.get("artifacts")
        findings = artifacts.get("findings") if isinstance(artifacts, dict) else None
        critique = active.get("last_output")
        transition = continue_with_waiver_handoff(
            run.session,
            handoff_id=handoff_id,
            phase=phase if isinstance(phase, str) else "implement",
            feedback=feedback,
            note=note,
            decided_at=decided_at,
            findings=findings,
            critique=critique if isinstance(critique, str) else "",
        )
        run.session["phase_handoff_waiver"] = transition.waiver
        run.state.extras["phase_handoff_waiver"] = transition.waiver
        run.state.extras["phase_handoff_override"] = transition.override
        _persist_handoff_running_state(run)
        return PhaseHandoffResumeOutcome(profile, completed, False)

    raise VerificationHandoffRetryBlocked(
        f"unsupported verification handoff action {action!r}",
    )


def apply_verification_handoff_retry(
    *, run: Any, profile: Any, ctx: Any, active: dict[str, Any], handoff_id: str,
    feedback: str, note: str | None, decided_at: str, identity: GateIdentity,
) -> Any:
    """Repair once, then re-run one selected gate on a fresh subject.

    All validation precedes ``retry_feedback_handoff`` so malformed routing,
    stale decisions, or absent retained work leave the active handoff available
    for operator recovery. Provider/process exceptions are intentionally not
    caught: their established interrupted/failed lifecycle remains authoritative.
    """
    if not feedback.strip():
        raise VerificationHandoffRetryBlocked("verification retry requires retry_feedback")
    persisted = run.session.get("phase_handoff")
    if not isinstance(persisted, dict) or persisted.get("id") != handoff_id:
        raise VerificationHandoffRetryBlocked("active recovery subject no longer matches decision")
    from pipeline.project.retry_subject import RepairSubjectUnproven, guard_review_retry_subject

    try:
        guard_review_retry_subject(run)
    except RepairSubjectUnproven as exc:
        raise VerificationHandoffRetryBlocked(str(exc)) from exc
    from pipeline.project.gate_repair import _repair_step

    repair_step = _repair_step(profile)
    if repair_step is None:
        raise VerificationHandoffRetryBlocked("verification retry profile has no repair_changes step")

    retry_context = VerificationHandoffRetryContext.from_active(active, identity)

    transition = retry_feedback_handoff(
        run.session, handoff_id=handoff_id, mode=HandoffRetryMode.VERIFICATION,
        feedback=feedback, note=note, decided_at=decided_at,
    )
    run.state.extras["phase_handoff_override"] = transition.override
    run.state.extras["human_feedback"] = transition.human_feedback
    run.state.human_feedback = feedback
    from pipeline.project.handoff import _persist_handoff_running_state
    _persist_handoff_running_state(run)

    try:
        _dispatch_one_repair(
            run,
            repair_step,
            ctx,
            retry_context=retry_context,
        )
    except AgentCallError:
        # Provider/process failures retain their established lifecycle handling;
        # they are not operator control-plane blockers.
        raise
    except (RuntimeError, ValueError) as exc:
        _restore_recovery_subject(run, active)
        raise VerificationHandoffRetryBlocked(str(exc)) from exc
    if getattr(run.state, "halt", False):
        return _outcome(profile, paused=False)
    from pipeline.project.gate_repair import rerun_verification_handoff_gate
    from pipeline.project.handoff import _persist_handoff_retry_metrics

    # The FSM remains the sole owner of the repair_changes attempt.  Preserve
    # its completed attempt before the exact-gate rerun can publish a new pause.
    _persist_handoff_retry_metrics(run)

    try:
        passed = rerun_verification_handoff_gate(
            run, retry_context=retry_context, profile=profile,
        )
    except AgentCallError:
        raise
    except (RuntimeError, ValueError) as exc:
        # Identity/ledger/dispatch configuration errors are control-plane
        # blockers. Re-expose the original subject rather than consuming it.
        _restore_recovery_subject(run, active)
        raise VerificationHandoffRetryBlocked(str(exc)) from exc
    if not passed:
        # The gate router installed a new signal/id; keep it active for the
        # normal pause persistence tail rather than clearing it as consumed.
        return _outcome(profile, paused=True)
    return _outcome(profile, paused=False)


def _dispatch_one_repair(
    run: Any,
    repair_step: Any,
    ctx: Any,
    *,
    retry_context: VerificationHandoffRetryContext,
) -> None:
    """Dispatch a human-directed repair with explicit loop identity."""
    from pipeline.runtime.runner import _dispatch_via_fsm

    active_key_sentinel = object()
    human_directed_sentinel = object()
    previous_active_key = run.state.extras.get(
        "_active_loop_round_key", active_key_sentinel,
    )
    previous_human_directed = run.state.extras.get(
        HUMAN_DIRECTED_FLAG_KEY, human_directed_sentinel,
    )
    # These are the existing FSM-facing keys.  Keep the richer retry context
    # local to this orchestration seam rather than growing state.extras flags.
    run.state.extras["repair_round"] = retry_context.fresh_round
    run.state.extras["repair_round_max"] = retry_context.loop_max_rounds
    run.state.extras["_active_loop_round_key"] = "repair_round"
    run.state.extras[HUMAN_DIRECTED_FLAG_KEY] = True
    try:
        run.state = _dispatch_via_fsm(
            repair_step, run.state, ctx,
            on_phase_start=getattr(run, "_on_phase_start", None),
            on_phase_end=getattr(run, "_on_phase_end", None),
        )
    finally:
        if previous_active_key is active_key_sentinel:
            run.state.extras.pop("_active_loop_round_key", None)
        else:
            run.state.extras["_active_loop_round_key"] = previous_active_key
        if previous_human_directed is human_directed_sentinel:
            run.state.extras.pop(HUMAN_DIRECTED_FLAG_KEY, None)
        else:
            run.state.extras[HUMAN_DIRECTED_FLAG_KEY] = previous_human_directed


def _restore_recovery_subject(run: Any, active: dict[str, Any]) -> None:
    """Durably re-expose a consumed subject after a control-plane failure."""
    from pipeline.project.handoff import _persist_decidable_after_guard_abort

    run.session["phase_handoff"] = dict(active)
    _persist_decidable_after_guard_abort(run)


def _outcome(profile: Any, *, paused: bool) -> Any:
    # Lazy to avoid circular import at module load; the existing outcome DTO is
    # still the public handoff contract.
    from pipeline.project.handoff import PhaseHandoffResumeOutcome

    return PhaseHandoffResumeOutcome(profile, frozenset(), paused)


__all__ = [
    "VerificationHandoffRetryBlocked", "apply_verification_handoff_resume",
    "VerificationHandoffRetryContext", "apply_verification_handoff_retry",
]
