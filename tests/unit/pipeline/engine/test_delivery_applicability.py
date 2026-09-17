"""Fail-closed classification of completed plan recipes and delivery subjects."""
import pytest

from core.infra.paths import CONFIG_DIR
from pipeline.engine.delivery_applicability import absent_delivery_subject, completed_plan_only
from pipeline.profiles.loader import load_profiles_v2


@pytest.mark.parametrize("name", ["planning", "research"])
def test_full_recipe_and_approved_facts_required(name):
    profile = load_profiles_v2(CONFIG_DIR / "pipeline_profiles_v2.json")[name]
    facts = {"status": "done", "phases": {
        "plan": [{"total_atomic_tasks": 1}],
        "validate_plan": [{"approved": True, "verdict": "APPROVED"}],
    }}
    assert completed_plan_only(profile, facts)
    assert not completed_plan_only(None, facts)
    delivery_profile = load_profiles_v2(CONFIG_DIR / "pipeline_profiles_v2.json")["feature"]
    assert not completed_plan_only(delivery_profile, facts)
    assert not completed_plan_only(profile, {**facts, "status": "awaiting_phase_handoff"})
    assert not completed_plan_only(profile, {**facts, "phase_handoff": {"id": "pending"}})


@pytest.mark.parametrize("patch,untracked,absent", [
    ("", (), True), ("(no diff)", (), True),
    ("(diff unavailable)", (), False), ("", None, False),
    ("patch", (), False), ("", ("new.py",), False),
])
def test_subject_absence_requires_successful_empty_reads(patch, untracked, absent):
    assert absent_delivery_subject(patch, untracked) is absent


@pytest.mark.parametrize("facts", [
    {}, {"status": "done"}, {"status": "done", "phases": []},
    {"status": "done", "phases": {"plan": [], "validate_plan": []}},
    {"status": "done", "phases": {
        "plan": [{"total_atomic_tasks": 1}],
        "validate_plan": [{"approved": False, "verdict": "REJECTED"}],
    }},
    {"status": "done", "phases": {
        "plan": [{"total_atomic_tasks": "unknown"}],
        "validate_plan": [{"approved": True, "verdict": "APPROVED"}],
    }},
    {"status": "done", "phases": {
        "plan": [{"total_atomic_tasks": 1}],
        "validate_plan": [{"approved": True, "verdict": "APPROVED"}],
        "implement": [{"output": "changed"}],
    }},
])
def test_unknown_or_conflicting_run_facts_do_not_prove_plan_only(facts):
    profile = load_profiles_v2(CONFIG_DIR / "pipeline_profiles_v2.json")["planning"]
    assert not completed_plan_only(profile, facts)
