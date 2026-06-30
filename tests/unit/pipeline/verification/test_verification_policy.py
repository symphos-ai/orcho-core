# SPDX-License-Identifier: Apache-2.0
"""Per-gate effective delivery policy + gap partition (T1, ADR 0097).

Covers the pure ``pipeline.verification_policy`` layer:

* :func:`effective_delivery_policy_by_command` — manual/operator-only commands
  resolve to ``manual_only``; scheduled delivery-hook gates resolve to their
  plan policy (strictest delivery position wins); a ``contract.required`` command
  with no delivery-hook gate falls back to the boundary policy.
* :func:`partition_gaps` — ``require`` gaps are blocking, ``warn`` / ``suggest``
  gaps are warnings, ``manual_only`` gaps land in their own bucket and never in
  blocking (ADR 0090 "never falsely green").
* a purity lock: the module imports no ``pipeline.project.*``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pipeline.verification_policy as verification_policy
from pipeline.plugins import PluginConfig
from pipeline.verification_contract import VerificationContract
from pipeline.verification_policy import (
    MANUAL_ONLY_POLICY,
    GapEntry,
    GapPartition,
    effective_delivery_policy_by_command,
    partition_gaps,
)
from pipeline.verification_selection import ScheduledGateEntry, ScheduledGatePlan

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _contract(**verification_extra) -> VerificationContract:
    verification = {
        "default_env": "ci",
        "commands": {
            "lint": {"run": "ruff check {checkout}", "env": "ci"},
            "test": {"run": "pytest -q {checkout}"},
            "audit": {"run": "audit {checkout}"},
        },
        "required": ["test"],
        "schedule": [{"before_delivery": True, "commands": ["test"]}],
    }
    verification.update(verification_extra)
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode="governed",
            verification_envs={"ci": {}},
            verification=verification,
        ),
    )
    assert contract is not None
    return contract


def _entry(command: str, hook: str, phase: str, policy: str) -> ScheduledGateEntry:
    return ScheduledGateEntry(
        command=command,
        hook=hook,
        phase=phase,
        policy=policy,
        action="continue_warn",
        contributing_gate_sets=(),
        primary_gate_set="",
    )


def _plan(*entries: ScheduledGateEntry) -> ScheduledGatePlan:
    return ScheduledGatePlan(
        entries=entries,
        selected_gate_sets=(),
        selected_commands=tuple(e.command for e in entries),
    )


# ─────────────────────────────────────────────────────────────────────────────
# effective_delivery_policy_by_command
# ─────────────────────────────────────────────────────────────────────────────


def test_scheduled_require_gate_resolves_to_require() -> None:
    contract = _contract()
    plan = _plan(_entry("test", "before_delivery", "", "require"))
    policies = effective_delivery_policy_by_command(
        contract, plan, manual_set=set(), boundary_policy="warn",
    )
    assert policies["test"] == "require"


def test_scheduled_warn_and_suggest_gates_resolve_to_their_policy() -> None:
    contract = _contract(required=["test", "lint"])
    plan = _plan(
        _entry("test", "before_delivery", "", "warn"),
        _entry("lint", "after_phase", "implement", "suggest"),
    )
    policies = effective_delivery_policy_by_command(
        contract, plan, manual_set=set(), boundary_policy="off",
    )
    assert policies["test"] == "warn"
    assert policies["lint"] == "suggest"


def test_manual_only_command_resolves_to_manual_only_token() -> None:
    contract = _contract(required=["test", "audit"])
    # ``audit`` is scheduled require at delivery, but the caller marked it
    # manual/operator-only: manual membership is authoritative.
    plan = _plan(
        _entry("test", "before_delivery", "", "require"),
        _entry("audit", "before_delivery", "", "require"),
    )
    policies = effective_delivery_policy_by_command(
        contract, plan, manual_set={"audit"}, boundary_policy="warn",
    )
    assert policies["audit"] == MANUAL_ONLY_POLICY
    assert policies["test"] == "require"


def test_required_command_without_delivery_gate_falls_back_to_boundary() -> None:
    contract = _contract()
    # No plan at all: ``test`` is contract.required, so it takes boundary policy.
    policies = effective_delivery_policy_by_command(
        contract, None, manual_set=set(), boundary_policy="require",
    )
    assert policies["test"] == "require"

    policies_warn = effective_delivery_policy_by_command(
        contract, None, manual_set=set(), boundary_policy="warn",
    )
    assert policies_warn["test"] == "warn"


def test_strictest_delivery_position_wins() -> None:
    contract = _contract()
    # Same command scheduled at two delivery positions with differing policy —
    # the stricter (require) must win over warn.
    plan = _plan(
        _entry("test", "after_phase", "implement", "warn"),
        _entry("test", "before_delivery", "", "require"),
    )
    policies = effective_delivery_policy_by_command(
        contract, plan, manual_set=set(), boundary_policy="off",
    )
    assert policies["test"] == "require"


def test_non_delivery_hook_gate_is_ignored_for_policy() -> None:
    contract = _contract()
    # A gate at a non-delivery hook must not set the delivery policy; the
    # required command falls back to the boundary policy.
    plan = _plan(_entry("test", "before_phase", "implement", "require"))
    policies = effective_delivery_policy_by_command(
        contract, plan, manual_set=set(), boundary_policy="warn",
    )
    assert policies["test"] == "warn"


# ─────────────────────────────────────────────────────────────────────────────
# partition_gaps
# ─────────────────────────────────────────────────────────────────────────────


def test_partition_require_gap_is_blocking() -> None:
    part = partition_gaps(
        {"test": "missing"}, {"test": "require"},
    )
    assert part.blocking == (GapEntry("test", "missing", "require"),)
    assert part.warning == ()
    assert part.manual_only == ()
    assert part.has_blocking is True
    assert part.blocking_commands == ("test",)
    assert part.commands_with_status("missing") == ("test",)


def test_partition_warn_and_suggest_gaps_are_warnings_not_blocking() -> None:
    part = partition_gaps(
        {"a": "missing", "b": "stale"}, {"a": "warn", "b": "suggest"},
    )
    assert part.blocking == ()
    assert part.has_blocking is False
    assert part.warning_commands == ("a", "b")
    assert part.warning_policies == ("warn", "suggest")


def test_partition_manual_only_gap_is_separate_bucket_never_blocking() -> None:
    part = partition_gaps(
        {"test": "missing", "audit": "missing"},
        {"test": "require", "audit": MANUAL_ONLY_POLICY},
    )
    assert part.blocking_commands == ("test",)
    assert part.manual_only_commands == ("audit",)
    # The manual_only gap is NOT counted among blocking/required gaps.
    assert "audit" not in part.blocking_commands


def test_partition_present_status_contributes_nothing() -> None:
    part = partition_gaps(
        {"test": "present"}, {"test": "require"},
    )
    assert part == GapPartition()


def test_partition_accepts_classification_like_objects() -> None:
    class _Cls:
        def __init__(self, status: str) -> None:
            self.status = status

    part = partition_gaps(
        {"test": _Cls("failed")}, {"test": "require"},
    )
    assert part.blocking == (GapEntry("test", "failed", "require"),)


def test_partition_off_policy_gap_is_dropped() -> None:
    part = partition_gaps({"test": "missing"}, {"test": "off"})
    assert part == GapPartition()


def test_partition_preserves_input_order() -> None:
    status = {"c": "missing", "a": "failed", "b": "stale"}
    policy = {"c": "require", "a": "require", "b": "require"}
    part = partition_gaps(status, policy)
    assert part.blocking_commands == ("c", "a", "b")


# ─────────────────────────────────────────────────────────────────────────────
# Purity lock
# ─────────────────────────────────────────────────────────────────────────────


def test_module_does_not_import_pipeline_project() -> None:
    """Lock: the pure policy module must never import ``pipeline.project.*``.

    Mirrors the ROUTING_PLANS_EXTRAS_KEY-style lock in the readiness tests — the
    policy layer stays free of the orchestration layer so it can be reused by
    readiness, delivery, and the DONE timeline without an import cycle.
    """
    source = Path(verification_policy.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders.extend(
                alias.name for alias in node.names
                if alias.name.startswith("pipeline.project")
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "pipeline.project" or module.startswith("pipeline.project."):
                offenders.append(module)
    assert offenders == [], f"pipeline.project import(s) found: {offenders}"
