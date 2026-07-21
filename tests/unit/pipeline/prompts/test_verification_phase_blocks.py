"""Stage 4 plan-based per-phase verification prompt blocks (T6).

``_verification_contract_part`` projects the resolved ``ScheduledGatePlan`` into
limited, per-phase prompt blocks when the contract declares gate_sets/selection.
Effective policy/action come from the plan (post work_mode transform); the gate
source is shown via ``primary_gate_set``; the whole config is never dumped.
"""

from __future__ import annotations

from types import SimpleNamespace

from pipeline.plugins import PluginConfig
from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
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
                             "cheap": True},
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
                    # under ADR 0117 the cheap flag never lifts the blocking tier,
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
    assert "envs: ci" in body
    assert "Scheduled gates:" in body
    # placeholder resolved + gate source shown via primary_gate_set.
    assert "ruff check /co" in body
    assert "<core>" in body and "<delivery>" in body


def test_implement_block_shows_debug_freedom_and_effective_action() -> None:
    state = _state(_plan_contract())
    part = _part(state, "implement")
    assert part is not None
    body = part.body
    assert "Debug freely" in body
    # effective action after the work_mode transform (governed + after_phase
    # implement => repair_loop), effective policy require for the required gate.
    assert "require; action=repair_loop] test" in body
    assert "pytest -q /co" in body


def test_review_block_prioritizes_declared_receipts() -> None:
    state = _state(_plan_contract())
    part = _part(state, "review_changes")
    assert part is not None
    body = part.body
    assert "Declared receipts are authoritative" in body
    # warn (lint) and require (test/smoke) receipts are authoritative.
    assert "lint" in body and "test" in body


def test_delivery_block_is_limited_to_before_delivery_gates() -> None:
    state = _state(_plan_contract())
    part = _part(state, "final_acceptance")
    assert part is not None
    body = part.body
    assert body.startswith("Verification contract — final_acceptance:")
    assert "require; action=handoff] smoke" in body
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
    assert "operator available] lint" in body
    assert "operator recommendation] smoke" in body
    assert "manual->" not in body and "suggest->" not in body


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
