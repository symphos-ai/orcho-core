"""Loop-runner trigger discipline for ``PhaseStep.handoff``.

Pins the contract that:

* ``human_feedback_on_reject`` fires only on the final automatic round
  when the verdict is rejected — earlier rejects keep the auto retry
  budget intact;
* ``human_feedback_always`` fires on every round with the full action
  set ``[continue, retry_feedback, halt]`` on either verdict — the
  human keeps feedback authority even when the reviewer approved;
* the default resolver returns PAUSE, which writes a structured
  ``PhaseHandoffRequested`` signal onto ``state.phase_handoff_request``
  and halts the loop without further iteration;
* a custom resolver can be plugged in and receives the signal +
  state;
* a top-level (non-loop) PhaseStep with a non-bypass handoff is
  rejected at ``run_profile`` time (fail-fast before any handler runs);
* the human-directed extra-rounds budget extends loop iteration above
  ``LoopStep.max_rounds`` without mutating the profile.
"""
from __future__ import annotations

import pytest

from pipeline.plugins import PluginConfig
from pipeline.runtime import (
    LoopStep,
    PhaseHandoffPolicy,
    PhaseHandoffType,
    PhaseRegistry,
    PhaseStep,
    PipelineProfile,
    PipelineState,
    Profile,
    run_profile,
)
from pipeline.runtime.handoff import (
    HUMAN_DIRECTED_FLAG_KEY,
    HUMAN_DIRECTED_ROUNDS_KEY,
    PhaseHandoffRequested,
    PhaseHandoffResolution,
)


def _state(**kw) -> PipelineState:
    return PipelineState(task="t", project_dir="/p", plugin=PluginConfig(), **kw)


def _build_registry(verdict_sequence: list[bool]) -> tuple[PhaseRegistry, list[str]]:
    """Build a phase registry where ``validate_plan`` walks through
    ``verdict_sequence`` one ``approved`` value per call. ``plan`` is a
    no-op pass-through. Returns ``(registry, calls)`` where ``calls``
    records every handler invocation for assertion.
    """
    calls: list[str] = []
    reg = PhaseRegistry()

    def plan(state: PipelineState) -> PipelineState:
        calls.append("plan")
        return state

    # The sequence is consumed via an index in extras so closures stay
    # picklable / hashable and the test can introspect mid-run state.
    iter_box = {"i": 0}

    def validate_plan(state: PipelineState) -> PipelineState:
        idx = iter_box["i"]
        approved = (
            verdict_sequence[idx] if idx < len(verdict_sequence) else False
        )
        iter_box["i"] = idx + 1
        state.phase_log["validate_plan"] = {
            "approved": approved,
            "verdict": "APPROVED" if approved else "REJECTED",
            "critique": f"round-{idx + 1}",
        }
        calls.append(f"validate_plan({approved})")
        return state

    reg.register("plan", plan)
    reg.register("validate_plan", validate_plan)
    return reg, calls


def _plan_loop_profile(
    handoff_type: PhaseHandoffType,
    *,
    max_rounds: int = 2,
) -> Profile:
    """Build a minimal ``plan -> validate_plan`` loop profile with the
    requested handoff policy on ``validate_plan``."""
    return Profile(
        name="test_plan_loop",
        steps=(
            LoopStep(
                steps=(
                    PhaseStep(phase="plan"),
                    PhaseStep(
                        phase="validate_plan",
                        handoff=PhaseHandoffPolicy(type=handoff_type),
                    ),
                ),
                until="validate_plan.approved",
                max_rounds=max_rounds,
                round_extras_key="plan_round",
            ),
        ),
    )


def _build_review_registry(verdict_sequence: list[bool]) -> tuple[PhaseRegistry, list[str]]:
    calls: list[str] = []
    reg = PhaseRegistry()
    iter_box = {"i": 0}

    def review_changes(state: PipelineState) -> PipelineState:
        idx = iter_box["i"]
        approved = (
            verdict_sequence[idx] if idx < len(verdict_sequence) else False
        )
        iter_box["i"] = idx + 1
        state.last_critique = "" if approved else f"review-round-{idx + 1}"
        state.phase_log["review_changes"] = {
            "approved": approved,
            "clean": approved,
            "verdict": "APPROVED" if approved else "REJECTED",
            "critique": state.last_critique,
        }
        state.phase_log["rounds_pending"] = {
            "critique": state.last_critique,
        }
        calls.append(f"review_changes({approved})")
        return state

    def repair_changes(state: PipelineState) -> PipelineState:
        state.phase_log["repair_changes"] = {"output": "repair-output"}
        pending = dict(state.phase_log.get("rounds_pending", {}) or {})
        pending["repair_output"] = "repair-output"
        state.phase_log["rounds_pending"] = pending
        calls.append("repair_changes")
        return state

    reg.register("review_changes", review_changes)
    reg.register("repair_changes", repair_changes)
    return reg, calls


