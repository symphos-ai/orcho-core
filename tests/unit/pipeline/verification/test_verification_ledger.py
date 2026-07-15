# SPDX-License-Identifier: Apache-2.0
"""Unit tests for pipeline/verification_ledger.py.

The ledger is the single projection over a declared verification contract that
both the start banner and the DONE summary consume. These tests lock:

* the row set / identity is byte-identical to the banner's ``_build_gate_rows``
  (same ``(command, hook, phase)`` multiplicity, including e2e under
  ``manual_only`` and baseline/broad/narrow under ``after_phase(implement)``);
* ``timing`` / ``run_mode`` equal the banner's ``_timing`` / ``_RUN_MODE``;
* the declared activation ``condition`` (always / on_path+globs / operator);
* the identity-based resolve — a plan / changed files disposition each row as
  ``active`` / ``dormant`` / ``manual``, matched by identity, never by command
  name.
"""

from __future__ import annotations

from dataclasses import replace

import core.io.verification_header as verification_header
from pipeline.plugins import PluginConfig
from pipeline.verification_contract import VerificationContract
from pipeline.verification_ledger import (
    TERMINAL_DISPOSITIONS,
    GateLedgerRow,
    GateTrailEvent,
    build_gate_ledger,
    effective_stage,
    gate_run_mode,
    gate_timing,
    reduce_disposition,
)
from pipeline.verification_selection import (
    ScheduledGateEntry,
    ScheduledGatePlan,
)

# ── reference contract (mirrors .orcho/multiagent/plugin.py) ───────────────

_RUN_STATE_PATHS = (
    "pipeline/run_state/**",
    "pipeline/lifecycle.py",
    "tests/unit/pipeline/run_state/**",
)
_VERIFICATION_PATHS = (
    "pipeline/verification*.py",
    "pipeline/verification/**",
    "tests/unit/pipeline/verification/**",
)
_CLI_SDK_PATHS = (
    "cli/**",
    "sdk/**",
    "tests/unit/cli/**",
    "tests/unit/sdk/**",
)


def _reference_contract() -> VerificationContract:
    """A contract shaped like the repo's own multiagent plugin contract."""
    verification = {
        "default_env": "ci",
        "delivery_policy": "warn",
        "required": ["env-provenance", "lint"],
        "commands": {
            "env-provenance": {"env": "ci", "cheap": True, "run": "prov"},
            "lint": {"env": "ci", "cheap": True, "run": "ruff check ."},
            "run-state-unit": {"env": "ci", "run": "pytest run_state"},
            "verification-unit": {"env": "ci", "run": "pytest verification"},
            "cli-sdk-unit": {"env": "ci", "run": "pytest cli sdk"},
            "broad-non-e2e": {"env": "ci", "run": "pytest -m 'not e2e'"},
            "e2e": {"env": "ci", "run": "pytest -m e2e"},
        },
        "gate_sets": {
            "baseline": {
                "commands": ["env-provenance", "lint"],
                "default_policy": "warn",
                "default_cheap": True,
            },
            "run-state": {"commands": ["run-state-unit"], "default_policy": "require"},
            "verification": {
                "commands": ["verification-unit"], "default_policy": "require",
            },
            "cli-sdk": {"commands": ["cli-sdk-unit"], "default_policy": "require"},
            "broad": {"commands": ["broad-non-e2e"], "default_policy": "require"},
            "e2e": {"commands": ["e2e"], "default_policy": "suggest"},
        },
        "selection": [
            {"always": ["baseline", "broad"]},
            {"paths": list(_RUN_STATE_PATHS), "include": ["run-state"]},
            {"paths": list(_VERIFICATION_PATHS), "include": ["verification"]},
            {"paths": list(_CLI_SDK_PATHS), "include": ["cli-sdk"]},
            {"operator": ["e2e"]},
        ],
        "schedule": [
            {
                "after_phase": "implement",
                "gate_sets": ["baseline"],
                "policy": "warn",
            },
            {
                "after_phase": "implement",
                "gate_sets": ["run-state", "verification", "cli-sdk"],
                "policy": "require",
                "action": "repair_loop",
            },
            {
                "after_phase": "implement",
                "gate_sets": ["broad"],
                "policy": "require",
                "action": "repair_loop",
            },
            {"manual_only": True, "gate_sets": ["e2e"], "policy": "suggest"},
        ],
    }
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode="pro",
            verification_envs={"ci": {"image": "python:3.12"}},
            verification=verification,
        ),
    )
    assert contract is not None
    return contract


