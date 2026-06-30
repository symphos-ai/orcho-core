# SPDX-License-Identifier: Apache-2.0
"""ADR 0113 T1 — the role-explicit session-disposition seam + its readers.

Covers the session_keys seam (:func:`decide_session_continuation`), the
divergence guarantee (one round_key, different roles → different decisions
where the policy says so), the ``format_repair`` recovery reader, and the
adapter session-metadata reflection of the policy disposition.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pipeline.phases import review_contract_recovery
from pipeline.phases.adapters import _runtime_session_meta
from pipeline.phases.builtin.session_keys import (
    _runtime_session_meta as _session_keys_runtime_session_meta,
    auxiliary_session_continuity,
    decide_session_continuation,
)
from pipeline.phases.review_contract_recovery import retry_review_contract_once
from pipeline.plugins import PluginConfig
from pipeline.runtime import PipelineState
from pipeline.runtime.roles import SessionContinuity, SessionInvocationRole


def _state(**extras: Any) -> PipelineState:
    return PipelineState(
        task="t", project_dir="/p", plugin=PluginConfig(), extras=dict(extras),
    )


def _state_with_continuity(continuity: str, **extras: Any) -> PipelineState:
    """A state whose active step declares ``session_continuity``.

    The resolver (:func:`resolve_session_continuity`) reads the declared
    per-phase continuity off ``lifecycle_ctx.active_step.execution_policy``
    exactly as the lifecycle FSM seeds it at runtime; the unit seam stubs that
    chain so the policy is the declared one, not an accident of the call stack.
    """
    state = _state(**extras)
    state.lifecycle_ctx = SimpleNamespace(
        active_step=SimpleNamespace(
            execution_policy=SimpleNamespace(session_continuity=continuity)
        )
    )
    return state


# ── divergent decisions for one round_key, keyed on declared policy ────


class TestRoleDivergenceForSameRoundKey:
    def test_repair_round_review_vs_repair_diverge(self) -> None:
        # A CHAIN repair follow-on shares the implement write zone
        # (extras['_repair_same_write_zone'] is the CHAIN signal). For the
        # same 'repair_round' loop the REVIEW invocation (fresh_only) stays
        # fresh while the REPAIR invocation (same_zone_continue) continues —
        # the seam must not collapse them.
        review_state = _state_with_continuity(
            "fresh_only", repair_round=2, _repair_same_write_zone=True
        )
        repair_state = _state_with_continuity(
            "same_zone_continue", repair_round=2, _repair_same_write_zone=True
        )
        review = decide_session_continuation(
            review_state, role=SessionInvocationRole.REVIEW,
            phase="review_changes",
        )
        repair = decide_session_continuation(
            repair_state, role=SessionInvocationRole.REPAIR,
            phase="repair_changes",
        )
        assert review.continue_session is False
        assert repair.continue_session is True
        assert review.continue_session != repair.continue_session

    def test_plan_round_loop_continue_resumes_on_round_two(self) -> None:
        # plan and validate_plan share 'plan_round' and both declare
        # loop_continue: on round 2+ both RESUME the prior loop session
        # (the restored pre-0113 behaviour). round_key='plan_round' selects
        # the loop counter the loop_continue policy consults.
        plan_state = _state_with_continuity("loop_continue", plan_round=2)
        validate_state = _state_with_continuity("loop_continue", plan_round=2)
        plan = decide_session_continuation(
            plan_state, role=SessionInvocationRole.PLAN, phase="plan",
            round_key="plan_round",
        )
        validate = decide_session_continuation(
            validate_state, role=SessionInvocationRole.VALIDATE_PLAN,
            phase="validate_plan", round_key="plan_round",
        )
        assert plan.continue_session is True
        assert validate.continue_session is True

    def test_plan_round_loop_continue_fresh_on_round_one(self) -> None:
        # Round 1 has no prior loop session yet → loop_continue starts fresh.
        plan_state = _state_with_continuity("loop_continue", plan_round=1)
        plan = decide_session_continuation(
            plan_state, role=SessionInvocationRole.PLAN, phase="plan",
            round_key="plan_round",
        )
        assert plan.continue_session is False


# ── auxiliary roles are fresh by invocation shape, not declaration ────


class TestAuxiliaryRolesAlwaysFresh:
    def test_companion_is_fresh_without_active_step(self) -> None:
        # An auxiliary role resolves to fresh_only from its invocation shape,
        # so it never reads (or requires) an active-step declaration.
        state = _state(plan_round=2, _repair_same_write_zone=True)
        assert decide_session_continuation(
            state, role=SessionInvocationRole.COMPANION, phase="implement"
        ).continue_session is False


class TestPhaseRoleRequiresActiveStep:
    def test_plan_without_active_step_raises(self) -> None:
        # A phase role must arrive with an FSM-seeded active step carrying its
        # declared continuity. With no active step the resolver refuses to
        # guess fresh — that silent default is exactly the ADR 0113
        # plan/validate continuity regression this guard prevents. (F2)
        state = _state(plan_round=2)
        with pytest.raises(ValueError, match="no active step"):
            decide_session_continuation(
                state, role=SessionInvocationRole.PLAN, phase="plan",
                round_key="plan_round",
            )

    def test_auxiliary_without_active_step_stays_fresh(self) -> None:
        # The contrast case: an auxiliary role with no active step is *not* an
        # error — its freshness is a property of the invocation shape, not a
        # missing declaration.
        state = _state(plan_round=2)
        assert decide_session_continuation(
            state, role=SessionInvocationRole.COMPANION, phase="implement"
        ).continue_session is False


# ── seam derives policy inputs from state ─────────────────────────────


class TestSeamStateDerivation:
    def test_repair_same_write_zone_requires_chain_signal(self) -> None:
        # No CHAIN signal → repair follow-on crosses write zones → fresh.
        no_chain = _state_with_continuity("same_zone_continue", repair_round=2)
        assert decide_session_continuation(
            no_chain, role=SessionInvocationRole.REPAIR, phase="repair_changes"
        ).continue_session is False
        # CHAIN signal present → same write zone → continue.
        chain = _state_with_continuity(
            "same_zone_continue", repair_round=2, _repair_same_write_zone=True
        )
        assert decide_session_continuation(
            chain, role=SessionInvocationRole.REPAIR, phase="repair_changes"
        ).continue_session is True


# ── format_repair recovery reads the policy (fresh, not hardcoded) ─────


class _RecordingAgent:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.session_id = "sess-reviewer"

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        continue_session: bool = False,
        attachments: tuple = (),
        mutates_artifacts: bool = False,
    ) -> str:
        self.calls.append({
            "continue_session": continue_session,
            "mutates_artifacts": mutates_artifacts,
        })
        return '{"decision": "approve"}'


class TestReviewContractRecoveryReadsPolicy:
    def test_format_repair_is_fresh_not_resume(self) -> None:
        agent = _RecordingAgent()
        result = retry_review_contract_once(
            agent,
            phase="review_changes",
            cwd="/p",
            raw_output="APPROVE: looks good",
            parse_error=ValueError("not json"),
        )
        # Policy: format_repair is non-edit-shaped → fresh session.
        assert agent.calls[0]["continue_session"] is False
        # Still a read-only invocation.
        assert agent.calls[0]["mutates_artifacts"] is False
        assert result.repair_meta["triggered"] is True

    def test_format_repair_routes_through_auxiliary_classifier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # F3 — the continuity must come from the single auxiliary classifier,
        # not a hardcoded SessionContinuity.FRESH_ONLY at the call site. We
        # spy on the classifier the recovery module imported: if the hardcode
        # creeps back, the classifier is never called for FORMAT_REPAIR and
        # this assertion fails (catching the hardcode, not just the False).
        seen: list[SessionInvocationRole] = []

        def _spy(role: SessionInvocationRole) -> SessionContinuity:
            seen.append(role)
            return auxiliary_session_continuity(role)

        monkeypatch.setattr(
            review_contract_recovery, "auxiliary_session_continuity", _spy
        )
        agent = _RecordingAgent()
        retry_review_contract_once(
            agent,
            phase="review_changes",
            cwd="/p",
            raw_output="APPROVE: looks good",
            parse_error=ValueError("not json"),
        )
        assert seen == [SessionInvocationRole.FORMAT_REPAIR]
        assert agent.calls[0]["continue_session"] is False

    def test_auxiliary_classifier_rejects_phase_role(self) -> None:
        # The shared classifier is auxiliary-only; a phase role must go through
        # resolve_session_continuity so its declaration is honoured.
        with pytest.raises(ValueError, match="not an auxiliary role"):
            auxiliary_session_continuity(SessionInvocationRole.PLAN)


# ── adapter meta reflects the policy disposition, not its own probe ────


class TestAdapterMetaReflectsDisposition:
    def test_meta_uses_passed_continue_session_over_resumed_probe(self) -> None:
        # The agent's resume probe would independently read True, but the
        # runner passed continue_session=False (the policy disposition);
        # the meta must reflect the disposition, not re-derive from the
        # agent's _last_resumed_session_id.
        agent = SimpleNamespace(
            session_id="sess-1",
            _last_resumed_session_id="sess-1",
            _last_followup_parent_session_id=None,
        )
        meta = _runtime_session_meta(agent, continue_session=False)
        assert meta["continue_session"] is False
        assert meta["session_id"] == "sess-1"

    def test_meta_reflects_continue_true_even_without_resumed_probe(self) -> None:
        agent = SimpleNamespace(
            session_id="sess-2",
            _last_resumed_session_id=None,
            _last_followup_parent_session_id=None,
        )
        meta = _runtime_session_meta(agent, continue_session=True)
        assert meta["continue_session"] is True


class TestSessionKeysMetaReflectsDisposition:
    """F1: the handler-facing ``session_keys._runtime_session_meta`` seam (used
    for the per-phase ``phase_log`` meta) must also reflect the policy
    disposition, never re-derive continuity from ``_last_resumed_session_id``.
    """

    def test_meta_uses_passed_continue_session_over_resumed_probe(self) -> None:
        # Resume probe would read True (a live resumed id), but the handler
        # passed the policy disposition False → meta reflects the policy.
        agent = SimpleNamespace(
            session_id="sess-1",
            _last_resumed_session_id="sess-1",
            _last_followup_parent_session_id=None,
        )
        meta = _session_keys_runtime_session_meta(agent, continue_session=False)
        assert meta["continue_session"] is False
        assert meta["session_id"] == "sess-1"

    def test_meta_reflects_continue_true_even_without_resumed_probe(self) -> None:
        # Policy said continue (same-write-zone repair) but the runtime never
        # captured a resumed id (burned bridge) → meta still reflects True.
        agent = SimpleNamespace(
            session_id=None,
            _last_resumed_session_id=None,
            _last_followup_parent_session_id=None,
        )
        meta = _session_keys_runtime_session_meta(agent, continue_session=True)
        assert meta["continue_session"] is True
