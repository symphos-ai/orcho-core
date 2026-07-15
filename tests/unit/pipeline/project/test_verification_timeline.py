"""Unit tests for the live verification-gate render model (ADR 0095 / 0125).

Pins the environment-provenance downgrade on the *live* DONE/HALTED surface
(:mod:`pipeline.project.verification_timeline`), the companion to the typed SDK
projection: a required gate scheduled at a phase whose ``verification_environment``
receipt recorded a failed check is forced into the failed residual (=>
``residual_failed`` + ``blocking_residual`` under a ``require`` policy + the shared
``fix`` hint), using the SAME shared rule the SDK projection uses so the two can
never diverge. Healthy/absent provenance keeps the prior semantics intact.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.evidence.verification_receipt import write_verification_receipt
from pipeline.plugins import PluginConfig
from pipeline.project.verification_timeline import (
    _run_level_projection,
    build_verification_timeline,
    render_verification_gate_done_block,
)
from pipeline.verification_contract import (
    VerificationContract,
    placeholder_context_for,
)


def test_done_ledger_projection_keeps_duplicate_identities_and_dispositions(tmp_path: Path) -> None:
    """Ledger-backed DONE never collapses a duplicated command by name."""
    from pipeline.verification_ledger import GateLedgerRow
    from pipeline.verification_ledger_store import ScheduledGateLedger, write_ledger

    rows = (
        GateLedgerRow(
            "check", "after_phase", "implement", "after_implement", "auto", (), "always",
            selected=True, execution_policy="require", consequence="required_action",
            disposition="executed_pass",
        ),
        GateLedgerRow(
            "check", "before_delivery", "", "delivery", "auto", (), "on_path",
            selected=False, execution_policy="require", selection_reason="paths",
            disposition="not_selected",
        ),
    )
    write_ledger(tmp_path, ScheduledGateLedger(rows, finalized=True))
    timeline = build_verification_timeline(run_dir=tmp_path, extras={})
    assert timeline is not None
    block = "\n".join(render_verification_gate_done_block(timeline))
    assert "check (after_phase:implement)" in block
    assert "check (before_delivery)" in block
    assert "disposition=executed_pass" in block
    assert "disposition=not_selected" in block


def _prov_contract() -> VerificationContract:
    """A contract whose required ``env-provenance`` gate is scheduled at
    after_phase(implement) with a ``require`` policy."""
    plugin = PluginConfig(
        verification_envs={"ci": {}},
        verification={
            "default_env": "ci",
            "required": ["env-provenance"],
            "commands": {"env-provenance": {"run": "echo prov"}},
            "delivery_policy": "require",
            "schedule": [
                {
                    "after_phase": "implement",
                    "policy": "require",
                    "commands": ["env-provenance"],
                },
            ],
        },
    )
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    return contract


def _manual_prov_contract() -> VerificationContract:
    """Same phase-scheduled gate, but ALSO marked manual_only — never blocking."""
    plugin = PluginConfig(
        verification_envs={"ci": {}},
        verification={
            "default_env": "ci",
            "required": ["env-provenance"],
            "commands": {"env-provenance": {"run": "echo prov"}},
            "schedule": [
                {
                    "after_phase": "implement",
                    "policy": "warn",
                    "commands": ["env-provenance"],
                },
                {"manual_only": True, "commands": ["env-provenance"]},
            ],
        },
    )
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    return contract


def _extras(contract: VerificationContract, checkout: Path, run_dir: Path) -> dict:
    return {
        "verification_contract": contract,
        "verification_placeholders": placeholder_context_for(
            contract,
            checkout=str(checkout),
            project=str(checkout),
            workspace=str(run_dir),
            run_dir=str(run_dir),
        ),
    }


def _write_command_receipt(run_dir: Path, command: str, *, exit_code: int = 0) -> None:
    rdir = run_dir / "verification_command_receipts"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / f"{command}.json").write_text(
        json.dumps({
            "kind": "verification_command",
            "command": command,
            "env": "ci",
            "exit_code": exit_code,
            "assertions": [],
            "detail": "",
            "git": {
                "checkout_head": None,
                "baseline_head": None,
                "changed_files_fingerprint": None,
            },
            "dependencies": [],
        }),
        encoding="utf-8",
    )


def _write_failed_phase_receipt(run_dir: Path) -> Path:
    path = write_verification_receipt(
        output_dir=run_dir,
        phase="implement",
        round=1,
        cwd=run_dir,
        checks=[{
            "name": "pipeline_import",
            "expected": "/abs/checkout/pipeline/__init__.py",
            "actual": "/abs/install/pipeline/__init__.py",
            "passed": False,
        }],
    )
    assert path is not None
    return path


def _write_healthy_phase_receipt(run_dir: Path) -> Path:
    path = write_verification_receipt(
        output_dir=run_dir,
        phase="implement",
        round=1,
        cwd=run_dir,
        checks=[{
            "name": "pipeline_import",
            "expected": "/abs/checkout/pipeline/__init__.py",
            "actual": "/abs/checkout/pipeline/__init__.py",
            "passed": True,
        }],
    )
    assert path is not None
    return path


class TestProvenanceRunLevelProjection:
    def test_failed_provenance_is_failed_residual_and_blocking(
        self, tmp_path: Path,
    ) -> None:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _prov_contract()
        # A passing command receipt would otherwise read present/PASS ...
        _write_command_receipt(run_dir, "env-provenance", exit_code=0)
        # ... but the implement phase's environment provenance broke.
        _write_failed_phase_receipt(run_dir)

        projection = _run_level_projection(
            contract, run_dir, _extras(contract, checkout, run_dir),
        )

        assert "env-provenance" in projection.residual_failed
        # The require policy makes it a blocker on the built timeline.
        timeline = build_verification_timeline(
            run_dir=run_dir,
            extras=_extras(contract, checkout, run_dir),
        )
        assert timeline is not None
        assert "env-provenance" in timeline.residual_failed
        assert "env-provenance" in timeline.blocking_residual

    def test_healthy_provenance_does_not_fail_present_gate(
        self, tmp_path: Path,
    ) -> None:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _prov_contract()
        _write_command_receipt(run_dir, "env-provenance", exit_code=0)
        _write_healthy_phase_receipt(run_dir)

        projection = _run_level_projection(
            contract, run_dir, _extras(contract, checkout, run_dir),
        )

        # No provenance break -> the present command receipt is not downgraded.
        assert projection.residual_failed == ()

    def test_manual_only_provenance_gate_stays_non_blocking(
        self, tmp_path: Path,
    ) -> None:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _manual_prov_contract()
        _write_command_receipt(run_dir, "env-provenance", exit_code=0)
        _write_failed_phase_receipt(run_dir)

        projection = _run_level_projection(
            contract, run_dir, _extras(contract, checkout, run_dir),
        )

        # A manual gate is never escalated by the overlay: it is surfaced as
        # manual-only, never as a failed/blocking residual.
        assert "env-provenance" in projection.manual_only
        assert projection.residual_failed == ()
        timeline = build_verification_timeline(
            run_dir=run_dir,
            extras=_extras(contract, checkout, run_dir),
        )
        assert timeline is not None
        assert "env-provenance" in timeline.manual_only
        assert "env-provenance" not in timeline.blocking_residual


class TestProvenanceDoneBlock:
    def test_done_block_shows_provenance_in_residual_blocking_and_fix(
        self, tmp_path: Path,
    ) -> None:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _prov_contract()
        _write_command_receipt(run_dir, "env-provenance", exit_code=0)
        _write_failed_phase_receipt(run_dir)

        timeline = build_verification_timeline(
            run_dir=run_dir,
            extras=_extras(contract, checkout, run_dir),
        )
        block = "\n".join(render_verification_gate_done_block(timeline))

        assert "residual: " in block
        assert "failed=env-provenance" in block
        assert "blocking (require): env-provenance" in block
        # The shared fix hint is printed for the open required deficit.
        assert "fix:" in block
        assert any(
            line.strip().startswith("fix:") and "orcho verify" in line
            for line in block.splitlines()
        )


def test_no_phase_receipt_keeps_present_gate_present(tmp_path: Path) -> None:
    """With no verification_environment receipt at all, the gate keeps its prior
    semantics (a passing command receipt is present, not failed)."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    contract = _prov_contract()
    _write_command_receipt(run_dir, "env-provenance", exit_code=0)

    projection = _run_level_projection(
        contract, run_dir, _extras(contract, checkout, run_dir),
    )

    assert projection.residual_failed == ()
    assert projection.residual_missing == ()


