"""ADR 0132 execution-eligibility matrix and purity lock."""

from __future__ import annotations

import ast
from itertools import product
from pathlib import Path

import pytest

from pipeline.verification_execution import (
    ExecutionEligibility,
    ExecutionEligibilityError,
    resolve_execution_eligibility,
)

_PHASES = {"before_phase": "implement", "after_phase": "implement"}
_HOOKS = ("before_phase", "after_phase", "before_delivery", "manual_only", "on_resume")
_POLICIES = ("manual", "suggest", "warn", "require")


def _expected(policy: str, hook: str, selected: bool) -> ExecutionEligibility:
    phase = _PHASES.get(hook, "")
    trigger = {
        "before_phase": "operator" if policy in ("manual", "suggest") else "before_phase",
        "after_phase": "operator" if policy in ("manual", "suggest") else "after_phase",
        "before_delivery": "pre_final",
        "manual_only": "operator",
        "on_resume": "on_resume",
    }[hook]
    if not selected:
        return ExecutionEligibility(False, None, trigger, phase, "none")
    if policy in ("manual", "suggest"):
        return ExecutionEligibility(True, "operator", trigger, phase, "none")
    return ExecutionEligibility(
        True, "engine", trigger, phase,
        "warning" if policy == "warn" else "required_action",
    )


@pytest.mark.parametrize(
    ("policy", "hook", "selected"),
    [
        (policy, hook, selected)
        for policy, hook, selected in product(_POLICIES, _HOOKS, (False, True))
        if hook != "manual_only" or policy in ("manual", "suggest")
    ],
)
def test_accepted_adr_0132_matrix(policy: str, hook: str, selected: bool) -> None:
    phase = _PHASES.get(hook, "")
    assert resolve_execution_eligibility(selected, policy, hook, phase) == _expected(
        policy, hook, selected,
    )


@pytest.mark.parametrize("policy", ("warn", "require"))
def test_manual_only_rejects_automatic_policies(policy: str) -> None:
    with pytest.raises(ExecutionEligibilityError, match="manual_only"):
        resolve_execution_eligibility(True, policy, "manual_only", "")


@pytest.mark.parametrize(
    ("policy", "hook", "phase"),
    (("off", "before_phase", "implement"), ("warn", "later", ""),
     ("warn", "before_phase", ""), ("warn", "on_resume", "implement")),
)
def test_unknown_or_malformed_identity_is_rejected(policy: str, hook: str, phase: str) -> None:
    with pytest.raises(ExecutionEligibilityError):
        resolve_execution_eligibility(True, policy, hook, phase)


def test_resolver_is_pure_and_has_no_forbidden_inputs_or_imports() -> None:
    source = Path(__file__).parents[4] / "pipeline" / "verification_execution.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "resolve_execution_eligibility")
    assert [arg.arg for arg in function.args.args] == ["selected", "policy", "hook", "phase"]
    imports = [node.module or "" for node in tree.body if isinstance(node, ast.ImportFrom)]
    assert all(not module.startswith("pipeline.project") for module in imports)
    assert "cheap" not in source.read_text(encoding="utf-8")
