"""Per-round handoff outcome classification + observer callback.

Slice 1 polish: even when the trigger does **not** fire (approved
verdict on ``human_feedback_on_reject``; rejected with remaining auto
budget; malformed verdict), the runner must still surface what the
policy decided. ``describe_handoff_outcome`` returns the classification;
``run_profile(on_handoff_outcome=...)`` is the callback seam the
orchestrator uses to print one line per round.

Bypass-only policies (and steps without a policy) must produce no
outcome — observers should never see noise for unconfigured phases.
"""
from __future__ import annotations

from pipeline.plugins import PluginConfig
from pipeline.runtime import (
    LoopStep,
    PhaseHandoffPolicy,
    PhaseHandoffType,
    PhaseRegistry,
    PhaseStep,
    PipelineState,
    Profile,
    run_profile,
)
from pipeline.runtime.handoff import (
    HandoffOutcome,
    HandoffOutcomeKind,
    describe_handoff_outcome,
)

# ── describe_handoff_outcome — pure classifier ─────────────────────────────


def _state_with_verdict(*, approved: object | None = None, verdict: str | None = None) -> PipelineState:
    state = PipelineState(task="t", project_dir="/p", plugin=PluginConfig())
    entry: dict[str, object] = {}
    if approved is not None or verdict is not None:
        if approved is not None:
            entry["approved"] = approved
        if verdict is not None:
            entry["verdict"] = verdict
        state.phase_log["validate_plan"] = entry
    return state


def _loop() -> LoopStep:
    return LoopStep(
        steps=(
            PhaseStep(phase="plan"),
            PhaseStep(phase="validate_plan"),
        ),
        until="validate_plan.approved",
        max_rounds=2,
        round_extras_key="plan_round",
    )


class TestDescribeHandoffOutcomeBypassOmitted:
    """No outcome surfaces for bypass / missing policy — observers must
    not see noise for unconfigured phases."""

    def test_no_policy_returns_none(self) -> None:
        step = PhaseStep(phase="validate_plan")
        out = describe_handoff_outcome(step, _loop(), _state_with_verdict(approved=True), 1)
        assert out is None

    def test_human_bypass_returns_none(self) -> None:
        step = PhaseStep(
            phase="validate_plan",
            handoff=PhaseHandoffPolicy(type=PhaseHandoffType.HUMAN_BYPASS),
        )
        out = describe_handoff_outcome(step, _loop(), _state_with_verdict(approved=False), 1)
        assert out is None


class TestDescribeHandoffOutcomeOnReject:
    """``human_feedback_on_reject`` — three branches (bypassed / deferred / fired)."""

    def _step(self) -> PhaseStep:
        return PhaseStep(
            phase="validate_plan",
            handoff=PhaseHandoffPolicy(type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT),
        )

    def test_approved_round_classifies_bypassed(self) -> None:
        out = describe_handoff_outcome(
            self._step(), _loop(),
            _state_with_verdict(approved=True, verdict="APPROVED"), 1,
        )
        assert isinstance(out, HandoffOutcome)
        assert out.kind is HandoffOutcomeKind.BYPASSED
        assert out.approved is True
        assert out.verdict == "APPROVED"
        assert out.round == 1
        assert out.loop_max_rounds == 2
        assert "approved" in out.message

    def test_rejected_with_budget_classifies_deferred(self) -> None:
        out = describe_handoff_outcome(
            self._step(), _loop(),
            _state_with_verdict(approved=False, verdict="REJECTED"), 1,
        )
        assert isinstance(out, HandoffOutcome)
        assert out.kind is HandoffOutcomeKind.DEFERRED
        assert out.approved is False
        assert "auto-retry budget remains" in out.message

    def test_rejected_on_final_round_classifies_fired(self) -> None:
        out = describe_handoff_outcome(
            self._step(), _loop(),
            _state_with_verdict(approved=False, verdict="REJECTED"), 2,
        )
        assert isinstance(out, HandoffOutcome)
        assert out.kind is HandoffOutcomeKind.FIRED
        assert "pausing for human decision" in out.message