def _row(rows: tuple[GateLedgerRow, ...], gate: str) -> GateLedgerRow:
    matches = [r for r in rows if r.gate == gate]
    assert len(matches) == 1, f"expected one row for {gate!r}, got {matches!r}"
    return matches[0]


_NARROW = ("run-state-unit", "verification-unit", "cli-sdk-unit")
_BROAD_LIKE = ("env-provenance", "lint", "broad-non-e2e")


# ── row identity matches the banner ─────────────────────────────────────────


def test_row_identity_matches_banner_gate_rows() -> None:
    contract = _reference_contract()
    ledger = build_gate_ledger(contract)

    banner_rows = verification_header._build_gate_rows(contract)
    banner_identity = {(r.gate, r.timing) for r in banner_rows}
    ledger_identity = {(r.gate, r.timing) for r in ledger}
    assert ledger_identity == banner_identity

    # Full (command, hook, phase) identity: six after_phase(implement) gates plus
    # e2e under manual_only, nothing collapsed.
    ids = {(r.gate, r.hook, r.phase) for r in ledger}
    assert ids == {
        ("env-provenance", "after_phase", "implement"),
        ("lint", "after_phase", "implement"),
        ("run-state-unit", "after_phase", "implement"),
        ("verification-unit", "after_phase", "implement"),
        ("cli-sdk-unit", "after_phase", "implement"),
        ("broad-non-e2e", "after_phase", "implement"),
        ("e2e", "manual_only", ""),
    }


def test_timing_and_run_mode_match_ledger_helpers() -> None:
    contract = _reference_contract()
    ledger = build_gate_ledger(contract)
    for row in ledger:
        # The ledger owns the timing/run-mode derivation; each row's cached label
        # equals the public helper for its identity.
        assert row.timing == gate_timing(row.hook, row.phase)
        assert row.run_mode == gate_run_mode(row.hook)
    # Concrete labels: after_phase(implement) -> after_implement / auto; e2e ->
    # operator / manual.
    assert _row(ledger, "lint").timing == "after_implement"
    assert _row(ledger, "lint").run_mode == "auto"
    assert _row(ledger, "e2e").timing == "operator"
    assert _row(ledger, "e2e").run_mode == "manual"


def test_public_timing_helpers() -> None:
    assert gate_timing("after_phase", "implement") == "after_implement"
    assert gate_timing("before_delivery", "") == "delivery"
    assert gate_timing("manual_only", "") == "operator"
    assert gate_run_mode("after_phase") == "auto"
    assert gate_run_mode("manual_only") == "manual"


# ── declared activation condition ───────────────────────────────────────────


def test_condition_always_for_baseline_and_broad() -> None:
    ledger = build_gate_ledger(_reference_contract())
    for gate in _BROAD_LIKE:
        row = _row(ledger, gate)
        assert row.condition == "always", gate
        assert row.condition_paths == ()


def test_condition_on_path_with_globs_for_narrow_gates() -> None:
    ledger = build_gate_ledger(_reference_contract())
    assert _row(ledger, "run-state-unit").condition == "on_path"
    assert _row(ledger, "run-state-unit").condition_paths == _RUN_STATE_PATHS
    assert _row(ledger, "verification-unit").condition_paths == _VERIFICATION_PATHS
    assert _row(ledger, "cli-sdk-unit").condition_paths == _CLI_SDK_PATHS


def test_condition_operator_for_e2e() -> None:
    row = _row(build_gate_ledger(_reference_contract()), "e2e")
    assert row.condition == "operator"
    assert row.condition_paths == ()