# ── T3: declared scheduled-gate dispositions in DONE ───────────────────────


def _ledger_contract() -> VerificationContract:
    """A gate-set/selection/schedule contract shaped like the repo's own plugin.

    baseline/broad are ``always``; run-state/verification/cli-sdk are path-gated;
    e2e is operator/manual — the mix that exercises the DONE dispositions.
    """
    plugin = PluginConfig(
        work_mode="pro",
        verification_envs={"ci": {}},
        verification={
            "default_env": "ci",
            "required": ["env-provenance", "lint"],
            "commands": {
                n: {"run": "true"}
                for n in (
                    "env-provenance", "lint", "run-state-unit",
                    "verification-unit", "cli-sdk-unit", "broad-non-e2e", "e2e",
                )
            },
            "gate_sets": {
                "baseline": {
                    "commands": ["env-provenance", "lint"],
                    "default_policy": "warn",
                },
                "run-state": {
                    "commands": ["run-state-unit"], "default_policy": "require",
                },
                "verification": {
                    "commands": ["verification-unit"], "default_policy": "require",
                },
                "cli-sdk": {
                    "commands": ["cli-sdk-unit"], "default_policy": "require",
                },
                "broad": {
                    "commands": ["broad-non-e2e"], "default_policy": "require",
                },
                "e2e": {"commands": ["e2e"], "default_policy": "suggest"},
            },
            "selection": [
                {"always": ["baseline", "broad"]},
                {"paths": ["pipeline/run_state/**"], "include": ["run-state"]},
                {"paths": ["pipeline/verification*.py"], "include": ["verification"]},
                {"paths": ["cli/**", "sdk/**"], "include": ["cli-sdk"]},
                {"operator": ["e2e"]},
            ],
            "schedule": [
                {"after_phase": "implement", "gate_sets": ["baseline"], "policy": "warn"},
                {
                    "after_phase": "implement",
                    "gate_sets": ["run-state", "verification", "cli-sdk"],
                    "policy": "require",
                },
                {"after_phase": "implement", "gate_sets": ["broad"], "policy": "require"},
                {"manual_only": True, "gate_sets": ["e2e"], "policy": "suggest"},
            ],
        },
    )
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    return contract