class TestDescribeHandoffOutcomeAlways:
    """``human_feedback_always`` — always fires, regardless of verdict."""

    def _step(self) -> PhaseStep:
        return PhaseStep(
            phase="validate_plan",
            handoff=PhaseHandoffPolicy(type=PhaseHandoffType.HUMAN_FEEDBACK_ALWAYS),
        )

    def test_approved_classifies_fired(self) -> None:
        out = describe_handoff_outcome(
            self._step(), _loop(),
            _state_with_verdict(approved=True, verdict="APPROVED"), 1,
        )
        assert isinstance(out, HandoffOutcome)
        assert out.kind is HandoffOutcomeKind.FIRED
        assert "approved" in out.message

    def test_rejected_classifies_fired(self) -> None:
        out = describe_handoff_outcome(
            self._step(), _loop(),
            _state_with_verdict(approved=False, verdict="REJECTED"), 1,
        )
        assert isinstance(out, HandoffOutcome)
        assert out.kind is HandoffOutcomeKind.FIRED
        assert "rejected" in out.message


class TestDescribeHandoffOutcomeNoVerdict:
    """Defensive — surface NO_VERDICT for malformed phase log entries.

    Mirrors the strict-bool guard inside ``build_phase_handoff_signal``:
    a missing or shape-mismatched ``approved`` field must not be
    silently coerced to True. Operators should see the policy was
    active and the phase log was empty/malformed.
    """

    def _step(self) -> PhaseStep:
        return PhaseStep(
            phase="validate_plan",
            handoff=PhaseHandoffPolicy(type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT),
        )

    def test_missing_phase_log_entry(self) -> None:
        state = PipelineState(task="t", project_dir="/p", plugin=PluginConfig())
        out = describe_handoff_outcome(self._step(), _loop(), state, 1)
        assert isinstance(out, HandoffOutcome)
        assert out.kind is HandoffOutcomeKind.NO_VERDICT
        assert out.approved is None

    def test_approved_string_not_bool(self) -> None:
        # ``bool("false")`` would silently coerce to True; we fail
        # closed and surface NO_VERDICT instead.
        out = describe_handoff_outcome(
            self._step(), _loop(),
            _state_with_verdict(approved="false"), 1,
        )
        assert isinstance(out, HandoffOutcome)
        assert out.kind is HandoffOutcomeKind.NO_VERDICT


# ── on_handoff_outcome callback — end-to-end through run_profile ───────────


def _build_registry(verdict_sequence: list[bool]) -> PhaseRegistry:
    reg = PhaseRegistry()

    def plan(state: PipelineState) -> PipelineState:
        return state

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
        return state

    reg.register("plan", plan)
    reg.register("validate_plan", validate_plan)
    return reg


def _profile_with_handoff(policy_type: PhaseHandoffType) -> Profile:
    return Profile(
        name="t",
        steps=(
            LoopStep(
                steps=(
                    PhaseStep(phase="plan"),
                    PhaseStep(
                        phase="validate_plan",
                        handoff=PhaseHandoffPolicy(type=policy_type),
                    ),
                ),
                until="validate_plan.approved",
                max_rounds=2,
                round_extras_key="plan_round",
            ),
        ),
    )