def test_task_kind_condition_keeps_its_snapshot_values() -> None:
    contract = VerificationContract.from_plugin(
        PluginConfig(
            verification={
                "commands": {"api": "pytest tests/api"},
                "gate_sets": {"api": {"commands": ["api"]}},
                "selection": [{"task_kind": "api", "include": ["api"]}],
                "schedule": [{"after_phase": "implement", "gate_sets": ["api"]}],
            },
        ),
    )
    assert contract is not None

    row = _row(build_gate_ledger(contract), "api")

    assert row.condition == "task_kind"
    assert row.selection_task_kinds == ("api",)


def test_execution_policy_is_normalized_when_declaration_is_unspecified() -> None:
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode="governed",
            verification={
                "commands": {"lint": "ruff check ."},
                "required": ["lint"],
                "schedule": [{"after_phase": "implement", "commands": ["lint"]}],
            },
        ),
    )
    assert contract is not None

    row = _row(build_gate_ledger(contract), "lint")

    assert row.policy == "unknown"
    assert row.execution_policy == "require"


def test_direct_schedule_row_uses_explicit_always_activation_binding() -> None:
    contract = VerificationContract.from_plugin(
        PluginConfig(
            verification={
                "commands": {"lint": "ruff check ."},
                "schedule": [{"after_phase": "implement", "commands": ["lint"]}],
            },
        ),
    )
    assert contract is not None
    row = _row(build_gate_ledger(contract, changed_files=()), "lint")
    assert row.activation_binding == "always"
    assert row.condition == "always"
    assert row.resolved == "active"


def test_direct_manual_only_row_keeps_operator_binding_when_plan_resolves() -> None:
    contract = VerificationContract.from_plugin(
        PluginConfig(
            verification={
                "commands": {"parity": "pytest tests/parity"},
                "gate_sets": {"expensive": {"commands": ["parity"]}},
                "selection": [{"operator": ["expensive"]}],
                "schedule": [{"manual_only": True, "commands": ["parity"]}],
            },
        ),
    )
    assert contract is not None

    # The direct schedule creates a plan entry with ``always`` even though the
    # operator-gated set was not selected. That plan binding must not overwrite
    # this manual hook's explicit operator binding in the ledger.
    row = _row(build_gate_ledger(contract, changed_files=()), "parity")
    assert row.activation_binding == "operator"
    assert row.condition == "operator"
    assert row.resolved == "manual"


# ── resolve: start (no plan/changed files) ──────────────────────────────────


def test_start_leaves_resolved_none() -> None:
    ledger = build_gate_ledger(_reference_contract())
    assert all(row.resolved is None for row in ledger)


# ── resolve: changed files ──────────────────────────────────────────────────


def test_non_subsystem_diff_actives_broad_dormants_narrow() -> None:
    ledger = build_gate_ledger(
        _reference_contract(), changed_files=("README.md", "docs/x.md"),
    )
    for gate in _BROAD_LIKE:
        assert _row(ledger, gate).resolved == "active", gate
    for gate in _NARROW:
        assert _row(ledger, gate).resolved == "dormant", gate
    assert _row(ledger, "e2e").resolved == "manual"


def test_verification_path_actives_verification_gate() -> None:
    ledger = build_gate_ledger(
        _reference_contract(),
        changed_files=("pipeline/verification_ledger.py",),
    )
    assert _row(ledger, "verification-unit").resolved == "active"
    # The other narrow gates stay dormant; baseline/broad active; e2e manual.
    assert _row(ledger, "run-state-unit").resolved == "dormant"
    assert _row(ledger, "cli-sdk-unit").resolved == "dormant"
    assert _row(ledger, "lint").resolved == "active"
    assert _row(ledger, "e2e").resolved == "manual"


def test_empty_changed_files_still_resolves() -> None:
    # An explicit empty diff (not None) resolves: only the always gates active.
    ledger = build_gate_ledger(_reference_contract(), changed_files=())
    assert _row(ledger, "broad-non-e2e").resolved == "active"
    assert _row(ledger, "run-state-unit").resolved == "dormant"
    assert _row(ledger, "e2e").resolved == "manual"


# ── resolve is by identity, not by command name ─────────────────────────────


