# SPDX-License-Identifier: Apache-2.0
"""C11 + renderer coverage: the authoring workflow reads coherently.

Also pins that no user-visible surface renders a value-object repr, and that a
correction/handoff snapshot preserves criterion id and intent.
"""
from __future__ import annotations

import json

from core.contracts.criteria import AcceptanceCriterion, GateRef
from core.contracts.plan_schema import PLAN_SCHEMA_DOC
from core.io.transcript import render_plan_block
from pipeline.criterion_claims import record_criterion_claim
from pipeline.criterion_matrix import CriterionClaim, build_criterion_matrix
from pipeline.plan_contract import render_plan_contract
from pipeline.plan_parser import parse_plan

_PLAN = {
    "short_summary": "s",
    "planning_context": "p",
    "goal": "g",
    "acceptance_criteria": [
        {"id": "C1", "intent": "regression tested", "verify": "executable",
         "gate_refs": [
             {"command": "unit", "hook": "after_phase", "phase": "implement"},
         ]},
        {"id": "C2", "intent": "reads coherently", "verify": "agent_assertion"},
        {"id": "C3", "intent": "operator accepts", "verify": "human",
         "human_instructions": "Exercise it."},
    ],
    "tasks": [{"id": "t1", "goal": "g", "acceptance_refs": ["C1", "C2"]}],
}


class TestPlanPromptTeachesTheChain:
    def test_the_prompt_names_all_three_verification_classes(self) -> None:
        for token in ("executable", "agent_assertion", "human"):
            assert token in PLAN_SCHEMA_DOC

    def test_the_prompt_forbids_command_only_gate_identity(self) -> None:
        assert "COMPLETE scheduled gate identity" in PLAN_SCHEMA_DOC
        assert "A command name alone" in PLAN_SCHEMA_DOC
        assert "never a list of strings" in PLAN_SCHEMA_DOC

    def test_the_prompt_explains_reference_by_id_and_coverage(self) -> None:
        assert "acceptance_refs" in PLAN_SCHEMA_DOC
        assert "referenced by at least one task" in PLAN_SCHEMA_DOC
        assert "never copy or restate the criterion text" in PLAN_SCHEMA_DOC


class TestDeterministicRendering:
    def test_plan_contract_block_renders_ids_classes_and_gates(self) -> None:
        block = render_plan_contract(parse_plan(json.dumps(_PLAN)))
        assert "- C1 [executable] regression tested — unit @ after_phase implement" in block
        assert "- C2 [agent_assertion] reads coherently" in block
        assert "- C3 [human] operator accepts" in block
        assert "AcceptanceCriterion(" not in block
        assert "GateRef(" not in block

    def test_plan_contract_block_is_deterministic(self) -> None:
        plan = parse_plan(json.dumps(_PLAN))
        assert render_plan_contract(plan) == render_plan_contract(plan)

    def test_transcript_plan_block_never_renders_a_value_object_repr(
        self, monkeypatch,
    ) -> None:
        # The default output mode is ``summary``, which collapses the block to
        # counters; the full contract lists live in ``live``/``debug``.
        from core.observability import logging as _logging
        from pipeline.phases.builtin.plan_artifact import (
            _parsed_plan_to_render_dict,
        )

        monkeypatch.setattr(_logging, "_output_mode", "live")
        rendered = render_plan_block(
            _parsed_plan_to_render_dict(parse_plan(json.dumps(_PLAN))),
        )
        assert "AcceptanceCriterion(" not in rendered
        assert "GateRef(" not in rendered
        assert "C1 [executable] regression tested" in rendered
        assert "acceptance C1, C2" in rendered

    def test_evidence_cli_full_view_renders_criteria_readably(self) -> None:
        from cli._evidence_cli_full import full_plan_lines

        lines = full_plan_lines(
            {
                "source": "json",
                "has_contract": True,
                "acceptance_criteria": _PLAN["acceptance_criteria"],
                "subtasks": [],
            },
            artifacts=[],
        )
        text = "\n".join(lines)
        assert "C3 [human] operator accepts" in text
        assert "AcceptanceCriterion(" not in text


class TestCorrectionSnapshotPreservesCriteria:
    def test_handoff_snapshot_keeps_criterion_id_and_intent(self) -> None:
        from pipeline.project.handoff_advice_contract import (
            build_advice_contract_snapshot,
        )

        class _State:
            task = "t"
            parsed_plan = parse_plan(json.dumps(_PLAN))

        class _Run:
            state = _State()

        class _Signal:
            artifacts: dict = {}
            phase = "implement"
            handoff_id = "h1"
            trigger = "gate"
            available_actions = ()
            round = 1
            loop_max_rounds = 1

        snapshot = build_advice_contract_snapshot(_Run(), _Signal())
        texts = [item.text for item in snapshot.acceptance_criteria]
        assert texts[0].startswith("C1 [executable] regression tested")
        assert "AcceptanceCriterion(" not in " ".join(texts)


class TestC11AdvisoryClaim:
    def test_a_reviewer_inspection_records_an_advisory_claim(self, tmp_path) -> None:
        record = record_criterion_claim(
            tmp_path,
            run_id="20260101_000000",
            criterion_id="C11",
            actor="reviewer",
            statement=(
                "The authoring workflow reads as task -> criteria -> "
                "verification -> subtasks -> receipts -> readiness."
            ),
        )
        matrix = build_criterion_matrix(
            (AcceptanceCriterion("C11", "workflow reads coherently",
                                 "agent_assertion"),),
            claims=(CriterionClaim("C11", record.claim_id, "claim"),),
        )
        row = matrix.rows[0]
        assert row.state == "advisory"
        assert row.blocking is False
        assert row.state != "proven"


def test_gate_ref_label_is_the_only_user_facing_gate_projection() -> None:
    assert GateRef("unit", "after_phase", "implement").label() == (
        "unit @ after_phase implement"
    )
    assert GateRef("smoke", "before_delivery", "").label() == (
        "smoke @ before_delivery"
    )
