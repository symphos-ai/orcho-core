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
    """Canonical identities and round accounting for one human gate retry.

    The active handoff, rather than the fresh retry round, remains the source
    of the automatic loop maximum.  This makes the one-shot retry structurally
    human-directed even when it produces another gate handoff.

    ``identity`` is the primary (route-classifying) gate; ``identities`` is the
    complete blocking set the handoff was raised for.  A retry must recheck all
    of them — rechecking only the primary would close the pause while another
    required command of the same gate set is still red.
    """

    identity: GateIdentity
    prior_round: int
    fresh_round: int
    loop_max_rounds: int
    human_retry_ordinal: int
    identities: tuple[GateIdentity, ...]

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
            identities=_blocking_identities(active, identity),
        )


def _blocking_identities(
    active: Mapping[str, object], identity: GateIdentity,
) -> tuple[GateIdentity, ...]:
    """Every gate identity the persisted handoff blocked on, primary first.

    Falls back to the primary identity alone for a handoff written before the
    set was durable, or for any record whose ``gate_identities`` entry is not a
    complete identity — a malformed entry must not silently widen or narrow
    what the retry rechecks.
    """
    artifacts = active.get("artifacts")
    raw = artifacts.get("gate_identities") if isinstance(artifacts, Mapping) else None
    if not isinstance(raw, list | tuple) or not raw:
        return (identity,)
    resolved: list[GateIdentity] = []
    for item in raw:
        if not isinstance(item, Mapping):
            return (identity,)
        command, hook, phase = item.get("command"), item.get("hook"), item.get("phase")
        if not (
            isinstance(command, str) and command
            and isinstance(hook, str) and hook
            and isinstance(phase, str)
        ):
            return (identity,)
        resolved.append(GateIdentity(command, hook, phase))
    if identity not in resolved:
        return (identity,)
    # Primary first: it names the re-parked handoff and its phase.
    return (identity, *[item for item in resolved if item != identity])


def _nonempty_str(value: object) -> str:
    """The value when it is a non-blank string, else ``""``."""
    return value if isinstance(value, str) and value.strip() else ""


def _persisted_gate_failure(active: Mapping[str, object]) -> tuple[str, str]:
    """Recover ``(critique, test_output)`` for the repair round from the record.

    ``last_output`` is preferred: ``_request_handoff`` stores the critique
    ``_synthesize_critique`` built over the WHOLE failing command set, so it is
    the only carrier guaranteed to name every red command. ``short_summary``
    and the per-command finding bodies are progressively lossier fallbacks for
    a record written without it.

    The test output is best-effort — only ``test_failure`` findings carry one,
    and the critique already restates their evidence, so an empty second
    element is a normal result rather than a recovery failure.
    """
    artifacts = active.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, Mapping) else {}
    findings = artifacts.get("findings")
    findings = findings if isinstance(findings, list | tuple) else ()
    bodies = [
        (finding, _nonempty_str(finding.get("body")))
        for finding in findings
        if isinstance(finding, Mapping)
    ]
    critique = (
        _nonempty_str(active.get("last_output"))
        or _nonempty_str(artifacts.get("short_summary"))
        or "\n\n".join(body for _finding, body in bodies if body)
    )
    test_output = "\n\n".join(
        body for finding, body in bodies
        if body and finding.get("failure_kind") == "test_failure"
    )
    return critique, test_output