class TestOnHandoffOutcomeCallback:
    def test_callback_fires_once_per_round_per_non_bypass_step(self) -> None:
        """Two rounds → two outcomes (one deferred, one fired)."""
        outcomes: list[HandoffOutcome] = []
        reg = _build_registry([False, False])
        run_profile(
            _profile_with_handoff(PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT),
            PipelineState(task="t", project_dir="/p", plugin=PluginConfig()),
            reg,
            on_handoff_outcome=outcomes.append,
        )
        assert [o.kind for o in outcomes] == [
            HandoffOutcomeKind.DEFERRED,
            HandoffOutcomeKind.FIRED,
        ]
        assert [o.round for o in outcomes] == [1, 2]

    def test_callback_silent_for_bypass_policy(self) -> None:
        """``human_bypass`` produces no outcome — observer never invoked."""
        outcomes: list[HandoffOutcome] = []
        reg = _build_registry([False, False])
        profile = Profile(
            name="t",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(phase="plan"),
                        PhaseStep(
                            phase="validate_plan",
                            handoff=PhaseHandoffPolicy(
                                type=PhaseHandoffType.HUMAN_BYPASS,
                            ),
                        ),
                    ),
                    until="validate_plan.approved",
                    max_rounds=2,
                    round_extras_key="plan_round",
                ),
            ),
        )
        run_profile(
            profile,
            PipelineState(task="t", project_dir="/p", plugin=PluginConfig()),
            reg,
            on_handoff_outcome=outcomes.append,
        )
        assert outcomes == []

    def test_callback_exception_does_not_break_loop(self) -> None:
        """Observer raising must not corrupt the loop — fired signal still lands."""
        def bad_observer(_outcome: HandoffOutcome) -> None:
            raise RuntimeError("simulated observer fault")

        reg = _build_registry([False, False])
        state = PipelineState(task="t", project_dir="/p", plugin=PluginConfig())
        run_profile(
            _profile_with_handoff(PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT),
            state, reg,
            on_handoff_outcome=bad_observer,
        )
        assert state.phase_handoff_request is not None

    def test_review_outcome_reflects_post_repair_verdict(self) -> None:
        """When repair clears the findings, the operator must see
        BYPASSED (not the pre-repair FIRED) for that round.

        Regression for the operator-surface lie: previously the runner
        emitted FIRED for ``review_changes`` immediately after the
        first (rejected) review, then the post-repair re-review
        approved and no pause actually fired — leaving terminal /
        dashboard observers told "pause imminent" on the success path.
        """
        calls: list[str] = []
        reg = PhaseRegistry()
        iter_box = {"i": 0}

        def review_changes(state: PipelineState) -> PipelineState:
            idx = iter_box["i"]
            # First call: reject. Second (post-repair re-review): approve.
            approved = (idx >= 1)
            iter_box["i"] = idx + 1
            state.phase_log["review_changes"] = {
                "approved": approved,
                "clean": approved,
                "verdict": "APPROVED" if approved else "REJECTED",
                "critique": "" if approved else f"review-round-{idx + 1}",
            }
            calls.append(f"review_changes({approved})")
            return state

        def repair_changes(state: PipelineState) -> PipelineState:
            calls.append("repair_changes")
            return state

        reg.register("review_changes", review_changes)
        reg.register("repair_changes", repair_changes)

        profile = Profile(
            name="t",
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
                    until="review_changes.clean",
                    max_rounds=1,
                    round_extras_key="repair_round",
                ),
            ),
        )

        outcomes: list[HandoffOutcome] = []
        state = PipelineState(task="t", project_dir="/p", plugin=PluginConfig())
        run_profile(profile, state, reg, on_handoff_outcome=outcomes.append)

        # Sanity: re-review actually ran and the loop exited cleanly.
        assert calls == [
            "review_changes(False)", "repair_changes", "review_changes(True)",
        ]
        assert state.phase_handoff_request is None
        assert not state.halt

        # The load-bearing invariant: exactly one review_changes outcome
        # for this round, and it reflects the FINAL (post-repair)
        # verdict — BYPASSED — not the stale pre-repair FIRED.
        assert len(outcomes) == 1
        assert outcomes[0].phase == "review_changes"
        assert outcomes[0].kind is HandoffOutcomeKind.BYPASSED
        assert outcomes[0].approved is True
        assert outcomes[0].round == 1

    def test_callback_fires_for_each_round_under_human_feedback_always(self) -> None:
        """``human_feedback_always`` always fires; observer sees one
        FIRED per round (the trigger stops the loop after the first
        one, so we get exactly one in this single-round scenario)."""
        outcomes: list[HandoffOutcome] = []
        reg = _build_registry([True])
        run_profile(
            _profile_with_handoff(PhaseHandoffType.HUMAN_FEEDBACK_ALWAYS),
            PipelineState(task="t", project_dir="/p", plugin=PluginConfig()),
            reg,
            on_handoff_outcome=outcomes.append,
        )
        assert len(outcomes) == 1
        assert outcomes[0].kind is HandoffOutcomeKind.FIRED
        assert outcomes[0].approved is True