def _review_loop_profile(
    handoff_type: PhaseHandoffType = PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
    *,
    max_rounds: int = 1,
) -> Profile:
    return Profile(
        name="test_review_loop",
        steps=(
            LoopStep(
                steps=(
                    PhaseStep(
                        phase="review_changes",
                        handoff=PhaseHandoffPolicy(type=handoff_type),
                    ),
                    PhaseStep(phase="repair_changes"),
                ),
                until="review_changes.clean",
                max_rounds=max_rounds,
                round_extras_key="repair_round",
            ),
        ),
    )


# ── human_feedback_on_reject 3-condition gate ──────────────────────────────


class TestOnRejectTrigger:
    def test_approved_on_first_round_no_handoff(self) -> None:
        """Approved verdict → no handoff, loop exits via until clause."""
        reg, calls = _build_registry([True])
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT, max_rounds=2,
        )
        state = _state()
        run_profile(profile, state, reg)
        assert state.phase_handoff_request is None
        assert not state.halt
        # One round only — until satisfied.
        assert calls == ["plan", "validate_plan(True)"]

    def test_rejected_with_remaining_budget_no_pause(self) -> None:
        """Round 1 rejected, round 2 approved → no pause; loop exits
        naturally on round 2 via until clause."""
        reg, calls = _build_registry([False, True])
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT, max_rounds=2,
        )
        state = _state()
        run_profile(profile, state, reg)
        assert state.phase_handoff_request is None
        assert not state.halt
        # Two rounds: rejected then approved.
        assert calls == [
            "plan", "validate_plan(False)",
            "plan", "validate_plan(True)",
        ]

    def test_rejected_on_final_round_pauses(self) -> None:
        """Round 1 rejected, round 2 rejected (final) → handoff fires."""
        reg, calls = _build_registry([False, False])
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT, max_rounds=2,
        )
        state = _state()
        run_profile(profile, state, reg)
        assert isinstance(state.phase_handoff_request, PhaseHandoffRequested)
        signal = state.phase_handoff_request
        assert signal.phase == "validate_plan"
        assert signal.type is PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT
        assert signal.trigger == "rejected"
        assert signal.approved is False
        assert signal.round == 2
        assert signal.loop_max_rounds == 2
        assert signal.handoff_id == "validate_plan:plan_round:2"
        assert signal.available_actions == (
            "continue", "retry_feedback", "halt", "continue_with_waiver",
        )
        # Loop halted; no third call.
        assert calls == [
            "plan", "validate_plan(False)",
            "plan", "validate_plan(False)",
        ]
        assert state.halt
        assert "phase handoff requested" in state.halt_reason

    def test_single_round_loop_rejected_pauses_immediately(self) -> None:
        """``max_rounds=1`` → first rejection IS the final round."""
        reg, calls = _build_registry([False])
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT, max_rounds=1,
        )
        state = _state()
        run_profile(profile, state, reg)
        assert state.phase_handoff_request is not None
        assert state.phase_handoff_request.round == 1
        assert calls == ["plan", "validate_plan(False)"]

    def test_review_handoff_defers_until_repair_finishes(self) -> None:
        """review/fix handoff fires from the rejected review verdict but
        only after the current repair step AND a final validating review
        have both completed (so the handoff payload reflects the
        repaired state, not the stale pre-repair critique)."""
        # Reviewer rejects every call — final re-review still rejects,
        # so the handoff fires from the SECOND review's verdict.
        reg, calls = _build_review_registry([False, False])
        profile = _review_loop_profile(max_rounds=1)
        state = _state()
        run_profile(profile, state, reg)
        signal = state.phase_handoff_request
        assert signal is not None
        assert signal.phase == "review_changes"
        assert signal.handoff_id == "review_changes:repair_round:1"
        # last_output is the FRESH (post-repair) review critique, not
        # the pre-repair one.
        assert signal.last_output == "review-round-2"
        assert calls == [
            "review_changes(False)",
            "repair_changes",
            "review_changes(False)",
        ]

    def test_review_handoff_clears_when_repair_fixes_findings(self) -> None:
        """When the post-repair re-review approves, the deferred handoff
        is dropped and the loop exits cleanly via its until clause."""
        # First review rejects, repair runs, second review approves.
        reg, calls = _build_review_registry([False, True])
        profile = _review_loop_profile(max_rounds=1)
        state = _state()
        run_profile(profile, state, reg)
        assert state.phase_handoff_request is None
        assert not state.halt
        assert calls == [
            "review_changes(False)",
            "repair_changes",
            "review_changes(True)",
        ]

    def test_post_repair_reverify_flag_set_on_second_review_only(
        self,
    ) -> None:
        """ADR 0039 extension: the loop sets
        ``state.extras["_review_reverify_resume"]`` around the
        post-repair re-dispatch so the review handler resumes the
        prior review session (and the banner / log layer can tag the
        pass). The flag must be False on the FIRST review (cold
        start) and True on the SECOND (validating pass), and cleared
        after the loop returns.
        """
        flag_per_call: list[bool] = []
        reg = PhaseRegistry()
        iter_box = {"i": 0}

        def review_changes(state: PipelineState) -> PipelineState:
            flag_per_call.append(
                bool(state.extras.get("_review_reverify_resume")),
            )
            idx = iter_box["i"]
            iter_box["i"] = idx + 1
            # First call rejects; second (post-repair) approves so
            # the loop exits cleanly via until.
            approved = idx >= 1
            state.phase_log["review_changes"] = {
                "approved": approved,
                "clean": approved,
                "verdict": "APPROVED" if approved else "REJECTED",
                "critique": "" if approved else "first-pass critique",
            }
            return state

        def repair_changes(state: PipelineState) -> PipelineState:
            state.phase_log["repair_changes"] = {"output": "fixed"}
            return state

        reg.register("review_changes", review_changes)
        reg.register("repair_changes", repair_changes)

        profile = _review_loop_profile(max_rounds=1)
        state = _state()
        run_profile(profile, state, reg)

        # First review must see no flag (cold start, no prior
        # session). Second must see the flag set so the review
        # handler can resume.
        assert flag_per_call == [False, True]
        # And the flag must not leak out of the loop — the finally
        # block in the runner pops it.
        assert "_review_reverify_resume" not in state.extras