def apply_verification_handoff_resume(
    *, run: Any, profile: Any, ctx: Any, active: dict[str, Any], handoff_id: str,
    action: str, feedback: str, note: str | None, decided_at: str,
    identity: GateIdentity,
) -> Any:
    """Resolve every verification-handoff action without entering a phase loop.

    A verification gate may be raised at a terminal phase, but it is a gate
    pause, not a plan or scope-expansion pause.  Where closing it resumes
    depends on the gate's hook, exactly as for ``retry_verification``
    (ADR 0195): an ``after_phase`` gate reported on a phase that ran, so the
    run continues after it; a ``before_phase`` / ``before_delivery`` gate
    guards a phase that has not run, so ``continue`` / ``continue_with_waiver``
    accept the gate failure and then run that phase. Reporting the guarded
    phase complete would deliver without ``final_acceptance`` ever executing.
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
    if action not in ("continue", "continue_with_waiver"):
        raise VerificationHandoffRetryBlocked(
            f"unsupported verification handoff action {action!r}",
        )
    if action == "continue_with_waiver" and not feedback.strip():
        raise VerificationHandoffRetryBlocked(
            "verification waiver requires continue_with_waiver feedback",
        )
    # Proven before the transition, as for ``retry_verification``: a guarded
    # pause whose resume point cannot be located blocks with the handoff and
    # its decision left exactly as they were.
    guarded = _guarded_continuation(profile, active)

    if action == "continue":
        transition = continue_handoff(
            run.session, handoff_id=handoff_id, note=note, decided_at=decided_at,
        )
        run.state.extras["phase_handoff_override"] = transition.override
        _persist_handoff_running_state(run)
        if guarded is not None:
            return _enter_guarded_phase(run, profile, active, guarded)
        return PhaseHandoffResumeOutcome(profile, completed, False)

    if action == "continue_with_waiver":
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
        if guarded is not None:
            return _enter_guarded_phase(run, profile, active, guarded)
        return PhaseHandoffResumeOutcome(profile, completed, False)

    raise VerificationHandoffRetryBlocked(
        f"unsupported verification handoff action {action!r}",
    )


def _guarded_continuation(profile: Any, active: Mapping[str, Any]) -> Any | None:
    """The proven resume point of a pre-phase gate pause, else ``None``.

    Shares the ``retry_verification`` owner (ADR 0195) so both ways of closing
    a gate pause agree on which phases are behind the resume point.
    """
    from pipeline.project.verification_env_retry import (
        pause_guards_its_phase,
        prove_continuation_position,
    )

    if not pause_guards_its_phase(active):
        return None
    return prove_continuation_position(profile, active)


def _enter_guarded_phase(
    run: Any, profile: Any, active: Mapping[str, Any], continuation: Any,
) -> Any:
    """Resume at the guarded phase without re-raising the decided hook."""
    from pipeline.project.gate_repair import record_accepted_gate_pause
    from pipeline.project.verification_env_retry import (
        continuation_outcome,
        primary_gate_hook,
    )

    record_accepted_gate_pause(
        run.state, phase=str(active.get("phase")), hook=str(primary_gate_hook(active)),
    )
    return continuation_outcome(run, profile, continuation)


def apply_verification_handoff_retry(
    *, run: Any, profile: Any, ctx: Any, active: dict[str, Any], handoff_id: str,
    feedback: str, note: str | None, decided_at: str, identity: GateIdentity,
) -> Any:
    """Repair once, then re-run one selected gate on a fresh subject.

    All validation precedes ``retry_feedback_handoff`` so malformed routing,
    stale decisions, absent retained work, or an unrecoverable persisted gate
    failure leave the active handoff available for operator recovery.
    Provider/process exceptions are intentionally not caught: their established
    interrupted/failed lifecycle remains authoritative.
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
    from pipeline.project.handoff import find_repair_loop

    repair_step = _repair_step(profile)
    if repair_step is None:
        raise VerificationHandoffRetryBlocked("verification retry profile has no repair_changes step")
    gate_critique, gate_test_output = _persisted_gate_failure(active)
    if not gate_critique:
        raise VerificationHandoffRetryBlocked(
            "verification retry has no recoverable gate failure to repair",
        )

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

    from pipeline.repair_protocol import RepairFeedback

    previous_feedback = getattr(run.state, "repair_feedback", None)
    run.state.repair_feedback = RepairFeedback(
        verification_failure=gate_critique, test_failures=gate_test_output,
    )
    try:
        _dispatch_one_repair(
            run,
            repair_step,
            ctx,
            retry_context=retry_context,
            repair_loop=find_repair_loop(profile),
        )
    except AgentCallError:
        # Provider/process failures retain their established lifecycle handling;
        # they are not operator control-plane blockers.
        raise
    except (RuntimeError, ValueError) as exc:
        _restore_recovery_subject(run, active)
        raise VerificationHandoffRetryBlocked(str(exc)) from exc
    finally:
        run.state.repair_feedback = previous_feedback
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
    repair_loop: Any = None,
) -> None:
    """Dispatch a human-directed repair with explicit loop identity.

    ``repair_loop`` supplies the member order and ``until`` clause of the loop
    this round belongs to. It is what lets a verification gate failing *inside*
    this round record a resumable position: the round is outside the loop's
    declared budget, so nothing but the recorded position can locate it again.
    """
    from pipeline.runtime.runner import (
        LOOP_DISPATCH_REVIEW_RETRY,
        _dispatch_via_fsm,
        mark_loop_member_executed,
        restore_active_loop,
        stamp_active_loop,
    )

    human_directed_sentinel = object()
    previous_human_directed = run.state.extras.get(
        HUMAN_DIRECTED_FLAG_KEY, human_directed_sentinel,
    )
    # These are the existing FSM-facing keys.  Keep the richer retry context
    # local to this orchestration seam rather than growing state.extras flags.
    run.state.extras["repair_round"] = retry_context.fresh_round
    run.state.extras["repair_round_max"] = retry_context.loop_max_rounds
    previous_active_loop = stamp_active_loop(
        run.state,
        loop_key="repair_round",
        phases=tuple(
            inner.phase for inner in getattr(repair_loop, "steps", ()) or ()
        ),
        budget=max(retry_context.fresh_round, retry_context.loop_max_rounds),
        until=getattr(repair_loop, "until", ""),
        mode=LOOP_DISPATCH_REVIEW_RETRY,
    )
    run.state.extras[HUMAN_DIRECTED_FLAG_KEY] = True
    # Only the repair member runs here; a review still owed by this round must
    # stay owed if a gate pauses the repair.
    mark_loop_member_executed(run.state, repair_step.phase)
    try:
        run.state = _dispatch_via_fsm(
            repair_step, run.state, ctx,
            on_phase_start=getattr(run, "_on_phase_start", None),
            on_phase_end=getattr(run, "_on_phase_end", None),
        )
    finally:
        restore_active_loop(run.state, previous_active_loop)
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