def _identity_contract() -> VerificationContract:
    """One command lives in an always set AND a separate path-gated set.

    ``shared`` is scheduled twice: under ``after_phase(implement)`` via the
    always-selected ``always-set`` and under ``before_phase(plan)`` via the
    path-gated ``narrow-set``. Two distinct identities for one command.
    """
    verification = {
        "default_env": "ci",
        "commands": {"shared": {"env": "ci", "run": "run shared"}},
        "gate_sets": {
            "always-set": {"commands": ["shared"], "default_policy": "warn"},
            "narrow-set": {"commands": ["shared"], "default_policy": "require"},
        },
        "selection": [
            {"always": ["always-set"]},
            {"paths": ["src/narrow/**"], "include": ["narrow-set"]},
        ],
        "schedule": [
            {"after_phase": "implement", "gate_sets": ["always-set"]},
            {"before_phase": "plan", "gate_sets": ["narrow-set"]},
        ],
    }
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode="pro",
            verification_envs={"ci": {"image": "python:3.12"}},
            verification=verification,
        ),
    )
    assert contract is not None
    return contract


def test_one_command_two_identities_only_planned_is_active() -> None:
    contract = _identity_contract()
    # Diff does NOT touch src/narrow/** -> only always-set is selected, so
    # plan.entries carries (shared, after_phase, implement) but not
    # (shared, before_phase, plan).
    ledger = build_gate_ledger(contract, changed_files=("README.md",))
    rows = {(r.hook, r.phase): r for r in ledger}
    assert len(rows) == 2, ledger  # two identities, not collapsed

    always_row = rows[("after_phase", "implement")]
    narrow_row = rows[("before_phase", "plan")]
    assert always_row.condition == "always"
    assert always_row.resolved == "active"
    # Same command, different identity absent from plan.entries -> dormant,
    # NOT active. plan.selected_commands (which lists 'shared') is not the source.
    assert narrow_row.condition == "on_path"
    assert narrow_row.resolved == "dormant"


def test_resolve_accepts_prebuilt_plan_by_identity() -> None:
    contract = _identity_contract()
    plan = ScheduledGatePlan(
        entries=(
            ScheduledGateEntry(
                command="shared",
                hook="before_phase",
                phase="plan",
                policy="require",
                action="continue_warn",
                contributing_gate_sets=("narrow-set",),
                primary_gate_set="narrow-set",
            ),
        ),
        selected_gate_sets=("narrow-set",),
        selected_commands=("shared",),
    )
    ledger = build_gate_ledger(contract, plan=plan)
    rows = {(r.hook, r.phase): r for r in ledger}
    # Only the identity present in plan.entries is active.
    assert rows[("before_phase", "plan")].resolved == "active"
    assert rows[("after_phase", "implement")].resolved == "dormant"


# ── policy / kind carried on the row ────────────────────────────────────────


def test_policy_carried_on_row() -> None:
    ledger = build_gate_ledger(_reference_contract())
    # baseline set declares warn (and the entry re-declares warn): warn gates.
    assert _row(ledger, "lint").policy == "warn"
    assert _row(ledger, "env-provenance").policy == "warn"
    # require gate sets: require policy.
    for gate in _NARROW + ("broad-non-e2e",):
        assert _row(ledger, gate).policy == "require", gate
    # e2e is scheduled manual_only with an explicit suggest policy.
    assert _row(ledger, "e2e").policy == "suggest"


def test_kind_carried_on_row() -> None:
    ledger = build_gate_ledger(_reference_contract())
    # env-provenance / lint declare cheap (and baseline default_cheap is true).
    assert _row(ledger, "env-provenance").kind == "cheap"
    assert _row(ledger, "lint").kind == "cheap"
    # No cheap declared anywhere for these -> honest 'unknown'.
    for gate in _NARROW + ("broad-non-e2e", "e2e"):
        assert _row(ledger, gate).kind == "unknown", gate


def test_policy_strictest_backing_default_when_entry_omits() -> None:
    # A contract whose schedule entry omits policy so the strictest backing
    # gate-set default_policy wins (require over warn).
    verification = {
        "default_env": "ci",
        "commands": {"c": {"env": "ci", "run": "run c"}},
        "gate_sets": {
            "laxer": {"commands": ["c"], "default_policy": "warn"},
            "stricter": {"commands": ["c"], "default_policy": "require"},
        },
        "selection": [{"always": ["laxer", "stricter"]}],
        "schedule": [{"after_phase": "implement", "gate_sets": ["laxer", "stricter"]}],
    }
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode="pro",
            verification_envs={"ci": {"image": "python:3.12"}},
            verification=verification,
        ),
    )
    assert contract is not None
    row = _row(build_gate_ledger(contract), "c")
    assert row.policy == "require"


