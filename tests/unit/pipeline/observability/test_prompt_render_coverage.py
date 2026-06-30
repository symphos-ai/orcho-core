"""M12 prompt_render coverage contract.

M12 durable observability must persist only the phase surfaces that already
carry prompt-render metadata through the session-adapter boundary. This file
pins that coverage map before storage/evidence/UI work starts, so future M12
code cannot silently pretend every phase is traced.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

from pipeline.engine import hypothesis as hypothesis_engine
from pipeline.plugins import PluginConfig
from pipeline.runtime import PipelineState
from pipeline.session_adapters import (
    BuildAdapter,
    FinalAcceptanceAdapter,
    HypothesisAdapter,
    PlanAdapter,
    RoundAdapter,
    ValidatePlanAdapter,
    default_session_adapter_registry,
)

PROMPT_RENDER_FIXTURE: dict[str, Any] = {
    "render_mode": "full",
    "session_split": "per_phase",
    "session_key": {
        "run_id": "run-m12-coverage",
        "runtime": "tests.RecordingAgent",
        "model_key": "mock",
        "scope": "per_phase:plan",
    },
    "selected_part_keys": ["role:systems_architect@0"],
    "omitted_part_keys": [],
    "prefix_hash": "sha256:prefix",
    "payload_hash": "sha256:payload",
    "wire_chars": 1234,
}


COVERED_PROMPT_RENDER_SURFACES = {
    "plan": "session.phases.plan[].prompt_render",
    "replan": "session.phases.plan[].prompt_render",
    "validate_plan": "session.phases.validate_plan[].prompt_render",
    "implement": "session.phases.implement.prompt_render",
    "review_changes": "session.phases.rounds[].prompt_render_review",
    "repair_changes": "session.phases.rounds[].prompt_render_repair",
}


DOCUMENTED_PROMPT_RENDER_EXCEPTIONS = {
    "hypothesis": (
        "The hypothesis proposal prompt invokes plan_agent directly in "
        "pipeline.engine.hypothesis; M12 must not invent durable trace for it."
    ),
    "validate_hypothesis": (
        "Hypothesis QA invokes qa_agent directly in pipeline.engine.hypothesis; "
        "M12 must not invent durable trace for it."
    ),
    "final_acceptance": (
        "Final acceptance intentionally keeps full-render verdict isolation "
        "and FinalAcceptanceAdapter drops prompt_render."
    ),
}


def _state() -> PipelineState:
    return PipelineState(
        task="t",
        project_dir="/p",
        plugin=PluginConfig(),
        extras={"run_id": "run-m12-coverage"},
    )


def _prompt_render(scope: str) -> dict[str, Any]:
    return {
        **PROMPT_RENDER_FIXTURE,
        "session_key": {
            **PROMPT_RENDER_FIXTURE["session_key"],
            "scope": scope,
        },
    }


def _persisted_prompt_render(session: dict[str, Any], surface: str) -> dict[str, Any]:
    if surface == "plan":
        return session["phases"]["plan"][0]["prompt_render"]
    if surface == "replan":
        return session["phases"]["plan"][1]["prompt_render"]
    if surface == "validate_plan":
        return session["phases"]["validate_plan"][0]["prompt_render"]
    if surface == "implement":
        return session["phases"]["implement"]["prompt_render"]
    if surface == "review_changes":
        return session["phases"]["rounds"][0]["prompt_render_review"]
    if surface == "repair_changes":
        return session["phases"]["rounds"][0]["prompt_render_repair"]
    raise AssertionError(f"unmapped prompt_render surface: {surface}")


def test_prompt_render_coverage_contract_names_every_m12_surface() -> None:
    assert COVERED_PROMPT_RENDER_SURFACES == {
        "plan": "session.phases.plan[].prompt_render",
        "replan": "session.phases.plan[].prompt_render",
        "validate_plan": "session.phases.validate_plan[].prompt_render",
        "implement": "session.phases.implement.prompt_render",
        "review_changes": "session.phases.rounds[].prompt_render_review",
        "repair_changes": "session.phases.rounds[].prompt_render_repair",
    }
    assert set(DOCUMENTED_PROMPT_RENDER_EXCEPTIONS) == {
        "hypothesis",
        "validate_hypothesis",
        "final_acceptance",
    }
    assert all(DOCUMENTED_PROMPT_RENDER_EXCEPTIONS.values())


def test_prompt_render_coverage_contract_matches_adapter_registry() -> None:
    registry = default_session_adapter_registry()
    assert registry.has("plan")
    assert registry.has("validate_plan")
    assert registry.has("implement")
    assert registry.has("repair_changes")
    assert registry.get("repair_changes") is registry.get("rounds")
    assert registry.has("final_acceptance")
    assert registry.has("hypothesis")


def test_hypothesis_exceptions_match_direct_invoke_surfaces() -> None:
    hypothesis_source = inspect.getsource(hypothesis_engine.run_hypothesis_loop)
    validate_source = inspect.getsource(hypothesis_engine._validate_hypothesis)

    assert "plan_agent.invoke" in hypothesis_source
    assert "_validate_hypothesis" in hypothesis_source
    assert "qa_agent.invoke" in validate_source
    assert "_session_aware_invoke" not in hypothesis_source
    assert "_session_aware_invoke" not in validate_source


def test_phase_adapters_persist_all_covered_prompt_render_surfaces() -> None:
    state = _state()
    session: dict[str, Any] = {"phases": {"rounds": []}}

    state.phase_log["plan"] = {
        "attempt": 1,
        "output": "plan body",
        "prompt_render": _prompt_render("per_phase:plan"),
    }
    PlanAdapter().write("plan", state, session, round_n=1)

    state.phase_log["plan"] = {
        "attempt": 2,
        "output": "replan body",
        "replan_critique": "tighten acceptance criteria",
        "prompt_render": _prompt_render("per_phase:plan"),
    }
    PlanAdapter().write("plan", state, session, round_n=2)
    assert session["phases"]["plan"][1]["replan_critique"]

    state.phase_log["validate_plan"] = {
        "attempt": 1,
        "approved": True,
        "raw_output": "{}",
        "prompt_render": _prompt_render("per_phase:validate_plan"),
    }
    ValidatePlanAdapter().write("validate_plan", state, session, round_n=1)

    state.phase_log["implement"] = {
        "output": "implemented",
        "prompt_render": _prompt_render("per_phase:implement"),
    }
    BuildAdapter().write("implement", state, session)

    state.phase_log["review_changes"] = {
        "prompt_render": _prompt_render("per_phase:review_changes"),
    }
    state.phase_log["repair_changes"] = {
        "prompt_render": _prompt_render("per_phase:implement"),
    }
    state.phase_log["rounds_pending"] = {
        "critique": "approved",
        "repair_output": "repair body",
    }
    RoundAdapter().write("repair_changes", state, session, round_n=1)

    expected_by_surface = {
        "plan": _prompt_render("per_phase:plan"),
        "replan": _prompt_render("per_phase:plan"),
        "validate_plan": _prompt_render("per_phase:validate_plan"),
        "implement": _prompt_render("per_phase:implement"),
        "review_changes": _prompt_render("per_phase:review_changes"),
        "repair_changes": _prompt_render("per_phase:implement"),
    }
    assert set(expected_by_surface) == set(COVERED_PROMPT_RENDER_SURFACES)
    for surface, expected in expected_by_surface.items():
        assert _persisted_prompt_render(session, surface) == expected


def test_prompt_render_payload_contract_is_json_serializable() -> None:
    stateless_payload = {
        **PROMPT_RENDER_FIXTURE,
        "session_split": "stateless",
        "session_key": None,
    }
    json.dumps(stateless_payload)
    json.dumps(_prompt_render("per_phase:implement"))


def test_documented_exceptions_do_not_persist_prompt_render() -> None:
    state = _state()
    session: dict[str, Any] = {"phases": {}}

    state.phase_log["final_acceptance"] = {
        "output": "release verdict",
        "prompt_render": _prompt_render("per_phase:final_acceptance"),
    }
    FinalAcceptanceAdapter().write("final_acceptance", state, session)
    assert "prompt_render" not in session["phases"]["final_acceptance"]

    # ``hypothesis`` and ``validate_hypothesis`` are separate prompt-producing
    # exceptions, but today's persisted runtime shape aggregates their attempts
    # under the broader ``hypothesis`` adapter/session key.
    state.phase_log["hypothesis"] = {
        "approved": True,
        "attempts": [{"round": 1, "verdict": "APPROVED"}],
        "prompt_render": _prompt_render("per_phase:hypothesis"),
    }
    HypothesisAdapter().write("hypothesis", state, session)
    assert "prompt_render" not in session["phases"]["hypothesis"]