def _ledger_extras(
    contract: VerificationContract, checkout: Path, run_dir: Path, touched,
) -> dict:
    """Extras carrying the contract plus a cached delivery routing plan.

    The plan is built from ``touched`` and stashed at the delivery epoch so
    ``delivery_gate_plan`` reuses it (no git / no second selection pass) — the
    SAME plan the DONE dispositions resolve against, by identity.
    """
    from pipeline.verification_readiness import ROUTING_PLANS_EXTRAS_KEY
    from pipeline.verification_selection import (
        SelectionContext,
        build_scheduled_gate_plan,
    )

    plan = build_scheduled_gate_plan(
        contract, SelectionContext(touched_paths=tuple(touched), work_mode="pro"),
    )
    extras = _extras(contract, checkout, run_dir)
    extras[ROUTING_PLANS_EXTRAS_KEY] = {"before_delivery:": plan}
    return extras


def _banner_identities(contract: VerificationContract) -> set:
    """The (command, hook, phase) identities the START banner declares."""
    from pipeline.verification_ledger import build_gate_ledger

    return {(r.gate, r.hook, r.phase) for r in build_gate_ledger(contract)}


def test_done_dispositions_non_subsystem_diff(tmp_path: Path) -> None:
    # A diff touching nothing subsystem-owned: baseline/broad activate, the three
    # narrow path-gated gates are dormant (not selected), e2e is manual.
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    contract = _ledger_contract()
    extras = _ledger_extras(contract, checkout, run_dir, ["README.md"])

    timeline = build_verification_timeline(run_dir=run_dir, extras=extras)
    assert timeline is not None
    assert timeline.not_selected_on_path == (
        "run-state-unit", "verification-unit", "cli-sdk-unit",
    )
    assert timeline.manual_declared == ("e2e",)

    block = "\n".join(render_verification_gate_done_block(timeline))
    assert (
        "not selected (no matching path): "
        "run-state-unit, verification-unit, cli-sdk-unit"
    ) in block
    # e2e is on its own manual line, never folded into the 'no matching path' one.
    assert "manual (operator-run): e2e" in block
    not_sel_line = next(
        ln for ln in block.splitlines() if "no matching path" in ln
    )
    assert "e2e" not in not_sel_line


