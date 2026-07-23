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
import pipeline.verification_readiness as verification_readiness
from pipeline.plugins import PluginConfig
from pipeline.verification_contract import VerificationContract, placeholder_context_for
from pipeline.verification_failure import ReceiptClassification
from pipeline.verification_policy import (
    GapEntry,
    GapPartition,
    consequence_by_command,
    effective_delivery_policy_by_command,
    partition_gaps,
)
from pipeline.verification_readiness import resolve_delivery_selection
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
    assert policies["audit"] == "manual"
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


def test_required_phase_only_gate_gets_implicit_delivery_executor() -> None:
    """A delivery-enforced phase gate retains an engine refresh identity.

    Without this identity a stale receipt would remain delivery-blocking even
    though neither pre-final materialization nor the delivery hook could refresh
    it.
    """
    contract = _contract(delivery_policy="require")
    plan = _plan(_entry("test", "before_phase", "implement", "require"))

    selection = resolve_delivery_selection(contract, plan)

    assert selection.receipt_commands == ("test",)
    assert [(item.identity.hook, item.identity.phase, item.identity.policy,
             item.executor) for item in selection.identities] == [
        ("before_delivery", "", "require", "engine"),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# partition_gaps
# ─────────────────────────────────────────────────────────────────────────────


def test_partition_require_gap_is_blocking() -> None:
    part = partition_gaps(
        {"test": "missing"}, {"test": "require"},
    )
    assert part.blocking == (GapEntry("test", "missing", "require"),)
    assert part.warning == ()
    assert part.operator == ()
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
        {"test": "require", "audit": "manual"},
    )
    assert part.blocking_commands == ("test",)
    assert part.operator_commands == ("audit",)
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


def test_partition_manual_policy_gap_is_visible_as_manual_only() -> None:
    part = partition_gaps({"test": "missing"}, {"test": "manual"})
    assert part.operator == (GapEntry("test", "missing", "manual"),)


def test_partition_preserves_input_order() -> None:
    status = {"c": "missing", "a": "failed", "b": "stale"}
    policy = {"c": "require", "a": "require", "b": "require"}
    part = partition_gaps(status, policy)
    assert part.blocking_commands == ("c", "a", "b")


def test_hygiene_failure_softens_consequence_without_rewriting_require_policy() -> None:
    policies = {"provenance": "require", "environment": "require", "test": "require"}
    statuses = {
        "provenance": ReceiptClassification("failed", "provenance_failure"),
        "environment": ReceiptClassification("failed", "env_failure"),
        "test": ReceiptClassification("failed", "test_failure"),
    }

    consequences = consequence_by_command(statuses, policies)

    assert policies == {"provenance": "require", "environment": "require", "test": "require"}
    assert consequences == {
        "provenance": "warning",
        "environment": "warning",
        "test": "required_action",
    }
    partition = partition_gaps(statuses, policies, consequences)
    assert partition.blocking_commands == ("test",)
    assert partition.warning_commands == ("provenance", "environment")


def test_delivery_selection_keeps_identities_but_dedupes_receipt_commands() -> None:
    contract = _contract(required=["test"])
    plan = _plan(
        _entry("test", "after_phase", "implement", "manual"),
        _entry("test", "before_delivery", "", "warn"),
    )

    selection = resolve_delivery_selection(contract, plan)

    assert selection.receipt_commands == ("test",)
    assert tuple(item.identity.hook for item in selection.identities) == (
        "after_phase", "before_delivery",
    )
    assert tuple(item.executor for item in selection.executor_identities) == ("engine",)
    assert tuple(item.consequence for item in selection.consequence_identities) == ("warning",)


def test_delivery_selection_resolves_legacy_required_and_manual_only_identities() -> None:
    contract = _contract(
        required=["lint", "manual"],
        commands={"lint": {"run": "x"}, "manual": {"run": "x"}},
        gate_sets={"operator": {"commands": ["manual"]}},
        schedule=[{"manual_only": True, "gate_sets": ["operator"]}],
    )

    selection = resolve_delivery_selection(contract, None)

    assert selection.receipt_commands == ("lint", "manual")
    assert [(item.identity.command, item.identity.hook, item.executor) for item in selection.identities] == [
        ("lint", "before_delivery", "engine"),
        ("manual", "manual_only", "operator"),
    ]


def test_delivery_selection_without_plan_keeps_gate_set_schedule_fail_closed() -> None:
    """A plan-resolution failure must not erase scheduled delivery proof."""
    contract = _contract(
        commands={"smoke": {"run": "x"}},
        required=[],
        gate_sets={"smoke": {"commands": ["smoke"]}},
        selection=[{"always": ["smoke"]}],
        schedule=[{
            "before_delivery": True,
            "gate_sets": ["smoke"],
            "policy": "require",
        }],
    )

    selection = resolve_delivery_selection(contract, None)

    assert selection.receipt_commands == ("smoke",)
    assert [(item.identity.command, item.identity.hook, item.executor) for item in selection.identities] == [
        ("smoke", "before_delivery", "engine"),
    ]


def test_classification_keeps_gate_set_schedule_when_plan_build_fails(
    tmp_path: Path, monkeypatch,
) -> None:
    """The never-raise plan-build path remains fail-closed for delivery."""
    contract = _contract(
        commands={"smoke": {"run": "x"}},
        required=[],
        gate_sets={"smoke": {"commands": ["smoke"]}},
        selection=[{"always": ["smoke"]}],
        schedule=[{
            "before_delivery": True,
            "gate_sets": ["smoke"],
            "policy": "require",
        }],
    )
    monkeypatch.setattr(
        verification_readiness,
        "delivery_gate_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("plan failed")),
    )
    ctx = placeholder_context_for(
        contract,
        checkout=str(tmp_path),
        project=str(tmp_path),
        workspace=str(tmp_path),
        run_dir=str(tmp_path / "run"),
    )

    statuses = verification_readiness.classify_required_receipts(
        contract,
        tmp_path / "run",
        ctx,
        checkout=str(tmp_path),
    )

    assert statuses["smoke"].status == "missing"


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
