"""Stage 4 plan-based per-phase verification prompt blocks (T6).

``_verification_contract_part`` projects the resolved ``ScheduledGatePlan`` into
limited, per-phase prompt blocks when the contract declares gate_sets/selection.
Effective policy/action come from the plan (post work_mode transform); the gate
source is shown via ``primary_gate_set``; the whole config is never dumped.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from agents.entities import SubTask
from pipeline.plan_parser import ParsedPlan
from pipeline.plugins import PluginConfig
from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
    render_phase_gate_block,
)


def _plan_contract() -> VerificationContract:
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode="governed",
            verification_envs={"ci": {}},
            verification={
                "default_env": "ci",
                "commands": {
                    "lint": {"run": "ruff check {checkout}", "env": "ci",
                             "cost": "fast"},
                    "test": {"run": "pytest -q {checkout}"},
                    "smoke": {"run": "pytest -q {checkout}/smoke"},
                },
                "required": ["test"],
                "gate_sets": {
                    "core": {"commands": ["lint", "test"]},
                    "delivery": {"commands": ["smoke"]},
                },
                "selection": [{"always": ["core", "delivery"]}],
                "schedule": [
                    {"after_phase": "implement", "commands": ["test"]},
                    # lint is a warn-tier receipt by declaration, not by cost:
                    # under ADR 0117 cost never lifts the blocking tier,
                    # so an authoritative warn receipt must be declared explicitly.
                    {"after_phase": "implement", "policy": "warn",
                     "commands": ["lint"]},
                    {"before_delivery": True, "policy": "require",
                     "commands": ["smoke"]},
                ],
            },
        ),
    )
    assert contract is not None
    return contract


def _state(contract: VerificationContract) -> SimpleNamespace:
    return SimpleNamespace(extras={
        "verification_contract": contract,
        "verification_placeholders": PlaceholderContext(checkout="/co"),
    })


def _part(state, phase):
    from pipeline.phases.builtin.prompt_parts import _verification_contract_part

    return _verification_contract_part(state, phase)


def test_plan_block_lists_env_summary_and_scheduled_gates() -> None:
    state = _state(_plan_contract())
    part = _part(state, "plan")
    assert part is not None
    body = part.body
    assert body.startswith("Verification contract — plan:")
    assert "The engine owns engine-owned scheduled gates below" in body
    assert "manual/suggest entries remain operator-owned" in body.lower()
    assert "done criteria" in body
    assert "targeted checks" in body
    assert "envs: ci" in body
    assert "Scheduled gates:" in body
    # placeholder resolved + gate source shown via primary_gate_set.
    assert "ruff check /co" in body
    assert "<core>" in body and "<delivery>" in body
    assert "cost=fast" in body and "cost=unknown" in body


def test_validate_plan_block_requires_rejection_of_ownership_conflicts() -> None:
    state = _state(_plan_contract())
    part = _part(state, "validate_plan")

    assert part is not None
    assert part.body.startswith("Verification contract — validate_plan:")
    assert "Reject a plan" in part.body
    assert "implement command, task spec, or done criterion" in part.body
    assert "Targeted checks" in part.body
    assert "ruff check /co" in part.body
    assert "cost=fast" in part.body


def test_validate_plan_resolves_path_gates_from_parsed_plan() -> None:
    contract = VerificationContract.from_plugin(PluginConfig(
        work_mode="pro",
        verification={
            "commands": {"path": {"run": "pytest -q tests/unit/path"}},
            "gate_sets": {"path": {"commands": ["path"]}},
            "selection": [{
                "paths": ["pipeline/path/**"],
                "include": ["path"],
            }],
            "schedule": [{
                "after_phase": "implement",
                "gate_sets": ["path"],
                "policy": "require",
            }],
        },
    ))
    assert contract is not None
    state = _state(contract)
    state.parsed_plan = ParsedPlan(
        short_summary="plan",
        planning_context="context",
        subtasks=(
            SubTask(
                id="T1",
                goal="change path code",
                files=("pipeline/path/worker.py",),
            ),
        ),
        source="json",
    )

    part = _part(state, "validate_plan")

    assert part is not None
    assert "path <path>: pytest -q tests/unit/path" in part.body


def test_implement_block_shows_debug_freedom_and_effective_action() -> None:
    state = _state(_plan_contract())
    part = _part(state, "implement")
    assert part is not None
    body = part.body
    assert "Debug freely" in body
    # effective action after the work_mode transform (governed + after_phase
    # implement => repair_loop), effective policy require for the required gate.
    assert "require; action=repair_loop; cost=unknown] test" in body
    assert "pytest -q /co" in body
    assert "cost=unknown" in body
    assert "engine executes engine-owned scheduled gates" in body


def test_review_block_prioritizes_declared_receipts() -> None:
    state = _state(_plan_contract())
    part = _part(state, "review_changes")
    assert part is not None
    body = part.body
    assert "Engine-written receipts for engine-owned scheduled gates are authoritative" in body
    # warn (lint) and require (test/smoke) receipts are authoritative.
    assert "lint" in body and "test" in body
    assert "manual/suggest entries remain operator-owned" in body.lower()


def test_delivery_block_is_limited_to_before_delivery_gates() -> None:
    state = _state(_plan_contract())
    part = _part(state, "final_acceptance")
    assert part is not None
    body = part.body
    assert body.startswith("Verification contract — final_acceptance:")
    assert "engine-owned; require; action=handoff; cost=unknown] smoke" in body
    assert "pytest -q /co/smoke" in body
    # limited: the implement-only gates are NOT dumped into the delivery block.
    assert "ruff check" not in body
    assert "<core>" not in body


def test_delivery_block_describes_operator_owned_entries_without_actions() -> None:
    from pipeline.verification_contract import render_phase_gate_block

    plan = SimpleNamespace(entries=(
        SimpleNamespace(
            command="lint", hook="before_delivery", phase="", policy="manual",
            action="handoff", primary_gate_set="core",
        ),
        SimpleNamespace(
            command="smoke", hook="before_delivery", phase="", policy="suggest",
            action="handoff", primary_gate_set="delivery",
        ),
    ))

    body = render_phase_gate_block(_plan_contract(), plan, "final_acceptance", PlaceholderContext(
        checkout="/co",
    ))

    assert body is not None
    assert "operator-owned; available; cost=fast] lint" in body
    assert "operator-owned; recommendation; cost=unknown] smoke" in body
    assert "manual->" not in body and "suggest->" not in body


def test_raw_schedule_fallback_resolves_cost_or_defaults_to_unknown() -> None:
    from pipeline.verification_contract import render_phase_block

    block = render_phase_block(
        _plan_contract(), "implement", PlaceholderContext(checkout="/co"),
    )

    assert block is not None
    assert "test: pytest -q /co" in block and "cost=unknown" in block
    assert "lint: ruff check /co" in block and "cost=fast" in block


def _cost_contract(lint_cost: str) -> VerificationContract:
    contract = VerificationContract.from_plugin(PluginConfig(
        verification={
            "commands": {
                "fast": {"run": "fast", "cost": "fast"},
                "moderate": {"run": "moderate", "cost": "moderate"},
                "slow": {"run": "slow", "cost": "slow"},
                "unknown": {"run": "unknown", "cost": "unknown"},
                "lint": {"run": "lint", "cost": lint_cost},
            },
            "gate_sets": {
                "all": {
                    "commands": ["fast", "moderate", "slow", "unknown", "lint"],
                },
            },
            "selection": [{"always": ["all"]}],
            "schedule": [
                {"after_phase": "implement", "policy": "warn", "commands": [
                    "fast", "moderate", "slow", "unknown", "lint",
                ]},
                {"before_delivery": True, "policy": "warn", "commands": [
                    "fast", "moderate", "slow", "unknown", "lint",
                ]},
            ],
        },
    ))
    assert contract is not None
    return contract


def test_all_phase_projections_show_all_four_resolved_costs() -> None:
    from pipeline.verification_selection import (
        SelectionContext,
        build_scheduled_gate_plan,
    )

    contract = _cost_contract("fast")
    plan = build_scheduled_gate_plan(contract, SelectionContext())
    for phase in ("plan", "validate_plan", "implement", "review_changes", "final_acceptance"):
        block = render_phase_gate_block(
            contract, plan, phase, PlaceholderContext(),
        )
        assert block is not None
        for cost in ("fast", "moderate", "slow", "unknown"):
            assert f"cost={cost}" in block


def test_cost_only_mutation_does_not_change_rendered_gate_identity_or_ownership() -> None:
    from pipeline.verification_selection import (
        SelectionContext,
        build_scheduled_gate_plan,
    )

    def render(cost: str) -> str:
        contract = _cost_contract(cost)
        plan = build_scheduled_gate_plan(contract, SelectionContext())
        block = render_phase_gate_block(
            contract, plan, "implement", PlaceholderContext(),
        )
        assert block is not None
        return block

    fast = render("fast")
    slow = render("slow")
    assert "cost=fast" in fast and "cost=slow" in slow
    assert re.sub(r"; cost=(?:fast|moderate|slow|unknown)", "", fast) == re.sub(
        r"; cost=(?:fast|moderate|slow|unknown)", "", slow,
    )


def test_block_is_run_scoped_with_resolved_placeholders() -> None:
    from pipeline.prompts.types import PromptCacheScope, PromptStability

    state = _state(_plan_contract())
    part = _part(state, "plan")
    assert part is not None
    assert part.stability is PromptStability.RUN
    assert part.cache_scope is PromptCacheScope.SESSION
    assert "{checkout}" not in part.body


def test_no_contract_returns_none() -> None:
    state = SimpleNamespace(extras={})
    assert _part(state, "plan") is None


def test_write_phase_without_contract_gets_managed_command_boundary(
    tmp_path,
) -> None:
    state = SimpleNamespace(
        extras={"git_cwd": str(tmp_path / "checkout")},
        output_dir=tmp_path / "run-1",
        project_dir="/canonical",
    )

    part = _part(state, "repair_changes")

    assert part is not None
    assert "orcho command run" in part.body
    assert f"--run-dir {tmp_path / 'run-1'}" in part.body
    assert f"--cwd {tmp_path / 'checkout'}" in part.body
    assert "Targeted and diff-scoped checks may run normally" in part.body
    assert _part(state, "review_changes") is None


def test_empty_plan_returns_none() -> None:
    # gate_sets declared but selection selects nothing (task_kind rule with no
    # matching context kind) -> empty plan -> no block for any phase.
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode="governed",
            verification={
                "commands": {"test": {"run": "pytest"}},
                "gate_sets": {"core": {"commands": ["test"]}},
                "selection": [{"task_kind": "feature", "include": ["core"]}],
                "schedule": [{"after_phase": "implement", "gate_sets": ["core"]}],
            },
        ),
    )
    assert contract is not None
    state = _state(contract)
    assert _part(state, "plan") is None
    assert _part(state, "implement") is None
    assert _part(state, "final_acceptance") is None


def test_plan_is_memoized_in_state_extras() -> None:
    state = _state(_plan_contract())
    _part(state, "plan")
    # The prompt projection caches its own *preview* plan, distinct from the
    # executable routing plans gate_repair uses.
    assert "verification_gate_prompt_preview" in state.extras
    assert "verification_gate_routing_plans" not in state.extras


def test_validate_plan_block_omitted_when_only_manual_gates_exist() -> None:
    from pipeline.verification_contract import render_phase_gate_block

    plan = SimpleNamespace(entries=(
        SimpleNamespace(
            command="smoke", hook="manual_only", phase="", policy="suggest",
            action="handoff", primary_gate_set="delivery",
        ),
    ))

    assert render_phase_gate_block(
        _plan_contract(), plan, "validate_plan", PlaceholderContext(checkout="/co"),
    ) is None