def test_done_dispositions_verification_diff_activates_that_gate(
    tmp_path: Path,
) -> None:
    # A diff on a verification-owned path activates verification-unit, so it is
    # NOT in the not-selected line; the other two narrow gates stay dormant.
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    contract = _ledger_contract()
    extras = _ledger_extras(
        contract, checkout, run_dir, ["pipeline/verification_ledger.py"],
    )

    timeline = build_verification_timeline(run_dir=run_dir, extras=extras)
    assert timeline is not None
    assert "verification-unit" not in timeline.not_selected_on_path
    assert timeline.not_selected_on_path == ("run-state-unit", "cli-sdk-unit")
    assert timeline.manual_declared == ("e2e",)


def test_done_dispositions_close_every_banner_identity(tmp_path: Path) -> None:
    # Every scheduled-gate identity the banner declares is closed in DONE with
    # exactly one disposition (active / dormant / manual) by ledger identity —
    # resolved against plan.entries, not plan.selected_commands.
    from pipeline.verification_ledger import build_gate_ledger
    from pipeline.verification_selection import (
        SelectionContext,
        build_scheduled_gate_plan,
    )

    contract = _ledger_contract()
    plan = build_scheduled_gate_plan(
        contract, SelectionContext(touched_paths=("README.md",), work_mode="pro"),
    )
    rows = build_gate_ledger(contract, plan=plan)

    # Partition: each row lands in exactly one disposition, covering the banner.
    active = {(r.gate, r.hook, r.phase) for r in rows if r.resolved == "active"}
    dormant = {(r.gate, r.hook, r.phase) for r in rows if r.resolved == "dormant"}
    manual = {(r.gate, r.hook, r.phase) for r in rows if r.resolved == "manual"}
    assert active | dormant | manual == _banner_identities(contract)
    # Disjoint — no identity closed twice.
    assert not (active & dormant) and not (active & manual) and not (dormant & manual)
    # Concretely for this diff: broad-like active, narrow dormant, e2e manual.
    assert ("broad-non-e2e", "after_phase", "implement") in active
    assert ("run-state-unit", "after_phase", "implement") in dormant
    assert ("e2e", "manual_only", "") in manual


def test_done_disposition_resolves_by_identity_not_command(tmp_path: Path) -> None:
    # One command ('shared') scheduled under TWO identities: an always-selected
    # after_phase(implement) entry and a path-gated before_phase(plan) entry. With
    # a diff that does not touch the narrow path, only the always identity is in
    # plan.entries -> active; the other identity stays dormant, NOT active,
    # despite 'shared' appearing in plan.selected_commands.
    from pipeline.verification_ledger import build_gate_ledger
    from pipeline.verification_selection import (
        SelectionContext,
        build_scheduled_gate_plan,
    )

    plugin = PluginConfig(
        work_mode="pro",
        verification_envs={"ci": {}},
        verification={
            "default_env": "ci",
            "commands": {"shared": {"run": "true"}},
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
        },
    )
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    plan = build_scheduled_gate_plan(
        contract, SelectionContext(touched_paths=("README.md",), work_mode="pro"),
    )
    assert "shared" in plan.selected_commands  # the misleading command-name signal
    rows = build_gate_ledger(contract, plan=plan)
    by_identity = {(r.hook, r.phase): r.resolved for r in rows}
    assert by_identity[("after_phase", "implement")] == "active"
    assert by_identity[("before_phase", "plan")] == "dormant"