def test_policy_unknown_when_undeclared_anywhere() -> None:
    # No entry policy and no gate-set default_policy -> honest 'unknown'.
    verification = {
        "default_env": "ci",
        "commands": {"c": {"env": "ci", "run": "run c"}},
        "gate_sets": {"bare": {"commands": ["c"]}},
        "selection": [{"always": ["bare"]}],
        "schedule": [{"after_phase": "implement", "gate_sets": ["bare"]}],
    }
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode="pro",
            verification_envs={"ci": {"image": "python:3.12"}},
            verification=verification,
        ),
    )
    assert contract is not None
    assert _row(build_gate_ledger(contract), "c").policy == "unknown"


# ── effective_stage: the four gate classes × has_final_phase branches ────────


def test_effective_stage_require_is_timing_hook() -> None:
    # A required gate runs at its timing hook regardless of the profile.
    for hfp in (True, False, None):
        assert effective_stage("require", "after_phase", "implement", hfp) == (
            "after_implement"
        )
        assert effective_stage("require", "before_delivery", "", hfp) == "delivery"


def test_effective_stage_suggest_and_manual_are_operator() -> None:
    # suggest policy, or a manual/resume hook, is operator-run — never auto.
    assert effective_stage("suggest", "after_phase", "implement", True) == "operator"
    assert effective_stage("warn", "manual_only", "", True) == "operator"
    assert effective_stage("warn", "on_resume", "", False) == "operator"
    # A require gate on a manual hook still reads operator (via gate_timing).
    assert effective_stage("require", "manual_only", "", None) == "operator"


def test_effective_stage_warn_off_branch_on_has_final_phase() -> None:
    for policy in ("warn", "off"):
        assert effective_stage(policy, "after_phase", "implement", True) == (
            "pre-final"
        )
        assert effective_stage(policy, "after_phase", "implement", False) == (
            "not auto-run"
        )
        assert effective_stage(policy, "after_phase", "implement", None) == (
            "profile-dependent"
        )


def test_effective_stage_unknown_policy_follows_warn_off_branch() -> None:
    # An undeclared (unknown) auto gate is not enforced inline, so it follows the
    # same deferred branch as warn/off.
    assert effective_stage("unknown", "after_phase", "implement", True) == "pre-final"
    assert effective_stage("unknown", "after_phase", "implement", False) == (
        "not auto-run"
    )
    assert effective_stage("unknown", "after_phase", "implement", None) == (
        "profile-dependent"
    )


# ── when: derived onto each row via has_final_phase ──────────────────────────


def test_when_require_gates_are_timing_hook_on_row() -> None:
    for hfp in (True, False, None):
        ledger = build_gate_ledger(_reference_contract(), has_final_phase=hfp)
        for gate in _NARROW + ("broad-non-e2e",):
            assert _row(ledger, gate).when == "after_implement", (gate, hfp)


def test_when_warn_gates_track_has_final_phase_on_row() -> None:
    true_ledger = build_gate_ledger(_reference_contract(), has_final_phase=True)
    false_ledger = build_gate_ledger(_reference_contract(), has_final_phase=False)
    none_ledger = build_gate_ledger(_reference_contract(), has_final_phase=None)
    for gate in ("lint", "env-provenance"):
        assert _row(true_ledger, gate).when == "pre-final", gate
        assert _row(false_ledger, gate).when == "not auto-run", gate
        assert _row(none_ledger, gate).when == "profile-dependent", gate


def test_when_e2e_is_operator_regardless_of_profile() -> None:
    for hfp in (True, False, None):
        ledger = build_gate_ledger(_reference_contract(), has_final_phase=hfp)
        assert _row(ledger, "e2e").when == "operator", hfp