# ── human_feedback_always action variants ──────────────────────────────────


class TestAlwaysTrigger:
    def test_approved_pauses_with_full_actions(self) -> None:
        reg, _ = _build_registry([True])
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_FEEDBACK_ALWAYS, max_rounds=3,
        )
        state = _state()
        run_profile(profile, state, reg)
        signal = state.phase_handoff_request
        assert signal is not None
        assert signal.trigger == "approved"
        assert signal.approved is True
        # ``human_feedback_always`` gives the human full feedback
        # authority on either verdict — APPROVED still includes
        # ``retry_feedback`` so the operator can disagree with the
        # reviewer agent. The bonus human-directed round budget
        # (``HUMAN_DIRECTED_ROUNDS_KEY``) reserves the extra round on top
        # of ``max_rounds``.
        assert signal.available_actions == (
            "continue", "retry_feedback", "halt",
        )

    def test_rejected_pauses_with_full_actions(self) -> None:
        reg, _ = _build_registry([False])
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_FEEDBACK_ALWAYS, max_rounds=3,
        )
        state = _state()
        run_profile(profile, state, reg)
        signal = state.phase_handoff_request
        assert signal is not None
        assert signal.trigger == "rejected"
        assert signal.approved is False
        assert signal.available_actions == (
            "continue", "retry_feedback", "halt", "continue_with_waiver",
        )

    def test_always_fires_on_first_round_even_with_budget_left(self) -> None:
        """Unlike on-reject, always fires on the very first verdict
        irrespective of remaining auto budget."""
        reg, calls = _build_registry([True])
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_FEEDBACK_ALWAYS, max_rounds=5,
        )
        state = _state()
        run_profile(profile, state, reg)
        assert state.phase_handoff_request is not None
        # Loop did not consume the remaining 4 rounds.
        assert calls == ["plan", "validate_plan(True)"]


# ── human_bypass / no policy ───────────────────────────────────────────────


