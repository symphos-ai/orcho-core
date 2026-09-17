"""Fail-closed classification of completed plan recipes and delivery subjects."""
import pytest

from core.infra.paths import CONFIG_DIR
from pipeline.engine.delivery_applicability import (
    absent_delivery_subject,
    completed_plan_only,
    persisted_plan_only_outcome,
)
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


def _dogfood_meta(**overrides):
    """The persisted shape a real plan-only run writes (planning/research)."""
    meta = {
        "status": "done",
        "task": "Implement structured logging",
        "plan_source": "local",
        "commit_delivery": {
            "status": "not_applicable", "action": "none", "dirty": False,
            "run_id": "20260917_180242_0bf078",
            "decision_id": "20260917_180242_0bf078:delivery",
            "project_path": "/repo", "baseline_ref": "8dc028cb", "pr_url": None,
        },
    }
    meta.update(overrides)
    return meta


def test_dogfood_plan_only_receipt_is_the_canonical_outcome():
    assert persisted_plan_only_outcome(_dogfood_meta()) is True
    # Status is the caller's fact, not the predicate's: a non-terminal status
    # carrying the same receipt still matches here.
    assert persisted_plan_only_outcome(_dogfood_meta(status="running")) is True


@pytest.mark.parametrize("delivery", [
    None, [], {}, "not_applicable",
    {"status": "not_applicable"},
    {"action": "none"},
    {"status": "no_diff", "action": "none"},
    {"status": "disabled", "action": "none"},
    {"status": "committed", "action": "commit"},
    {"status": "pending", "action": "none"},
    {"status": "verification_blocked", "action": "none"},
    {"status": "not_applicable", "action": "commit"},
    {"status": "not_applicable", "action": "none", "error": "run diff unavailable"},
    {"status": "not_applicable", "action": "none", "commit_sha": "abc123"},
    {"status": "not_applicable", "action": "none", "release_verdict": "APPROVED"},
    {"status": "not_applicable", "action": "none", "release_verdict": "REJECTED"},
])
def test_ordinary_delivery_receipts_are_not_the_plan_only_outcome(delivery):
    meta = _dogfood_meta()
    meta["commit_delivery"] = delivery
    assert persisted_plan_only_outcome(meta) is False


@pytest.mark.parametrize("fact", [
    {"phase_handoff": {"trigger": "rejected"}},
    {"halt_reason": "phase_handoff_halt"},
])
def test_conflicting_terminal_facts_reject_the_plan_only_outcome(fact):
    assert persisted_plan_only_outcome(_dogfood_meta(**fact)) is False


@pytest.mark.parametrize("meta", [None, [], "done", 7])
def test_non_mapping_meta_is_not_an_outcome(meta):
    assert persisted_plan_only_outcome(meta) is False