def test_when_defaults_to_empty_without_has_final_phase() -> None:
    # No warn gate shows a timing hook; without has_final_phase the warn gates
    # fall to the None (profile-dependent) branch, and no auto gate reads
    # after_implement unless it is required.
    ledger = build_gate_ledger(_reference_contract())
    assert _row(ledger, "lint").when == "profile-dependent"
    for gate in _NARROW + ("broad-non-e2e",):
        assert _row(ledger, gate).when == "after_implement", gate


def test_has_final_phase_does_not_affect_resolve_or_row_set() -> None:
    base = build_gate_ledger(_reference_contract())
    with_hfp = build_gate_ledger(_reference_contract(), has_final_phase=True)
    # Same identities, same resolve (both unresolved at start).
    assert {(r.gate, r.hook, r.phase) for r in base} == {
        (r.gate, r.hook, r.phase) for r in with_hfp
    }
    assert all(r.resolved is None for r in with_hfp)


# ── totality on empty contract ──────────────────────────────────────────────


def test_total_on_empty_selection_schedule() -> None:
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode="pro",
            verification_envs={"ci": {}},
            verification={"commands": {"lint": "ruff check ."}},
        ),
    )
    assert contract is not None
    assert build_gate_ledger(contract) == ()
    assert build_gate_ledger(contract, changed_files=("a.py",)) == ()


# ── durable closure reducer ────────────────────────────────────────────────


def _durable_row(*, selected: bool, policy: str = "require") -> GateLedgerRow:
    return GateLedgerRow(
        gate="check", hook="after_phase", phase="implement",
        timing="after_implement", run_mode="auto", gate_sets=(),
        condition="always", selected=selected, execution_policy=policy,
    )


def test_reducer_covers_all_nine_terminal_dispositions() -> None:
    row = _durable_row(selected=True)
    cases = {
        "not_selected": (_durable_row(selected=False), ()),
        "manual_available": (_durable_row(selected=True, policy="manual"), ()),
        "suggested": (_durable_row(selected=True, policy="suggest"), ()),
        "skipped_fresh": (row, (GateTrailEvent("check", "after_phase", "implement", "reuse", "fresh"),)),
        "executed_pass": (row, (GateTrailEvent("check", "after_phase", "implement", "execution", "pass"),)),
        "executed_fail": (row, (GateTrailEvent("check", "after_phase", "implement", "execution", "fail"),)),
        "residual_missing": (row, ()),
        "residual_stale": (row, (GateTrailEvent("check", "after_phase", "implement", "receipt", "stale"),)),
        "residual_failed": (row, (GateTrailEvent("check", "after_phase", "implement", "receipt", "failed"),)),
    }
    assert set(cases) == TERMINAL_DISPOSITIONS
    for expected, (candidate, events) in cases.items():
        assert reduce_disposition(candidate, events) == expected


def test_not_selected_carries_each_explicit_selection_reason() -> None:
    for reason in ("paths", "task_kind", "operator"):
        row = replace(_durable_row(selected=False), selection_reason=reason)
        assert reduce_disposition(row, ()) == "not_selected"


def test_unvisited_manual_identity_remains_available_but_explicit_nonselection_wins() -> None:
    available = replace(_durable_row(selected=True, policy="manual"), selected=None)
    declined = replace(available, selected=False, selection_reason="operator")

    assert reduce_disposition(available, ()) == "manual_available"
    assert reduce_disposition(declined, ()) == "not_selected"


def test_present_receipt_cannot_infer_executed_pass() -> None:
    row = _durable_row(selected=True)
    receipt = GateTrailEvent(
        "check", "after_phase", "implement", "receipt", "present", "", "receipt.json",
    )
    assert reduce_disposition(row, (receipt,)) == "residual_missing"


def test_reducer_uses_full_identity_for_same_command() -> None:
    executed = _durable_row(selected=True)
    other = GateLedgerRow(
        gate="check", hook="before_phase", phase="plan", timing="before_plan",
        run_mode="auto", gate_sets=(), condition="always", selected=True,
        execution_policy="require",
    )
    event = GateTrailEvent("check", "after_phase", "implement", "execution", "pass")
    assert reduce_disposition(executed, (event,)) == "executed_pass"
    assert reduce_disposition(other, (event,)) == "residual_missing"