class TestBypassAndAbsentPolicy:
    def test_bypass_never_fires(self) -> None:
        reg, _ = _build_registry([False, False])
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_BYPASS, max_rounds=2,
        )
        state = _state()
        run_profile(profile, state, reg)
        assert state.phase_handoff_request is None

    def test_absent_policy_never_fires(self) -> None:
        reg, _ = _build_registry([False, False])
        profile = Profile(
            name="no_handoff",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(phase="plan"),
                        PhaseStep(phase="validate_plan"),  # no handoff
                    ),
                    until="validate_plan.approved",
                    max_rounds=2,
                    round_extras_key="plan_round",
                ),
            ),
        )
        state = _state()
        run_profile(profile, state, reg)
        assert state.phase_handoff_request is None


# ── resolver dispatch ──────────────────────────────────────────────────────


class TestResolverDispatch:
    def test_default_resolver_returns_pause(self) -> None:
        """The default ``pause_resolver`` runs in the previous tests; this
        case pins that the resolver-less ``run_profile`` call also pauses
        — i.e. ``phase_handoff_resolver=None`` selects the default."""
        reg, _ = _build_registry([False])
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT, max_rounds=1,
        )
        state = _state()
        run_profile(profile, state, reg, phase_handoff_resolver=None)
        assert state.phase_handoff_request is not None
        assert state.halt

    def test_custom_resolver_receives_signal_and_state(self) -> None:
        captured: list[PhaseHandoffRequested] = []

        def custom(
            signal: PhaseHandoffRequested,
            _state: PipelineState,
        ) -> PhaseHandoffResolution:
            captured.append(signal)
            return PhaseHandoffResolution.PAUSE

        reg, _ = _build_registry([False])
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT, max_rounds=1,
        )
        state = _state()
        run_profile(profile, state, reg, phase_handoff_resolver=custom)
        assert len(captured) == 1
        assert captured[0].phase == "validate_plan"
        assert captured[0].round == 1


# ── non-loop fail-fast (Phase 2.6) ─────────────────────────────────────────


