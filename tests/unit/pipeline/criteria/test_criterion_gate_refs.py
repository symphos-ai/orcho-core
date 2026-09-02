# SPDX-License-Identifier: Apache-2.0
"""T2 — executable criteria resolve against the durable scheduled-gate ledger.

Covers C3 of ADR 0188. Resolution is fail-closed: an absent or unreadable
ledger, an undeclared identity, and an identity the run resolved as
not-selected all reject the plan before implement.
"""
from __future__ import annotations

import pytest

from core.contracts.criteria import AcceptanceCriterion, GateRef
from pipeline.criterion_gate_refs import (
    CriterionGateRefError,
    official_gate_identities,
    unresolved_gate_refs,
    validate_criterion_gate_refs,
)
from pipeline.verification_ledger import GateLedgerRow
from pipeline.verification_ledger_store import (
    FILENAME,
    ScheduledGateLedger,
    write_ledger,
)


def _row(
    command: str,
    *,
    selected: bool | None,
    phase: str = "implement",
    hook: str = "after_phase",
):
    return GateLedgerRow(
        gate=command, hook=hook, phase=phase,
        timing=f"{hook} {phase}".strip(), run_mode="auto", gate_sets=("smoke",),
        condition="always", selected=selected,
    )


def _criterion(*refs: GateRef) -> AcceptanceCriterion:
    return AcceptanceCriterion("C1", "i", "executable", gate_refs=refs)


@pytest.fixture()
def run_dir(tmp_path):
    write_ledger(tmp_path, ScheduledGateLedger(rows=(
        _row("unit", selected=True),
        _row("slow", selected=False),
        _row("conditional", selected=None),
        # Non-phase-anchored identity: the ledger keys it with an empty phase.
        _row("release", selected=True, hook="before_delivery", phase=""),
    )))
    return tmp_path


class TestResolution:
    def test_a_declared_and_selected_identity_resolves(self, run_dir) -> None:
        validate_criterion_gate_refs(
            [_criterion(GateRef("unit", "after_phase", "implement"))], run_dir,
        )

    def test_an_unknown_identity_is_rejected(self, run_dir) -> None:
        with pytest.raises(CriterionGateRefError, match="does not declare"):
            validate_criterion_gate_refs(
                [_criterion(GateRef("ghost", "after_phase", "implement"))], run_dir,
            )

    def test_a_right_command_under_the_wrong_hook_is_a_different_identity(
        self, run_dir,
    ) -> None:
        with pytest.raises(CriterionGateRefError, match="does not declare"):
            validate_criterion_gate_refs(
                [_criterion(GateRef("unit", "before_delivery", "implement"))],
                run_dir,
            )

    def test_a_before_delivery_identity_resolves_end_to_end(
        self, run_dir,
    ) -> None:
        """Parser to ledger: the empty-phase identity the schema now accepts is
        exactly the identity the durable ledger declares."""
        import json

        from pipeline.plan_parser import parse_plan

        plan = parse_plan(json.dumps({
            "short_summary": "s",
            "planning_context": "p",
            "acceptance_criteria": [{
                "id": "C1", "intent": "shipped safely", "verify": "executable",
                "gate_refs": [
                    {"command": "release", "hook": "before_delivery", "phase": ""},
                ],
            }],
            "tasks": [{"id": "t1", "goal": "g", "acceptance_refs": ["C1"]}],
        }))
        ref = plan.acceptance_criteria[0].gate_refs[0]
        assert ref.identity in official_gate_identities(run_dir).selected
        validate_criterion_gate_refs(plan.acceptance_criteria, run_dir)

    def test_a_resolved_not_selected_identity_is_rejected(self, run_dir) -> None:
        with pytest.raises(CriterionGateRefError, match="not selected"):
            validate_criterion_gate_refs(
                [_criterion(GateRef("slow", "after_phase", "implement"))], run_dir,
            )

    def test_a_pending_selection_epoch_is_accepted_at_plan_time(
        self, run_dir,
    ) -> None:
        """``selected is None`` means the epoch has not resolved yet.

        It cannot be resolved during planning (path/task-kind rules need the
        implement diff), so the criterion is admitted here and stays
        fail-closed downstream: the reducer reports ``not_selected`` for an
        identity the run never selected, and that row blocks readiness.
        """
        validate_criterion_gate_refs(
            [_criterion(GateRef("conditional", "after_phase", "implement"))],
            run_dir,
        )

    def test_pending_and_rejected_are_distinct_states(self, run_dir) -> None:
        identities = official_gate_identities(run_dir)
        assert ("unit", "after_phase", "implement") in identities.selected
        assert ("slow", "after_phase", "implement") in identities.rejected
        assert ("conditional", "after_phase", "implement") in identities.pending
        assert identities.declared >= identities.selected | identities.rejected


class TestFailClosed:
    def test_a_missing_ledger_rejects_an_executable_criterion(
        self, tmp_path,
    ) -> None:
        with pytest.raises(CriterionGateRefError, match="no scheduled-gate ledger"):
            official_gate_identities(tmp_path)
        with pytest.raises(CriterionGateRefError, match="no scheduled-gate ledger"):
            validate_criterion_gate_refs(
                [_criterion(GateRef("unit", "after_phase", "implement"))], tmp_path,
            )

    def test_a_corrupt_ledger_rejects_an_executable_criterion(
        self, run_dir,
    ) -> None:
        (run_dir / FILENAME).write_text("{ not json", encoding="utf-8")
        with pytest.raises(CriterionGateRefError, match="unreadable"):
            validate_criterion_gate_refs(
                [_criterion(GateRef("unit", "after_phase", "implement"))], run_dir,
            )

    def test_a_run_without_an_output_dir_rejects_an_executable_criterion(
        self,
    ) -> None:
        with pytest.raises(CriterionGateRefError, match="no output directory"):
            validate_criterion_gate_refs(
                [_criterion(GateRef("unit", "after_phase", "implement"))], None,
            )

    def test_a_plan_with_no_executable_criterion_needs_no_ledger(
        self, tmp_path,
    ) -> None:
        validate_criterion_gate_refs(
            [
                AcceptanceCriterion("C2", "i", "agent_assertion"),
                AcceptanceCriterion("C3", "i", "human", human_instructions="do"),
            ],
            tmp_path,
        )
        validate_criterion_gate_refs([], None)


def test_non_executable_criteria_are_never_checked(run_dir) -> None:
    assert unresolved_gate_refs(
        [AcceptanceCriterion("C2", "i", "agent_assertion")],
        official_gate_identities(run_dir),
    ) == []


def test_every_unresolved_ref_is_reported_in_declaration_order(run_dir) -> None:
    problems = unresolved_gate_refs(
        [_criterion(
            GateRef("ghost", "after_phase", "implement"),
            GateRef("slow", "after_phase", "implement"),
        )],
        official_gate_identities(run_dir),
    )
    assert len(problems) == 2
    assert "does not declare" in problems[0]
    assert "not selected" in problems[1]
