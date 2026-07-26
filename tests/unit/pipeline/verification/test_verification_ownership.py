from __future__ import annotations

from agents.entities import SubTask
from pipeline.plan_parser import ParsedPlan
from pipeline.plugins import PluginConfig
from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
)
from pipeline.verification_ownership import (
    find_verification_ownership_conflicts,
    render_verification_ownership_rejection,
)


def _contract() -> VerificationContract:
    contract = VerificationContract.from_plugin(PluginConfig(
        work_mode="pro",
        verification={
            "commands": {
                "lint": {"run": ["python", "-m", "ruff", "check", "."]},
                "broad": {
                    "run": [
                        "python", "-m", "pytest", "-q", "-m",
                        "not e2e and not packaging",
                    ],
                },
                "path-unit": {
                    "run": ["python", "-m", "pytest", "-q", "tests/unit/path"],
                },
                "e2e": {"run": ["python", "-m", "pytest", "-q", "-m", "e2e"]},
            },
            "gate_sets": {
                "always": {"commands": ["lint", "broad"]},
                "path": {"commands": ["path-unit"]},
                "operator": {"commands": ["e2e"]},
            },
            "selection": [
                {"always": ["always"]},
                {"paths": ["pipeline/path/**"], "include": ["path"]},
                {"operator": ["operator"]},
            ],
            "schedule": [
                {
                    "after_phase": "implement",
                    "gate_sets": ["always", "path"],
                    "policy": "require",
                },
                {
                    "manual_only": True,
                    "gate_sets": ["operator"],
                    "policy": "suggest",
                },
            ],
        },
    ))
    assert contract is not None
    return contract


def _plan(*commands: str) -> ParsedPlan:
    return ParsedPlan(
        short_summary="plan",
        planning_context="context",
        goal="change path code",
        owned_files=("pipeline/path/owner.py",),
        commands_to_run=commands,
        subtasks=(
            SubTask(
                id="T1",
                goal="change path code",
                files=("pipeline/path/worker.py",),
                done_criteria=("Targeted regression passes.",),
            ),
        ),
        source="json",
    )


def test_exact_selected_engine_commands_are_conflicts() -> None:
    plan = _plan(
        "python -m ruff check .",
        'python -m pytest -q -m "not e2e and not packaging"',
        "python -m pytest -q tests/unit/path",
        "python -m pytest -q tests/unit/test_targeted.py",
    )

    conflicts = find_verification_ownership_conflicts(
        plan,
        _contract(),
        {"verification_placeholders": PlaceholderContext()},
    )

    assert [
        (conflict.location, conflict.gate_command)
        for conflict in conflicts
    ] == [
        ("commands_to_run[0]", "lint"),
        ("commands_to_run[1]", "broad"),
        ("commands_to_run[2]", "path-unit"),
    ]


def test_operator_command_and_targeted_check_are_not_engine_conflicts() -> None:
    plan = _plan(
        "python -m pytest -q -m e2e",
        "python -m pytest -q tests/unit/test_targeted.py",
    )

    assert find_verification_ownership_conflicts(plan, _contract(), {}) == ()


def test_no_contract_preserves_plan_behavior() -> None:
    assert find_verification_ownership_conflicts(
        _plan("python -m ruff check ."),
        None,
        {},
    ) == ()


def test_conflicts_render_valid_actionable_review() -> None:
    conflicts = find_verification_ownership_conflicts(
        _plan("python -m ruff check ."),
        _contract(),
        {},
    )

    rejection = render_verification_ownership_rejection(conflicts)

    assert '"verdict": "REJECTED"' in rejection
    assert "commands_to_run[0]" in rejection
    assert "done criteria" in rejection
    assert "exact normalized argv" in rejection


def _operator_policy_contract() -> VerificationContract:
    contract = VerificationContract.from_plugin(PluginConfig(
        work_mode="pro",
        verification={
            "commands": {
                "lint": {"run": ["python", "-m", "ruff", "check", "."]},
            },
            "gate_sets": {"core": {"commands": ["lint"]}},
            "selection": [{"always": ["core"]}],
            "schedule": [
                {
                    "after_phase": "implement",
                    "gate_sets": ["core"],
                    "policy": "suggest",
                },
            ],
        },
    ))
    assert contract is not None
    return contract


def _string_run_contract() -> VerificationContract:
    contract = VerificationContract.from_plugin(PluginConfig(
        work_mode="pro",
        verification={
            "commands": {"lint": {"run": "python -m ruff check ."}},
            "gate_sets": {"core": {"commands": ["lint"]}},
            "selection": [{"always": ["core"]}],
            "schedule": [
                {
                    "after_phase": "implement",
                    "gate_sets": ["core"],
                    "policy": "require",
                },
            ],
        },
    ))
    assert contract is not None
    return contract


def test_operator_executed_suggest_gate_is_not_an_engine_conflict() -> None:
    plan = _plan("python -m ruff check .")

    assert find_verification_ownership_conflicts(
        plan, _operator_policy_contract(), {},
    ) == ()


def test_string_run_declaration_matches_exact_argv() -> None:
    conflicts = find_verification_ownership_conflicts(
        _plan("python -m ruff check ."),
        _string_run_contract(),
        {},
    )

    assert [
        (conflict.location, conflict.gate_command)
        for conflict in conflicts
    ] == [("commands_to_run[0]", "lint")]


def test_unparseable_plan_command_never_conflicts_or_crashes() -> None:
    plan = _plan('echo "unbalanced')

    assert find_verification_ownership_conflicts(plan, _contract(), {}) == ()