def _multi_identity_contract(*, first_always: bool) -> VerificationContract:
    """One command ('shared') scheduled under two ``(hook, phase)`` identities.

    ``set-a`` backs an ``after_phase(implement)`` entry, ``set-b`` backs a
    ``before_phase(plan)`` entry. When ``first_always`` the ``set-a`` identity is
    ``always``-selected (so it resolves ``active``); otherwise both sets are
    path-gated on disjoint subsystem globs, so a non-subsystem diff leaves BOTH
    identities ``dormant``.
    """
    plugin = PluginConfig(
        work_mode="pro",
        verification_envs={"ci": {}},
        verification={
            "default_env": "ci",
            "commands": {"shared": {"run": "true"}},
            "gate_sets": {
                "set-a": {"commands": ["shared"], "default_policy": "require"},
                "set-b": {"commands": ["shared"], "default_policy": "require"},
            },
            "selection": (
                [{"always": ["set-a"]}]
                if first_always
                else [{"paths": ["a/**"], "include": ["set-a"]}]
            ) + [{"paths": ["b/**"], "include": ["set-b"]}],
            "schedule": [
                {
                    "after_phase": "implement",
                    "gate_sets": ["set-a"],
                    "policy": "require",
                },
                {
                    "before_phase": "plan",
                    "gate_sets": ["set-b"],
                    "policy": "require",
                },
            ],
        },
    )
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    return contract


def test_done_closes_each_dormant_identity_of_one_command_separately(
    tmp_path: Path,
) -> None:
    # F1 regression: a command with two dormant scheduled identities must NOT
    # collapse to a single 'shared' disposition. Each identity is closed on its
    # own, disambiguated by the same timing label the start banner shows.
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    contract = _multi_identity_contract(first_always=False)
    extras = _ledger_extras(contract, checkout, run_dir, ["README.md"])

    timeline = build_verification_timeline(run_dir=run_dir, extras=extras)
    assert timeline is not None
    assert timeline.not_selected_on_path == (
        "shared (after_implement)", "shared (before_plan)",
    )

    block = "\n".join(render_verification_gate_done_block(timeline))
    not_sel_line = next(
        ln for ln in block.splitlines() if "no matching path" in ln
    )
    assert "shared (after_implement)" in not_sel_line
    assert "shared (before_plan)" in not_sel_line


def test_done_closes_active_and_dormant_identity_of_one_command_separately(
    tmp_path: Path,
) -> None:
    # One command, two identities: the always-selected one resolves ``active``
    # (closed via its own outcome, not the dormant line), while the path-gated
    # one resolves ``dormant`` and is closed on the 'no matching path' line — kept
    # distinguishable by timing, so the active identity never leaks into it.
    from pipeline.verification_ledger import build_gate_ledger
    from pipeline.verification_selection import (
        SelectionContext,
        build_scheduled_gate_plan,
    )

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    contract = _multi_identity_contract(first_always=True)
    extras = _ledger_extras(contract, checkout, run_dir, ["README.md"])

    plan = build_scheduled_gate_plan(
        contract, SelectionContext(touched_paths=("README.md",), work_mode="pro"),
    )
    by_identity = {
        (r.hook, r.phase): r.resolved
        for r in build_gate_ledger(contract, plan=plan)
    }
    assert by_identity[("after_phase", "implement")] == "active"
    assert by_identity[("before_phase", "plan")] == "dormant"

    timeline = build_verification_timeline(run_dir=run_dir, extras=extras)
    assert timeline is not None
    # Only the dormant identity is on the not-selected line; the active identity
    # is not folded in with it.
    assert timeline.not_selected_on_path == ("shared (before_plan)",)
    block = "\n".join(render_verification_gate_done_block(timeline))
    not_sel_line = next(
        ln for ln in block.splitlines() if "no matching path" in ln
    )
    assert "shared (before_plan)" in not_sel_line
    assert "after_implement" not in not_sel_line