class TestNonLoopFailFast:
    def test_top_level_phase_step_with_non_bypass_handoff_rejected(
        self,
    ) -> None:
        """A top-level (non-loop) PhaseStep with a non-bypass handoff
        has no loop to gate against — runtime rejects it before any
        handler runs."""
        reg, _ = _build_registry([])
        profile = Profile(
            name="bad_profile",
            steps=(
                PhaseStep(
                    phase="validate_plan",
                    handoff=PhaseHandoffPolicy(
                        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                    ),
                ),
            ),
        )
        with pytest.raises(ValueError, match="not inside a LoopStep"):
            run_profile(profile, _state(), reg)

    def test_top_level_bypass_handoff_accepted(self) -> None:
        """``human_bypass`` is a no-op policy and must not be rejected
        on top-level steps."""
        reg, _ = _build_registry([])
        profile = Profile(
            name="bypass_profile",
            steps=(
                PhaseStep(
                    phase="validate_plan",
                    handoff=PhaseHandoffPolicy(
                        type=PhaseHandoffType.HUMAN_BYPASS,
                    ),
                ),
            ),
        )
        state = _state()
        # No verdict in registry path; the loose registry will not error
        # on the call — we only assert that validation passes.
        run_profile(profile, state, reg)
        assert state.phase_handoff_request is None

    def test_validate_plan_in_plan_loop_accepted(self) -> None:
        """The canonical supported shape: ``validate_plan`` with a
        non-bypass handoff inside a plan loop whose ``until`` is
        ``validate_plan.approved``. This must pass support validation."""
        reg, _ = _build_registry([True])
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT, max_rounds=1,
        )
        # Profile build alone implies a successful validation pass; run
        # to surface any latent assertion.
        run_profile(profile, _state(), reg)

    def test_unsupported_phase_in_loop_rejected(self) -> None:
        """A non-bypass handoff on a phase outside the supported set is
        rejected with the generic support-matrix message.

        ADR 0112 §5 widened the supported set to add ``final_acceptance`` as a
        bare top-level scope-expansion seam, so this case now uses
        ``compliance_check`` — a phase that is still genuinely unsupported — to
        keep exercising the generic rejection branch."""
        reg, _ = _build_registry([])
        reg.register("compliance_check", lambda s: s)
        profile = Profile(
            name="compliance_check_with_handoff",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(
                            phase="compliance_check",
                            handoff=PhaseHandoffPolicy(
                                type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                            ),
                        ),
                    ),
                    until="compliance_check.done",
                    max_rounds=1,
                    round_extras_key="cc_round",
                ),
            ),
        )
        with pytest.raises(
            ValueError,
            match="only supported on 'validate_plan', 'review_changes', "
            "'implement', and 'final_acceptance'",
        ):
            run_profile(profile, _state(), reg)

    def test_final_acceptance_handoff_in_loop_rejected(self) -> None:
        """ADR 0112 §5: final_acceptance is supported only as a bare top-level
        scope-expansion seam; declaring it inside a LoopStep is rejected with a
        dedicated message (mirroring implement-in-loop)."""
        reg, _ = _build_registry([])
        reg.register("final_acceptance", lambda s: s)
        profile = Profile(
            name="final_acceptance_in_loop",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(
                            phase="final_acceptance",
                            handoff=PhaseHandoffPolicy(
                                type=PhaseHandoffType.HUMAN_FEEDBACK_ALWAYS,
                            ),
                        ),
                    ),
                    until="final_acceptance.done",
                    max_rounds=1,
                    round_extras_key="fa_round",
                ),
            ),
        )
        with pytest.raises(
            ValueError,
            match="final_acceptance .scope-expansion. handoff is only "
            "supported as a bare top-level step",
        ):
            run_profile(profile, _state(), reg)

    def test_implement_in_loop_rejected(self) -> None:
        """ADR 0073: implement handoff is a bare top-level step; declaring
        it inside a LoopStep is rejected with a dedicated message."""
        reg, _ = _build_registry([])
        reg.register("implement", lambda s: s)
        profile = Profile(
            name="implement_in_loop",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(
                            phase="implement",
                            handoff=PhaseHandoffPolicy(
                                type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                            ),
                        ),
                    ),
                    until="implement.done",
                    max_rounds=1,
                    round_extras_key="impl_round",
                ),
            ),
        )
        with pytest.raises(
            ValueError,
            match="implement handoff is only supported as a bare top-level step",
        ):
            run_profile(profile, _state(), reg)

    def test_top_level_implement_handoff_accepted(self) -> None:
        """ADR 0073: a bare top-level implement step with a valid
        ``human_feedback_on_reject`` policy (and repair/auto_waiver config)
        passes support validation — no loop or ``until`` required."""
        reg, _ = _build_registry([])
        reg.register("implement", lambda s: s)
        profile = Profile(
            name="implement_top_level",
            steps=(
                PhaseStep(
                    phase="implement",
                    handoff=PhaseHandoffPolicy(
                        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                        repair_attempts=1,
                        on_exhausted="auto_waiver",
                    ),
                ),
            ),
        )
        state = _state()
        run_profile(profile, state, reg)
        assert state.phase_handoff_request is None

    def test_top_level_implement_handoff_stops_downstream_phases(self) -> None:
        """A handler-raised implement handoff pauses the top-level profile.

        The subtask_dag implement path can set ``state.phase_handoff_request``
        directly after attestation repair is exhausted. That pause must stop
        review/final phases even when there is no uncommitted diff; otherwise
        downstream phases add misleading "no uncommitted changes" noise before
        the operator sees the implement handoff.
        """
        calls: list[str] = []
        reg = PhaseRegistry()

        def implement(state: PipelineState) -> PipelineState:
            calls.append("implement")
            state.phase_log["implement"] = {
                "output": "build",
                "delivery_status": "incomplete",
                "delivery_clean": False,
                "incomplete_subtasks": ["T3"],
            }
            state.phase_handoff_request = PhaseHandoffRequested(
                handoff_id="implement:implement_handoff:1",
                phase="implement",
                type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                trigger="incomplete",
                verdict="INCOMPLETE",
                approved=False,
                round_extras_key="implement_handoff",
                round=1,
                loop_max_rounds=1,
                available_actions=("continue", "retry_feedback", "halt"),
                artifacts={"incomplete_subtasks": ["T3"]},
                last_output="build",
            )
            return state

        def review_changes(state: PipelineState) -> PipelineState:
            calls.append("review_changes")
            return state

        def final_acceptance(state: PipelineState) -> PipelineState:
            calls.append("final_acceptance")
            return state

        reg.register("implement", implement)
        reg.register("review_changes", review_changes)
        reg.register("final_acceptance", final_acceptance)
        profile = Profile(
            name="implement_handoff_stops",
            steps=(
                PhaseStep(
                    phase="implement",
                    handoff=PhaseHandoffPolicy(
                        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                        repair_attempts=1,
                    ),
                ),
                PhaseStep(phase="review_changes"),
                PhaseStep(phase="final_acceptance"),
            ),
        )

        state = _state()
        run_profile(profile, state, reg)

        assert calls == ["implement"]
        assert state.phase_handoff_request is not None

    def test_top_level_implement_wrong_type_rejected(self) -> None:
        """ADR 0073: implement handoff only supports
        ``human_feedback_on_reject``; ``human_feedback_always`` is rejected."""
        reg, _ = _build_registry([])
        reg.register("implement", lambda s: s)
        profile = Profile(
            name="implement_bad_type",
            steps=(
                PhaseStep(
                    phase="implement",
                    handoff=PhaseHandoffPolicy(
                        type=PhaseHandoffType.HUMAN_FEEDBACK_ALWAYS,
                    ),
                ),
            ),
        )
        with pytest.raises(
            ValueError,
            match="implement handoff only supports 'human_feedback_on_reject'",
        ):
            run_profile(profile, _state(), reg)

    def test_review_changes_in_review_repair_loop_accepted(self) -> None:
        reg, _ = _build_review_registry([True])
        run_profile(_review_loop_profile(max_rounds=1), _state(), reg)

    def test_review_changes_wrong_until_rejected(self) -> None:
        reg, _ = _build_review_registry([False])
        profile = Profile(
            name="bad_review_until",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(
                            phase="review_changes",
                            handoff=PhaseHandoffPolicy(
                                type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                            ),
                        ),
                        PhaseStep(phase="repair_changes"),
                    ),
                    until="repair_changes.done",
                    max_rounds=1,
                    round_extras_key="repair_round",
                ),
            ),
        )
        with pytest.raises(ValueError, match="requires until: 'review_changes.clean'"):
            run_profile(profile, _state(), reg)

    def test_review_changes_without_repair_successor_rejected(self) -> None:
        reg, _ = _build_review_registry([False])
        profile = Profile(
            name="bad_review_loop",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(
                            phase="review_changes",
                            handoff=PhaseHandoffPolicy(
                                type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                            ),
                        ),
                    ),
                    until="review_changes.clean",
                    max_rounds=1,
                    round_extras_key="repair_round",
                ),
            ),
        )
        with pytest.raises(
            ValueError,
            match=r"PhaseStep\(phase='repair_changes'\)",
        ):
            run_profile(profile, _state(), reg)

    def test_validate_plan_loop_with_wrong_until_rejected(self) -> None:
        """A ``validate_plan`` handoff inside a loop whose ``until`` is
        not the canonical ``validate_plan.approved`` is rejected — the
        slice narrows runtime support to the plan-loop shape so we
        don't accidentally pause loops with foreign exit predicates."""
        reg, _ = _build_registry([])
        profile = Profile(
            name="wrong_until",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(phase="plan"),
                        PhaseStep(
                            phase="validate_plan",
                            handoff=PhaseHandoffPolicy(
                                type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                            ),
                        ),
                    ),
                    until="plan.ok",  # wrong field
                    max_rounds=1,
                    round_extras_key="plan_round",
                ),
            ),
        )
        with pytest.raises(
            ValueError,
            match="requires until: 'validate_plan.approved'",
        ):
            run_profile(profile, _state(), reg)

    def test_validate_plan_handoff_without_plan_step_rejected(self) -> None:
        """The canonical supported shape is ``plan -> validate_plan``;
        a bare ``validate_plan``-only loop has nowhere for the
        retry_feedback resume to land, so it's rejected even when phase
        / until / nesting are otherwise correct."""
        reg, _ = _build_registry([])
        profile = Profile(
            name="bare_validate_plan_loop",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(
                            phase="validate_plan",
                            handoff=PhaseHandoffPolicy(
                                type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                            ),
                        ),
                    ),
                    until="validate_plan.approved",
                    max_rounds=1,
                    round_extras_key="plan_round",
                ),
            ),
        )
        with pytest.raises(
            ValueError,
            match=r"PhaseStep\(phase='plan'\)",
        ):
            run_profile(profile, _state(), reg)

    def test_validate_plan_handoff_with_plan_after_rejected(self) -> None:
        """``plan`` must come *before* ``validate_plan`` in the loop
        body. A profile that lists them in the wrong order is rejected
        — the retry_feedback resume replays the pair in declared order
        and would otherwise skip the human feedback injection point."""
        reg, _ = _build_registry([])
        profile = Profile(
            name="plan_after_validate_plan",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(
                            phase="validate_plan",
                            handoff=PhaseHandoffPolicy(
                                type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                            ),
                        ),
                        PhaseStep(phase="plan"),
                    ),
                    until="validate_plan.approved",
                    max_rounds=1,
                    round_extras_key="plan_round",
                ),
            ),
        )
        with pytest.raises(
            ValueError,
            match=r"PhaseStep\(phase='plan'\)",
        ):
            run_profile(profile, _state(), reg)

    def test_validate_plan_loop_with_negated_until_rejected(self) -> None:
        """The negated form ``not validate_plan.approved`` is not the
        plan-loop shape — also rejected."""
        reg, _ = _build_registry([])
        profile = Profile(
            name="negated_until",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(phase="plan"),
                        PhaseStep(
                            phase="validate_plan",
                            handoff=PhaseHandoffPolicy(
                                type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                            ),
                        ),
                    ),
                    until="not validate_plan.approved",
                    max_rounds=1,
                    round_extras_key="plan_round",
                ),
            ),
        )
        with pytest.raises(
            ValueError,
            match="requires until: 'validate_plan.approved'",
        ):
            run_profile(profile, _state(), reg)

    def test_pipeline_profile_legacy_path_skips_handoff_validation(
        self,
    ) -> None:
        """The legacy ``PipelineProfile`` (str-entries only) cannot
        carry handoff metadata, so the new validation does not run for
        it. This pins that the legacy dispatch path is untouched."""
        reg = PhaseRegistry()
        reg.register("plan", lambda s: s)
        profile = PipelineProfile("legacy", ("plan",))
        run_profile(profile, _state(), reg)  # must not raise


