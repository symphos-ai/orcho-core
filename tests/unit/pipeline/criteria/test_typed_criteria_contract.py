# SPDX-License-Identifier: Apache-2.0
"""T1 — typed criteria, schema validation, legacy ingress, round trips.

Covers C1, C2, C4, C9 of the ADR 0188 contract.
"""
from __future__ import annotations

import json

import pytest

from core.contracts.criteria import (
    AcceptanceCriterion,
    CriterionSchemaError,
    GateRef,
    coerce_acceptance_criteria,
    criteria_to_wire,
    criterion_display,
    normalize_legacy_criteria,
    validate_acceptance_criteria,
)
from core.contracts.plan_schema import PlanSchemaError, validate_plan_dict
from pipeline.plan_artifacts import parsed_plan_from_dict, parsed_plan_to_dict
from pipeline.plan_parser import parse_plan

_UNIT_REF = {"command": "unit", "hook": "after_phase", "phase": "implement"}


def _plan(**overrides):
    base = {
        "short_summary": "s",
        "planning_context": "p",
        "acceptance_criteria": [
            {
                "id": "C1",
                "intent": "regression tested",
                "verify": "executable",
                "gate_refs": [dict(_UNIT_REF)],
            },
            {"id": "C2", "intent": "readable", "verify": "agent_assertion"},
            {
                "id": "C3",
                "intent": "operator accepts",
                "verify": "human",
                "human_instructions": "Exercise the journey.",
            },
        ],
        "tasks": [
            {"id": "t1", "goal": "g1", "acceptance_refs": ["C1"]},
        ],
    }
    base.update(overrides)
    return base


