# SPDX-License-Identifier: Apache-2.0
"""A plan-contract violation is a rejection the planner can fix, not a halt.

Two consecutive dogfood runs died at planning round 1 of 2 on fixable output
mistakes (a gate named by its shell command; an executable criterion no task
referenced). The plan handler used to ``state.stop`` on any parse or schema
error. It now records the violation and ``validate_plan`` renders it as a
synthesized ``REJECTED`` verdict, so it becomes critique for the next round.

Fail-closed is preserved by the readers: ``validate_plan`` stops when no
replan or operator-decision path remains, and that same check now covers
unresolvable gate refs on the final round.
"""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

from core.contracts.criteria import AcceptanceCriterion, GateRef
from pipeline.phases.builtin.handlers.plan import _phase_plan
from pipeline.phases.builtin.handlers.validate_plan import _phase_validate_plan
from pipeline.phases.builtin.plan_artifact import PLAN_CONTRACT_REJECTION_KEY
from pipeline.plan_parser import parse_plan
from pipeline.runtime.roles import PhaseHandoffType
from pipeline.verification_ledger_store import ScheduledGateLedger, write_ledger
from tests.unit.pipeline.criteria.test_criterion_gate_refs import _row
from tests.unit.pipeline.phases.test_architect_prompt_session import (
    _approved_plan_json,
    _install_agent,
    _make_state,
    _RecordingAgent,
)

_GARBAGE = "this is not a parseable plan in any contract"


def _rejection(error: str = "plan missing required keys: ['tasks']") -> dict:
    return {"error": error, "kind": "PlanSchemaError", "round": 1}


class TestPlanHandlerRecordsInsteadOfHalting:
    def test_round_one_violation_does_not_halt(self) -> None:
        agent = _RecordingAgent(responses=[_GARBAGE])
        state = _make_state(plan_round=1)
        _install_agent(state, agent)

        _phase_plan(state)

        assert state.halt is False
        assert state.parsed_plan is None
        rejection = state.extras[PLAN_CONTRACT_REJECTION_KEY]
        assert rejection["round"] == 1
        assert rejection["error"]
        assert state.phase_log["plan"]["parse_error"] == rejection["error"]

    def test_replan_violation_does_not_halt_either(self) -> None:
        agent = _RecordingAgent(responses=[_GARBAGE])
        state = _make_state(plan_round=2, last_critique="fix the tasks")
        _install_agent(state, agent)

        _phase_plan(state)

        assert state.halt is False
        assert state.parsed_plan is None
        assert state.extras[PLAN_CONTRACT_REJECTION_KEY]["round"] == 2

    def test_a_stale_prior_plan_is_cleared(self) -> None:
        """Round N-1's plan must not be reviewed in place of round N's failure."""
        agent = _RecordingAgent(responses=[_GARBAGE])
        state = _make_state(plan_round=2, last_critique="x")
        _install_agent(state, agent)
        state.parsed_plan = parse_plan(_approved_plan_json())

        _phase_plan(state)

        assert state.parsed_plan is None


class TestValidatePlanRendersTheRejection:
    def test_rounds_remaining_means_critique_not_halt(self) -> None:
        agent = _RecordingAgent()
        state = _make_state(plan_round=1)
        _install_agent(state, agent)
        state.parsed_plan = None
        state.extras["plan_round_max"] = 2
        state.extras[PLAN_CONTRACT_REJECTION_KEY] = _rejection()

        _phase_validate_plan(state)

        assert agent.calls == [], "the engine answered; no reviewer call is spent"
        assert state.halt is False
        assert "plan missing required keys" in state.last_critique
        assert PLAN_CONTRACT_REJECTION_KEY not in state.extras, "consumed once"
        entry = state.phase_log["validate_plan"]
        assert entry["verdict"] == "REJECTED"
        assert entry["approved"] is False
        assert entry["contract_conflict"] == "plan_contract"
        assert entry["plan_file"] == ""

    def test_final_round_without_an_operator_path_fails_closed(self) -> None:
        agent = _RecordingAgent()
        state = _make_state(plan_round=2)
        _install_agent(state, agent)
        state.parsed_plan = None
        state.extras["plan_round_max"] = 2
        state.extras[PLAN_CONTRACT_REJECTION_KEY] = _rejection()
        # _make_state's active step declares no handoff policy at all.

        _phase_validate_plan(state)

        assert state.halt is True
        assert "plan rejected before implement" in state.halt_reason
        assert "plan missing required keys" in state.halt_reason

    def test_final_round_with_a_pausing_handoff_leaves_the_decision_to_the_operator(
        self,
    ) -> None:
        agent = _RecordingAgent()
        state = _make_state(plan_round=2)
        _install_agent(state, agent)
        state.parsed_plan = None
        state.extras["plan_round_max"] = 2
        state.extras[PLAN_CONTRACT_REJECTION_KEY] = _rejection()
        state.lifecycle_ctx.active_step.handoff = SimpleNamespace(
            type=PhaseHandoffType("human_feedback_on_reject"),
        )

        _phase_validate_plan(state)

        assert state.halt is False, "the loop runner owns the pause"
        assert state.phase_log["validate_plan"]["verdict"] == "REJECTED"


class TestGateRefFinalRoundFailsClosed:
    """Closes the gap the previous fix left: an unresolvable gate ref on the
    final round with no operator path must stop, not fall through to implement.
    """

    def test_undeclared_gate_ref_on_final_round_stops(self, tmp_path) -> None:
        write_ledger(tmp_path, ScheduledGateLedger(rows=(
            _row("lint", selected=True),
        )))
        agent = _RecordingAgent()
        state = _make_state(plan_round=2)
        _install_agent(state, agent)
        state.output_dir = tmp_path
        state.extras["plan_round_max"] = 2
        plan = parse_plan(_approved_plan_json())
        state.parsed_plan = dataclasses.replace(plan, acceptance_criteria=(
            AcceptanceCriterion(
                "C1", "lint passes", "executable",
                gate_refs=(GateRef("python -m ruff check .", "after_phase", "implement"),),
            ),
        ))

        _phase_validate_plan(state)

        assert agent.calls == []
        assert state.halt is True
        assert "validate_plan rejected before implement" in state.halt_reason
        assert "does not declare" in state.halt_reason
        entry = state.phase_log["validate_plan"]
        assert entry["contract_conflict"] == "criterion_gate_refs"
        assert "lint @ after_phase implement" in state.last_critique