# ── human_directed_rounds budget (Phase 2.5) ───────────────────────────────


class TestHumanDirectedRoundsBudget:
    def test_default_budget_zero_unchanged_iteration_count(self) -> None:
        """No human-directed extras key set → iteration count is exactly
        ``LoopStep.max_rounds``."""
        reg, calls = _build_registry([False, False])
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_BYPASS, max_rounds=2,
        )
        run_profile(profile, _state(), reg)
        # 2 rounds × 2 phases.
        assert len(calls) == 4

    def test_extra_round_extends_loop_without_mutating_max_rounds(
        self,
    ) -> None:
        """``state.extras[HUMAN_DIRECTED_ROUNDS_KEY][key] = 1`` adds one
        extra round; the third iteration is marked ``human_directed`` via
        the per-round flag key. ``LoopStep.max_rounds`` is not changed."""
        reg, calls = _build_registry([False, False, True])
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_BYPASS, max_rounds=2,
        )
        state = _state()
        state.extras[HUMAN_DIRECTED_ROUNDS_KEY] = {"plan_round": 1}

        # Capture per-round human_directed flag observed *during* the
        # validate_plan handler — the flag is cleared in ``finally`` so we
        # need to record it mid-flight.
        observed: list[bool] = []

        original = reg.get("validate_plan")

        def validate_plan_observing(s: PipelineState) -> PipelineState:
            observed.append(bool(s.extras.get(HUMAN_DIRECTED_FLAG_KEY, False)))
            return original(s)

        reg.register("validate_plan", validate_plan_observing)

        run_profile(profile, state, reg)
        # 3 rounds executed.
        assert len(calls) == 6
        # Rounds 1 + 2 are automatic; round 3 is human-directed.
        assert observed == [False, False, True]
        # Loop runner cleared the flag in finally.
        assert HUMAN_DIRECTED_FLAG_KEY not in state.extras

    def test_extra_rounds_non_int_treated_as_zero(self) -> None:
        """Malformed extras (boolean, negative, string) fall back to 0
        rather than crashing the loop — runtime is lenient about an
        unset/garbled budget because the value is a slice-4 contract."""
        reg, _ = _build_registry([False, False])
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_BYPASS, max_rounds=2,
        )
        state = _state()
        state.extras[HUMAN_DIRECTED_ROUNDS_KEY] = {"plan_round": "garbage"}
        run_profile(profile, state, reg)
        # No extra round — still 2 × 2.
        assert state.phase_handoff_request is None