class TestTypedShape:
    def test_three_classes_round_trip_through_the_wire(self) -> None:
        criteria = validate_acceptance_criteria(_plan()["acceptance_criteria"])
        assert [c.verify for c in criteria] == [
            "executable", "agent_assertion", "human",
        ]
        assert criteria_to_wire(criteria) == _plan()["acceptance_criteria"]

    def test_class_irrelevant_keys_are_absent_not_empty(self) -> None:
        wire = AcceptanceCriterion("C2", "i", "agent_assertion").to_dict()
        assert wire == {"id": "C2", "intent": "i", "verify": "agent_assertion"}
        assert "gate_refs" not in wire
        assert "human_instructions" not in wire

    @pytest.mark.parametrize(
        "payload",
        [
            {"id": "C1", "intent": "i", "verify": "executable"},
            {"id": "C1", "intent": "i", "verify": "executable", "gate_refs": []},
            {"id": "C1", "intent": "i", "verify": "executable",
             "gate_refs": [{"command": "unit"}]},
            {"id": "C1", "intent": "i", "verify": "executable",
             "gate_refs": [dict(_UNIT_REF)], "human_instructions": "x"},
            {"id": "C1", "intent": "i", "verify": "agent_assertion",
             "gate_refs": [dict(_UNIT_REF)]},
            {"id": "C1", "intent": "i", "verify": "human"},
            {"id": "C1", "intent": "i", "verify": "human", "human_instructions": " "},
            {"id": "C1", "intent": "i", "verify": "somethingelse"},
            {"id": "", "intent": "i", "verify": "agent_assertion"},
            {"id": "C1", "intent": "i", "verify": "agent_assertion", "extra": 1},
            # ADR 0188 §1: the id grammar is part of the wire contract.
            {"id": "bogus", "intent": "i", "verify": "agent_assertion"},
            {"id": "C0", "intent": "i", "verify": "agent_assertion"},
            {"id": "C01", "intent": "i", "verify": "agent_assertion"},
            {"id": "c1", "intent": "i", "verify": "agent_assertion"},
        ],
    )
    def test_invalid_criterion_payloads_are_rejected(self, payload) -> None:
        with pytest.raises(CriterionSchemaError):
            validate_acceptance_criteria([payload])

    def test_a_non_phase_anchored_hook_carries_an_empty_phase(self) -> None:
        """``before_delivery`` gates key on an empty phase — they must be
        addressable, and the triple must stay complete."""
        criterion = validate_acceptance_criteria([{
            "id": "C1", "intent": "i", "verify": "executable",
            "gate_refs": [
                {"command": "smoke", "hook": "before_delivery", "phase": ""},
            ],
        }])[0]
        ref = criterion.gate_refs[0]
        assert ref.identity == ("smoke", "before_delivery", "")
        assert ref.to_dict() == {
            "command": "smoke", "hook": "before_delivery", "phase": "",
        }
        assert ref.label() == "smoke @ before_delivery"

    @pytest.mark.parametrize("hook", ["before_delivery", "on_resume", "manual_only"])
    def test_a_phase_on_a_non_phase_anchored_hook_is_rejected(self, hook) -> None:
        with pytest.raises(CriterionSchemaError, match="must be empty"):
            validate_acceptance_criteria([{
                "id": "C1", "intent": "i", "verify": "executable",
                "gate_refs": [
                    {"command": "smoke", "hook": hook, "phase": "implement"},
                ],
            }])

    @pytest.mark.parametrize("hook", ["before_phase", "after_phase"])
    def test_a_phase_anchored_hook_still_requires_its_phase(self, hook) -> None:
        with pytest.raises(CriterionSchemaError, match="phase-anchored"):
            validate_acceptance_criteria([{
                "id": "C1", "intent": "i", "verify": "executable",
                "gate_refs": [{"command": "unit", "hook": hook, "phase": ""}],
            }])

    def test_the_phase_key_stays_required_even_when_empty(self) -> None:
        with pytest.raises(CriterionSchemaError, match="complete gate identity"):
            validate_acceptance_criteria([{
                "id": "C1", "intent": "i", "verify": "executable",
                "gate_refs": [{"command": "smoke", "hook": "before_delivery"}],
            }])

    def test_command_only_gate_identity_is_invalid(self) -> None:
        with pytest.raises(CriterionSchemaError, match="complete gate identity"):
            validate_acceptance_criteria([{
                "id": "C1", "intent": "i", "verify": "executable",
                "gate_refs": [{"command": "unit", "hook": "after_phase"}],
            }])

    def test_duplicate_ids_are_rejected(self) -> None:
        with pytest.raises(CriterionSchemaError, match="repeats criterion id"):
            validate_acceptance_criteria([
                {"id": "C1", "intent": "a", "verify": "agent_assertion"},
                {"id": "C1", "intent": "b", "verify": "agent_assertion"},
            ])

    def test_surrounding_whitespace_on_an_id_is_trimmed_not_rejected(self) -> None:
        assert validate_acceptance_criteria(
            [{"id": " C1 ", "intent": "i", "verify": "agent_assertion"}],
        )[0].id == "C1"

    def test_the_id_grammar_is_enforced_for_new_typed_criteria(self) -> None:
        with pytest.raises(CriterionSchemaError, match="must match"):
            validate_acceptance_criteria(
                [{"id": "bogus", "intent": "i", "verify": "agent_assertion"}],
            )

    def test_legacy_ingress_generates_ids_in_the_same_grammar(self) -> None:
        criteria = normalize_legacy_criteria(["a", "b", "c"])
        assert [c.id for c in criteria] == ["C1", "C2", "C3"]
        # Round-tripping the normalized form through typed validation proves
        # the legacy path is not a second accepted id shape.
        assert validate_acceptance_criteria(criteria_to_wire(criteria)) == criteria

    def test_typed_validation_rejects_bare_strings(self) -> None:
        with pytest.raises(CriterionSchemaError, match="never emit"):
            validate_acceptance_criteria(["tests pass"])


