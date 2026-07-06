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

import core.io.verification_header as verification_header
from pipeline.plugins import PluginConfig
from pipeline.verification_contract import VerificationContract
from pipeline.verification_ledger import (
    GateLedgerRow,
    build_gate_ledger,
    gate_run_mode,
    gate_timing,
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
                "action": "continue_warn",
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
