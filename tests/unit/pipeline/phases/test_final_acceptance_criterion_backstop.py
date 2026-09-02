# SPDX-License-Identifier: Apache-2.0
"""ADR 0188 — final_acceptance backstop for open acceptance criteria.

The criterion backstop is a *separate* authority from the ADR 0090 receipt
backstop, and it is gated on strictly less:

* it fires without a declared verification contract;
* it is NOT disarmed by an operator waiver — a general "continue with waiver"
  is not the per-criterion human decision a ``human`` criterion requires;
* it also guards the no-diff shortcut, which otherwise auto-approves on
  implement evidence alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pipeline.criterion_decisions import record_human_decision
from pipeline.phases.builtin import default_registry
from pipeline.phases.builtin.review_support import (
    _criterion_backstop,
    _required_receipt_backstop,
)
from pipeline.plan_artifacts import write_parsed_plan_artifact
from pipeline.plan_parser import parse_plan
from pipeline.plugins import PluginConfig
from pipeline.runtime import PipelineState
from pipeline.verification_contract import PlaceholderContext, VerificationContract

RUN_ID = "20260613_000000"

_PLAN = {
    "short_summary": "s",
    "planning_context": "p",
    "acceptance_criteria": [
        {"id": "C1", "intent": "the docs read coherently",
         "verify": "agent_assertion"},
        {"id": "C2", "intent": "the operator accepts the journey",
         "verify": "human",
         "human_instructions": "Exercise the journey and record the outcome."},
    ],
    "tasks": [{"id": "t1", "goal": "g"}],
}


def _approved_release() -> str:
    return json.dumps({
        "verdict": "APPROVED",
        "ship_ready": True,
        "short_summary": "Ship-ready.",
        "release_blockers": [],
        "verification_gaps": [],
        "contract_status": {
            "task_contract": "satisfied",
            "interfaces": "not_applicable",
            "persistence": "not_applicable",
            "tests": "sufficient",
        },
    })


class _FakeReleaseReviewer:
    def __init__(self) -> None:
        self.model = "fake-release-reviewer"
        self.session_id: str | None = None

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        del prompt, cwd, mutates_artifacts, continue_session, attachments
        return _approved_release()


class _StubPhaseConfig:
    def __init__(self, final_acceptance_agent: Any) -> None:
        self.final_acceptance_agent = final_acceptance_agent


def _contract() -> VerificationContract:
    contract = VerificationContract.from_plugin(PluginConfig(
        work_mode="pro",
        verification={"commands": {"test": {"run": ["pytest", "-q"]}}},
    ))
    assert contract is not None
    return contract


def _state(
    tmp_path: Path,
    *,
    contract: VerificationContract | None = None,
    waiver: bool = False,
    plan: dict[str, Any] | None = _PLAN,
    decide: str | None = None,
) -> PipelineState:
    run_dir = tmp_path / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(
        json.dumps({"run_id": RUN_ID}), encoding="utf-8",
    )
    if plan is not None:
        write_parsed_plan_artifact(run_dir, parse_plan(json.dumps(plan)), attempt=1)
    if decide is not None:
        record_human_decision(
            run_dir, run_id=RUN_ID, criterion_id="C2", decision=decide,
        )
    extras: dict = {"run_id": RUN_ID}
    if contract is not None:
        extras["verification_contract"] = contract
        extras["verification_placeholders"] = PlaceholderContext(
            checkout=str(tmp_path / "wt"), project=str(tmp_path),
        )
    if waiver:
        extras["phase_handoff_waiver"] = {
            "waiver_text": "operator accepted the residual risk",
        }
    st = PipelineState(
        task="t", project_dir="/p", plugin=PluginConfig(),
        phase_config=_StubPhaseConfig(_FakeReleaseReviewer()),
        extras=extras,
    )
    st.output_dir = run_dir
    st.dry_run = False
    return st


# ── the guard itself ─────────────────────────────────────────────────────────


class TestCriterionBackstopGuard:
    def test_a_pending_human_criterion_is_a_gap_without_any_contract(
        self, tmp_path: Path,
    ) -> None:
        state = _state(tmp_path, contract=None)
        # The receipt backstop is inert without a contract; the criterion one
        # is not — that separation is the whole point.
        assert _required_receipt_backstop(state) == []
        gaps = _criterion_backstop(state)
        assert [g["risk"] for g in gaps] == [
            "acceptance criterion C2 is pending",
        ]

    def test_an_operator_waiver_does_not_disarm_the_criterion_backstop(
        self, tmp_path: Path,
    ) -> None:
        state = _state(tmp_path, contract=_contract(), waiver=True)
        assert _required_receipt_backstop(state) == []
        assert [g["risk"] for g in _criterion_backstop(state)] == [
            "acceptance criterion C2 is pending",
        ]

    def test_a_rejected_human_criterion_still_blocks(self, tmp_path: Path) -> None:
        state = _state(tmp_path, decide="reject")
        assert [g["risk"] for g in _criterion_backstop(state)] == [
            "acceptance criterion C2 is rejected",
        ]

    def test_an_accepted_human_criterion_clears_the_backstop(
        self, tmp_path: Path,
    ) -> None:
        state = _state(tmp_path, decide="accept")
        assert _criterion_backstop(state) == []

    def test_an_advisory_assertion_is_never_a_gap(self, tmp_path: Path) -> None:
        state = _state(tmp_path, decide="accept")
        assert all("C1" not in g["risk"] for g in _criterion_backstop(state))

    def test_dry_run_is_inert(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        state.dry_run = True
        assert _criterion_backstop(state) == []

    def test_a_run_without_a_plan_artifact_is_inert(self, tmp_path: Path) -> None:
        assert _criterion_backstop(_state(tmp_path, plan=None)) == []

    def test_a_corrupt_plan_artifact_blocks_instead_of_vanishing(
        self, tmp_path: Path,
    ) -> None:
        state = _state(tmp_path)
        (state.output_dir / "parsed_plan.json").write_text(
            "{ not json", encoding="utf-8",
        )
        gaps = _criterion_backstop(state)
        assert len(gaps) == 1
        assert gaps[0]["risk"] == "acceptance criterion evidence is unreadable"


# ── handler integration ──────────────────────────────────────────────────────


class TestFinalAcceptanceCriterionBackstop:
    def test_pending_human_criterion_forces_rejection_without_a_contract(
        self, tmp_path: Path,
    ) -> None:
        state = _state(tmp_path, contract=None)

        new = default_registry().get("final_acceptance")(state)

        entry = new.phase_log["final_acceptance"]
        assert entry["approved"] is False
        assert entry["verdict"] == "REJECTED"
        assert entry["ship_ready"] is False
        assert any(
            g["risk"] == "acceptance criterion C2 is pending"
            for g in entry["verification_gaps"]
        )
        assert entry["engine_backstop"]["reason"] == "acceptance_criteria_open"
        assert "Engine backstop — acceptance criteria open" in entry["output"]

    def test_an_operator_waiver_cannot_ship_a_pending_human_criterion(
        self, tmp_path: Path,
    ) -> None:
        state = _state(tmp_path, contract=_contract(), waiver=True)

        entry = default_registry().get("final_acceptance")(
            state,
        ).phase_log["final_acceptance"]

        assert entry["approved"] is False
        assert entry["engine_backstop"]["reason"] == "acceptance_criteria_open"

    def test_an_accepted_criterion_keeps_the_reviewer_approval(
        self, tmp_path: Path,
    ) -> None:
        state = _state(tmp_path, decide="accept")

        entry = default_registry().get("final_acceptance")(
            state,
        ).phase_log["final_acceptance"]

        assert entry["approved"] is True
        assert entry["verdict"] == "APPROVED"
        assert "engine_backstop" not in entry


# ── the no-diff shortcut ─────────────────────────────────────────────────────


def _no_diff_state(tmp_path: Path, **kwargs: Any) -> PipelineState:
    state = _state(tmp_path, **kwargs)
    # Reproduce the durable signal the no-diff branch keys on: review_changes
    # already recorded that there were no uncommitted changes to review.
    from pipeline.phases.builtin.handlers.final_acceptance import (
        _NO_UNCOMMITTED_CHANGES,
    )

    state.phase_log["review_changes"] = {"skipped": _NO_UNCOMMITTED_CHANGES}
    state.phase_log["implement"] = {"output": "did the work"}
    return state


class TestNoDiffShortcut:
    def test_no_diff_cannot_auto_approve_over_a_pending_human_criterion(
        self, tmp_path: Path,
    ) -> None:
        state = _no_diff_state(tmp_path)

        entry = default_registry().get("final_acceptance")(
            state,
        ).phase_log["final_acceptance"]

        assert entry["approved"] is False
        assert entry["verdict"] == "REJECTED"
        assert entry["ship_ready"] is False
        assert any(
            g["risk"] == "acceptance criterion C2 is pending"
            for g in entry["verification_gaps"]
        )
        assert "Engine backstop — acceptance criteria open" in entry["output"]
        # Implement evidence is complete, so the no-diff branch must not blame
        # it for the rejection.
        assert all(
            "complete implement evidence" not in g["risk"]
            for g in entry["verification_gaps"]
        )

    def test_no_diff_still_approves_once_the_criterion_is_accepted(
        self, tmp_path: Path,
    ) -> None:
        state = _no_diff_state(tmp_path, decide="accept")

        entry = default_registry().get("final_acceptance")(
            state,
        ).phase_log["final_acceptance"]

        assert entry["approved"] is True
        assert entry["verdict"] == "APPROVED"
        assert entry["verification_gaps"] == []

    def test_no_diff_without_a_plan_artifact_is_unchanged(
        self, tmp_path: Path,
    ) -> None:
        state = _no_diff_state(tmp_path, plan=None)

        entry = default_registry().get("final_acceptance")(
            state,
        ).phase_log["final_acceptance"]

        assert entry["approved"] is True
        assert entry["verification_gaps"] == []


@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_the_backstop_reads_the_validated_chain_head(
    tmp_path: Path, decision: str,
) -> None:
    """A replacement decision, not the first one, decides the criterion."""
    state = _state(tmp_path, decide="reject" if decision == "accept" else "accept")
    head = record_human_decision(
        state.output_dir, run_id=RUN_ID, criterion_id="C2",
        decision=decision, supersedes="hd-C2-1",
    )
    assert head.decision_id == "hd-C2-2"
    gaps = _criterion_backstop(state)
    assert (gaps == []) is (decision == "accept")