class TestLegacyIngress:
    def test_single_normalizer_assigns_positional_ids(self) -> None:
        criteria = normalize_legacy_criteria(["tests pass", "docs updated"])
        assert [(c.id, c.verify) for c in criteria] == [
            ("C1", "agent_assertion"), ("C2", "agent_assertion"),
        ]

    def test_coerce_routes_legacy_and_typed_but_not_a_mix(self) -> None:
        assert coerce_acceptance_criteria(["a"])[0].id == "C1"
        assert coerce_acceptance_criteria(
            [{"id": "C9", "intent": "i", "verify": "agent_assertion"}],
        )[0].id == "C9"
        with pytest.raises(CriterionSchemaError, match="mixes legacy"):
            coerce_acceptance_criteria(
                ["a", {"id": "C2", "intent": "i", "verify": "agent_assertion"}],
            )

    def test_legacy_plan_normalizes_in_place_to_the_typed_shape(self) -> None:
        data = _plan(acceptance_criteria=["tests pass"])
        data["tasks"] = [{"id": "t1", "goal": "g"}]
        validated = validate_plan_dict(data)
        assert validated["acceptance_criteria"] == [
            {"id": "C1", "intent": "tests pass", "verify": "agent_assertion"},
        ]


class TestPlanCoverage:
    def test_dangling_task_reference_is_rejected(self) -> None:
        data = _plan()
        data["tasks"] = [{"id": "t1", "goal": "g", "acceptance_refs": ["C1", "C9"]}]
        with pytest.raises(PlanSchemaError, match="unknown criterion"):
            validate_plan_dict(data)

    def test_unowned_executable_criterion_is_rejected(self) -> None:
        data = _plan()
        data["tasks"] = [{"id": "t1", "goal": "g"}]
        with pytest.raises(PlanSchemaError, match="unowned"):
            validate_plan_dict(data)

    def test_unowned_non_executable_criteria_are_allowed(self) -> None:
        data = _plan(acceptance_criteria=[
            {"id": "C2", "intent": "i", "verify": "agent_assertion"},
        ])
        data["tasks"] = [{"id": "t1", "goal": "g"}]
        assert validate_plan_dict(data)

    def test_duplicate_refs_on_one_task_are_rejected(self) -> None:
        data = _plan()
        data["tasks"] = [{"id": "t1", "goal": "g", "acceptance_refs": ["C1", "C1"]}]
        with pytest.raises(PlanSchemaError, match="repeats criterion id"):
            validate_plan_dict(data)


class TestParsedPlanRoundTrip:
    def test_parse_artifact_reload_preserves_ids_and_content(self) -> None:
        plan = parse_plan(json.dumps(_plan()))
        assert [c.id for c in plan.acceptance_criteria] == ["C1", "C2", "C3"]
        assert plan.subtasks[0].acceptance_refs == ("C1",)

        artifact = parsed_plan_to_dict(plan)
        assert artifact["plan"]["acceptance_criteria"] == (
            _plan()["acceptance_criteria"]
        )
        reloaded = parsed_plan_from_dict(artifact)
        assert reloaded.acceptance_criteria == plan.acceptance_criteria
        assert reloaded.subtasks[0].acceptance_refs == ("C1",)
        # Byte-for-byte where serialization permits.
        assert json.dumps(parsed_plan_to_dict(reloaded)) == json.dumps(artifact)

    def test_no_reader_derives_an_id_from_array_position(self) -> None:
        data = _plan(acceptance_criteria=[
            {"id": "C7", "intent": "i", "verify": "agent_assertion"},
        ])
        data["tasks"] = [{"id": "t1", "goal": "g"}]
        plan = parse_plan(json.dumps(data))
        assert [c.id for c in plan.acceptance_criteria] == ["C7"]


class TestDisplay:
    def test_display_never_leaks_a_value_object_repr(self) -> None:
        criterion = AcceptanceCriterion(
            "C1", "regression tested", "executable",
            gate_refs=(GateRef("unit", "after_phase", "implement"),),
        )
        text = criterion_display(criterion)
        assert text == (
            "C1 [executable] regression tested — unit @ after_phase implement"
        )
        assert "AcceptanceCriterion(" not in text
        assert "GateRef(" not in str(criterion)

    def test_display_accepts_the_durable_dict_form(self) -> None:
        assert criterion_display(
            {"id": "C2", "intent": "readable", "verify": "agent_assertion"},
        ) == "C2 [agent_assertion] readable"