# ── signal payload shape ───────────────────────────────────────────────────


class TestSignalPayloadShape:
    def test_signal_carries_critique_as_last_output(self) -> None:
        reg, _ = _build_registry([False])
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT, max_rounds=1,
        )
        state = _state()
        run_profile(profile, state, reg)
        signal = state.phase_handoff_request
        assert signal is not None
        assert signal.last_output == "round-1"

    def test_string_false_approved_does_not_trigger_handoff(self) -> None:
        """``approved`` must be a strict bool. A custom handler that
        writes ``approved="false"`` (string) would coerce to truthy
        under ``bool(...)`` and silently mask a rejected verdict —
        :func:`build_phase_handoff_signal` AND
        :func:`pipeline.runtime.runner._evaluate_until` both fail closed
        on non-bool, so the loop neither pauses nor exits prematurely;
        it consumes the rest of the auto-budget."""
        calls: list[str] = []
        reg = PhaseRegistry()

        def plan(state: PipelineState) -> PipelineState:
            calls.append("plan")
            return state

        def validate_plan_with_string_verdict(state: PipelineState) -> PipelineState:
            state.phase_log["validate_plan"] = {
                "approved": "false",  # type-mismatched
                "verdict": "REJECTED",
                "critique": "shape-mismatched",
            }
            calls.append("validate_plan")
            return state

        reg.register("plan", plan)
        reg.register("validate_plan", validate_plan_with_string_verdict)
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT, max_rounds=2,
        )
        state = _state()
        run_profile(profile, state, reg)
        # Trigger refused to fire (non-bool approved → no opinion).
        assert state.phase_handoff_request is None
        # ``until`` also refused to exit early — the loop consumed both
        # auto rounds rather than silently treating "false" as approved.
        assert calls == [
            "plan", "validate_plan",
            "plan", "validate_plan",
        ]

    def test_int_zero_approved_does_not_exit_loop_early(self) -> None:
        """``approved=0`` is also not a strict bool — both trigger and
        ``until`` evaluation reject it as "no opinion"."""
        calls: list[str] = []
        reg = PhaseRegistry()

        def plan(state: PipelineState) -> PipelineState:
            calls.append("plan")
            return state

        def validate_plan_with_int_verdict(state: PipelineState) -> PipelineState:
            state.phase_log["validate_plan"] = {
                "approved": 0,
                "verdict": "REJECTED",
            }
            calls.append("validate_plan")
            return state

        reg.register("plan", plan)
        reg.register("validate_plan", validate_plan_with_int_verdict)
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT, max_rounds=2,
        )
        state = _state()
        run_profile(profile, state, reg)
        assert state.phase_handoff_request is None
        assert calls == [
            "plan", "validate_plan",
            "plan", "validate_plan",
        ]

    def test_int_one_approved_does_not_exit_loop_as_approved(self) -> None:
        """``approved=1`` is also not a strict bool — even though it
        looks like a positive signal, ``until`` must not accept it as
        approved. The loop continues; the strict contract is the same
        whether the malformed value would have read as truthy or falsy
        under bool() coercion."""
        calls: list[str] = []
        reg = PhaseRegistry()

        def plan(state: PipelineState) -> PipelineState:
            calls.append("plan")
            return state

        def validate_plan_with_int_one(state: PipelineState) -> PipelineState:
            state.phase_log["validate_plan"] = {
                "approved": 1,  # would have been truthy under bool()
                "verdict": "REJECTED",
            }
            calls.append("validate_plan")
            return state

        reg.register("plan", plan)
        reg.register("validate_plan", validate_plan_with_int_one)
        profile = _plan_loop_profile(
            PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT, max_rounds=2,
        )
        state = _state()
        run_profile(profile, state, reg)
        # Loop did NOT exit on round 1 — strict bool refused to coerce.
        # Both rounds produced malformed non-bool ``approved``; trigger
        # treats them as "no opinion" so no handoff fires, and ``until``
        # treats them as falsy so the loop consumes both auto rounds.
        assert calls == [
            "plan", "validate_plan",
            "plan", "validate_plan",
        ]
        assert state.phase_handoff_request is None

    def test_signal_handoff_id_includes_round_key_and_round(self) -> None:
        reg, _ = _build_registry([False])
        profile = Profile(
            name="custom_key",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(phase="plan"),
                        PhaseStep(
                            phase="validate_plan",
                            handoff=PhaseHandoffPolicy(
                                type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                            ),
                        ),
                    ),
                    until="validate_plan.approved",
                    max_rounds=1,
                    round_extras_key="custom_round",
                ),
            ),
        )
        state = _state()
        run_profile(profile, state, reg)
        signal = state.phase_handoff_request
        assert signal is not None
        assert signal.handoff_id == "validate_plan:custom_round:1"
        assert signal.round_extras_key == "custom_round"