# ── T4: observed-diff integration legibility on the real plugin contract ────


def _real_plugin_contract() -> VerificationContract:
    """Load the repo's own multiagent verification contract (real globs).

    This pins the observed-scenario check to the ACTUAL selection globs rather
    than a simplified fixture, so a real diff that touches no subsystem path is
    proven to leave the narrow gates dormant. Skips if the plugin is absent.
    """
    import importlib.util

    root = Path(__file__).resolve().parents[4]
    plugin_path = root / ".orcho" / "multiagent" / "plugin.py"
    if not plugin_path.exists():
        import pytest

        pytest.skip("repo multiagent plugin contract not present")
    spec = importlib.util.spec_from_file_location(
        "_orcho_plugin_under_test", plugin_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = module.PLUGIN
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode=plugin["work_mode"],
            verification_envs=plugin["verification_envs"],
            verification=plugin["verification"],
        ),
    )
    assert contract is not None
    return contract


# The observed diff from T4: it touches only these files and matches NO declared
# subsystem glob, so baseline/broad activate while the three narrow gates stay
# dormant and e2e stays manual.
_OBSERVED_DIFF = (
    "pipeline/project/profile_dispatch.py",
    "pipeline/project/run.py",
    "pipeline/project/resume_phase_summary.py",
    "pipeline/runtime/runner.py",
)


def test_observed_diff_closes_every_banner_identity_in_done(tmp_path: Path) -> None:
    # Integration legibility (T4): for the real plugin contract and the observed
    # diff, EVERY start-banner scheduled-gate identity is closed in DONE with
    # exactly one disposition and nothing disappears.
    from pipeline.verification_ledger import build_gate_ledger
    from pipeline.verification_selection import (
        SelectionContext,
        build_scheduled_gate_plan,
    )

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    contract = _real_plugin_contract()
    extras = _ledger_extras(contract, checkout, run_dir, _OBSERVED_DIFF)

    timeline = build_verification_timeline(run_dir=run_dir, extras=extras)
    assert timeline is not None
    assert timeline.not_selected_on_path == (
        "run-state-unit", "verification-unit", "cli-sdk-unit",
    )
    assert timeline.manual_declared == ("e2e",)

    plan = build_scheduled_gate_plan(
        contract, SelectionContext(touched_paths=_OBSERVED_DIFF, work_mode="pro"),
    )
    rows = build_gate_ledger(contract, plan=plan)
    banner = {(r.gate, r.hook, r.phase) for r in build_gate_ledger(contract)}
    active = {(r.gate, r.hook, r.phase) for r in rows if r.resolved == "active"}
    dormant = {(r.gate, r.hook, r.phase) for r in rows if r.resolved == "dormant"}
    manual = {(r.gate, r.hook, r.phase) for r in rows if r.resolved == "manual"}

    # Exactly the seven banner identities, partitioned with no overlap and none
    # dropped.
    assert active | dormant | manual == banner
    assert not (active & dormant) and not (active & manual) and not (dormant & manual)
    assert len(active) + len(dormant) + len(manual) == len(banner) == 7
    assert {g for g, _, _ in active} == {"env-provenance", "lint", "broad-non-e2e"}
    assert {g for g, _, _ in dormant} == {
        "run-state-unit", "verification-unit", "cli-sdk-unit",
    }
    assert {g for g, _, _ in manual} == {"e2e"}

    block = "\n".join(render_verification_gate_done_block(timeline))
    assert (
        "not selected (no matching path): "
        "run-state-unit, verification-unit, cli-sdk-unit"
    ) in block
    assert "manual (operator-run): e2e" in block
    # e2e is on its own manual line, never in the 'no matching path' line.
    not_sel_line = next(ln for ln in block.splitlines() if "no matching path" in ln)
    assert "e2e" not in not_sel_line
