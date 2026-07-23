from pipeline.project.handoff_advice_intent import parse_advice_intent


def test_intent_keeps_valid_entries_in_order() -> None:
    intent = parse_advice_intent({"proposed_operations": [{"kind": "repair", "target": "a.py"}], "contract_effects": [{"invariant_id": "acceptance:1", "effect": "advance"}]})
    assert intent.proposed_operations[0].kind == "repair"
    assert intent.contract_effects[0].invariant_id == "acceptance:1"
    assert intent.diagnostics == ()


def test_intent_retains_malformed_unknown_and_duplicate_effect_entries() -> None:
    intent = parse_advice_intent({"proposed_operations": [{"kind": "explode"}, 3], "contract_effects": [{"invariant_id": "bad", "effect": "mystery"}, {"invariant_id": "acceptance:1", "effect": "advance"}, {"invariant_id": "acceptance:1", "effect": "advance"}, None]})
    assert len(intent.contract_effects) == 4
    assert [effect.invariant_id for effect in intent.contract_effects[1:3]] == ["acceptance:1", "acceptance:1"]
    assert any("unknown_kind" in item for item in intent.diagnostics)
    assert any("unknown_effect" in item for item in intent.diagnostics)
    assert any("malformed" in item for item in intent.diagnostics)
