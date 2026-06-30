"""Unit tests for pipeline/verification_contract.py (read-only Stage 1)."""

import json
from pathlib import Path

import pytest

from pipeline.evidence.verification_receipt import write_verification_receipt
from pipeline.plugins import PluginConfig
from pipeline.verification_contract import (
    WORK_MODES,
    PlaceholderContext,
    VerificationContract,
    VerificationContractError,
    render_header_summary,
    render_phase_block,
    resolve_placeholders,
)
from pipeline.verification_readiness import (
    apply_environment_provenance,
    build_final_acceptance_readiness,
    classify_required_receipts,
    required_receipt_gaps,
)


def _contract_plugin(**overrides) -> PluginConfig:
    base = {
        "work_mode": "governed",
        "verification_envs": {"ci": {"image": "python:3.12"}},
        "verification": {
            "default_env": "ci",
            "required": ["test"],
            "commands": {
                "lint": {"run": "ruff check {checkout}", "env": "ci"},
                "test": "pytest -q",
            },
            "schedule": [
                {"after_phase": "implement",
                 "policy": "warn", "commands": ["lint", "test"]},
                {"before_delivery": True, "policy": "require",
                 "commands": ["test"]},
                {"manual_only": True, "policy": "suggest", "commands": ["lint"]},
            ],
        },
        "dependency_repos": {"shared": {"path": "../shared"}},
    }
    base.update(overrides)
    return PluginConfig(**base)


class TestWorkModes:
    """Pin the live operating-mode strictness vocabulary. ``team`` was an
    early ADR 0064 draft name for the middle mode and is retired in favour of
    ``pro``; this test fails if ``team`` is reintroduced or the tuple drifts."""

    def test_work_modes_exact_tuple(self) -> None:
        assert WORK_MODES == ("", "fast", "pro", "governed")

    def test_team_is_not_a_live_work_mode(self) -> None:
        assert "team" not in WORK_MODES


class TestFromPlugin:
    def test_returns_none_when_no_contract_declared(self) -> None:
        assert VerificationContract.from_plugin(PluginConfig()) is None

    def test_explicit_empty_containers_are_not_declared(self) -> None:
        # Explicitly supplying the empty defaults is indistinguishable from
        # "not declared" — it must still be the no-contract path, not an error.
        assert VerificationContract.from_plugin(
            PluginConfig(
                dependency_repos={}, verification_envs={},
                verification={}, work_mode="",
            ),
        ) is None

    def test_declared_wrong_falsey_verification_raises(self) -> None:
        # A declared-but-wrong falsey shape must NOT be coerced to a default;
        # it must reach the type check and raise.
        with pytest.raises(VerificationContractError, match="verification must be a dict"):
            VerificationContract.from_plugin(PluginConfig(verification=[]))

    def test_declared_wrong_falsey_dependency_repos_raises(self) -> None:
        with pytest.raises(VerificationContractError, match="dependency_repos must be a dict"):
            VerificationContract.from_plugin(PluginConfig(dependency_repos=[]))

    def test_declared_wrong_falsey_verification_envs_raises(self) -> None:
        with pytest.raises(VerificationContractError, match="verification_envs must be a dict"):
            VerificationContract.from_plugin(PluginConfig(verification_envs=[]))

    def test_declared_wrong_falsey_default_env_raises(self) -> None:
        with pytest.raises(VerificationContractError, match="default_env must be a str"):
            VerificationContract.from_plugin(
                PluginConfig(verification={"default_env": 0}),
            )

    def test_declared_wrong_falsey_schedule_commands_raises(self) -> None:
        with pytest.raises(VerificationContractError, match="commands must be a list"):
            VerificationContract.from_plugin(
                PluginConfig(verification={
                    "commands": {"lint": "ruff check ."},
                    "schedule": [{"after_phase": "implement", "commands": ""}],
                }),
            )

    def test_builds_normalized_contract(self) -> None:
        contract = VerificationContract.from_plugin(_contract_plugin())
        assert contract is not None
        assert contract.declared is True
        assert contract.work_mode == "governed"
        assert contract.required == ("test",)
        assert contract.default_env == "ci"
        assert set(contract.commands) == {"lint", "test"}
        # string command form normalised to a dict with ``run``.
        assert contract.commands["test"] == {"run": "pytest -q"}
        assert len(contract.schedule) == 3

    def test_raises_on_invalid_work_mode(self) -> None:
        with pytest.raises(VerificationContractError, match="work_mode"):
            VerificationContract.from_plugin(_contract_plugin(work_mode="turbo"))

    def test_raises_on_unknown_schedule_policy(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "schedule": [
                    {"after_phase": "implement",
                     "policy": "panic", "commands": ["lint"]},
                ],
            },
        )
        with pytest.raises(VerificationContractError, match="policy"):
            VerificationContract.from_plugin(plugin)

    def test_raises_on_missing_or_unknown_hook_key(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "schedule": [{"whenever": "implement", "policy": "warn"}],
            },
        )
        with pytest.raises(VerificationContractError, match="hook"):
            VerificationContract.from_plugin(plugin)

    def test_raises_on_command_env_reference_missing(self) -> None:
        plugin = _contract_plugin(
            verification_envs={"ci": {}},
            verification={
                "commands": {"lint": {"run": "ruff check .", "env": "nope"}},
            },
        )
        with pytest.raises(VerificationContractError, match="env"):
            VerificationContract.from_plugin(plugin)

    def test_raises_on_schedule_unknown_command_reference(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "schedule": [
                    {"after_phase": "implement",
                     "policy": "warn", "commands": ["ghost"]},
                ],
            },
        )
        with pytest.raises(VerificationContractError, match="unknown command"):
            VerificationContract.from_plugin(plugin)

    def test_required_is_tuple_of_declared_command_names(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check .", "test": "pytest -q"},
                "required": ["lint", "test"],
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        assert contract.required == ("lint", "test")

    def test_raises_on_required_unknown_command_name(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "required": ["ghost"],
            },
        )
        with pytest.raises(VerificationContractError, match="unknown command"):
            VerificationContract.from_plugin(plugin)

    def test_raises_on_required_non_list(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "required": True,
            },
        )
        with pytest.raises(VerificationContractError, match="required must be a list"):
            VerificationContract.from_plugin(plugin)

    def test_raises_on_unknown_command_parity(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": {"run": "ruff check .", "parity": "bogus"}},
            },
        )
        with pytest.raises(
            VerificationContractError, match="parity must be absolute|differential",
        ):
            VerificationContract.from_plugin(plugin)

    def test_valid_command_parity_values_pass(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {
                    "lint": {"run": "ruff check .", "parity": "absolute"},
                    "test": {"run": "pytest -q", "parity": "differential"},
                },
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        assert contract.commands["lint"]["parity"] == "absolute"
        assert contract.commands["test"]["parity"] == "differential"

    def test_work_mode_only_is_declared(self) -> None:
        contract = VerificationContract.from_plugin(PluginConfig(work_mode="fast"))
        assert contract is not None
        assert contract.work_mode == "fast"

    def test_documented_hook_as_key_schedule_parses_and_projects(self) -> None:
        # The exact hook-as-key schedule form used throughout
        # docs/architecture/verification_contract.md must pass from_plugin and
        # project per phase (regression for the validator/docs mismatch).
        plugin = PluginConfig(
            verification_envs={"canonical-core": {}},
            verification={
                "default_env": "canonical-core",
                "commands": {
                    "lint": {"env": "canonical-core", "run": "ruff check ."},
                    "architecture": {"env": "canonical-core", "run": "pytest -q"},
                    "mcp-smoke": {"env": "canonical-core", "run": "pytest -q smoke"},
                },
                "schedule": [
                    {"after_phase": "implement",
                     "commands": ["lint"], "policy": "warn"},
                    {"before_phase": "final_acceptance",
                     "commands": ["lint", "architecture"], "policy": "warn"},
                    {"before_delivery": True,
                     "commands": ["mcp-smoke"], "policy": "warn"},
                ],
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        ctx = PlaceholderContext()
        # implement sees only its after_phase lint entry.
        implement_block = render_phase_block(contract, "implement", ctx)
        assert implement_block is not None
        assert "lint" in implement_block
        assert "architecture" not in implement_block
        # final_acceptance sees its before_phase entry AND before_delivery.
        final_block = render_phase_block(contract, "final_acceptance", ctx)
        assert final_block is not None
        assert "architecture" in final_block
        assert "mcp-smoke" in final_block


class TestScheduleAbsenceVsExplicit:
    def test_schedule_without_policy_normalizes_to_none(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "schedule": [{"after_phase": "implement", "commands": ["lint"]}],
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        assert contract.schedule[0].policy is None
        assert contract.schedule[0].action is None

    def test_explicit_suggest_policy_is_preserved(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "schedule": [
                    {"after_phase": "implement",
                     "policy": "suggest", "commands": ["lint"]},
                ],
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        assert contract.schedule[0].policy == "suggest"

    def test_explicit_off_policy_is_distinct_from_absence(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "schedule": [
                    {"after_phase": "implement",
                     "policy": "off", "commands": ["lint"]},
                ],
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        assert contract.schedule[0].policy == "off"

    def test_explicit_action_is_preserved(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "schedule": [
                    {"after_phase": "implement",
                     "action": "repair_loop", "commands": ["lint"]},
                ],
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        assert contract.schedule[0].action == "repair_loop"

    def test_raises_on_unknown_schedule_action(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "schedule": [
                    {"after_phase": "implement",
                     "action": "explode", "commands": ["lint"]},
                ],
            },
        )
        with pytest.raises(VerificationContractError, match="action"):
            VerificationContract.from_plugin(plugin)


class TestGateSets:
    def test_gate_set_without_defaults_keeps_none(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "gate_sets": {"core": {"commands": ["lint"]}},
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        gs = contract.gate_sets["core"]
        assert gs.commands == ("lint",)
        assert gs.default_policy is None
        assert gs.default_action is None
        assert gs.default_cheap is None

    def test_gate_set_with_defaults(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "gate_sets": {
                    "core": {
                        "commands": ["lint"],
                        "default_policy": "require",
                        "default_action": "repair_loop",
                        "default_cheap": True,
                    },
                },
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        gs = contract.gate_sets["core"]
        assert gs.default_policy == "require"
        assert gs.default_action == "repair_loop"
        assert gs.default_cheap is True

    def test_gate_set_missing_commands_raises(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "gate_sets": {"core": {"default_policy": "warn"}},
            },
        )
        with pytest.raises(VerificationContractError, match="commands must be a list"):
            VerificationContract.from_plugin(plugin)

    def test_gate_set_unknown_command_raises(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "gate_sets": {"core": {"commands": ["ghost"]}},
            },
        )
        with pytest.raises(VerificationContractError, match="unknown command"):
            VerificationContract.from_plugin(plugin)

    def test_gate_set_invalid_default_policy_raises(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "gate_sets": {
                    "core": {"commands": ["lint"], "default_policy": "panic"},
                },
            },
        )
        with pytest.raises(VerificationContractError, match="default_policy"):
            VerificationContract.from_plugin(plugin)


class TestCommandCheap:
    def test_command_cheap_flag_preserved(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": {"run": "ruff check .", "cheap": True}},
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        assert contract.commands["lint"]["cheap"] is True

    def test_command_cheap_must_be_bool(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": {"run": "ruff check .", "cheap": "yes"}},
            },
        )
        with pytest.raises(VerificationContractError, match="cheap must be a bool"):
            VerificationContract.from_plugin(plugin)


class TestSelection:
    def test_always_rule_includes_gate_sets(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "gate_sets": {"core": {"commands": ["lint"]}},
                "selection": [{"always": ["core"]}],
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        assert len(contract.selection) == 1
        rule = contract.selection[0]
        assert rule.kind == "always"
        assert rule.include == ("core",)

    def test_task_kind_and_paths_rules(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "gate_sets": {"core": {"commands": ["lint"]}},
                "selection": [
                    {"task_kind": "feature", "include": ["core"]},
                    {"paths": ["src/**"], "include": ["core"]},
                    {"operator": ["core"]},
                ],
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        kinds = [r.kind for r in contract.selection]
        assert kinds == ["task_kind", "paths", "operator"]
        assert contract.selection[0].task_kind == "feature"
        assert contract.selection[1].paths == ("src/**",)
        assert contract.selection[2].include == ("core",)

    def test_selection_unknown_gate_set_raises(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "gate_sets": {"core": {"commands": ["lint"]}},
                "selection": [{"always": ["ghost"]}],
            },
        )
        with pytest.raises(VerificationContractError, match="unknown gate_set"):
            VerificationContract.from_plugin(plugin)

    def test_selection_multiple_type_keys_raises(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "gate_sets": {"core": {"commands": ["lint"]}},
                "selection": [{"always": ["core"], "operator": ["core"]}],
            },
        )
        with pytest.raises(VerificationContractError, match="exactly one type key"):
            VerificationContract.from_plugin(plugin)

    def test_schedule_gate_sets_unknown_reference_raises(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "schedule": [
                    {"after_phase": "implement",
                     "commands": ["lint"], "gate_sets": ["ghost"]},
                ],
            },
        )
        with pytest.raises(VerificationContractError, match="unknown gate_set"):
            VerificationContract.from_plugin(plugin)

    def test_schedule_gate_sets_known_reference_passes(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "gate_sets": {"core": {"commands": ["lint"]}},
                "schedule": [
                    {"after_phase": "implement",
                     "commands": ["lint"], "gate_sets": ["core"]},
                ],
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        assert contract.schedule[0].gate_sets == ("core",)


class TestSelectionIntent:
    def test_absent_intent_defaults(self) -> None:
        plugin = _contract_plugin(
            verification={"commands": {"lint": "ruff check ."}},
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        assert contract.task_kind == ""
        assert contract.operator_sets == ()

    def test_declared_task_kind_and_operator_sets(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "gate_sets": {"core": {"commands": ["lint"]}},
                "task_kind": "bugfix",
                "operator_sets": ["core"],
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        assert contract.task_kind == "bugfix"
        assert contract.operator_sets == ("core",)

    def test_task_kind_wrong_type_raises(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "task_kind": 5,
            },
        )
        with pytest.raises(VerificationContractError, match="task_kind must be a str"):
            VerificationContract.from_plugin(plugin)

    def test_operator_sets_unknown_gate_set_raises(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "gate_sets": {"core": {"commands": ["lint"]}},
                "operator_sets": ["ghost"],
            },
        )
        with pytest.raises(VerificationContractError, match="unknown gate_set"):
            VerificationContract.from_plugin(plugin)

    def test_operator_sets_non_list_raises(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "operator_sets": "core",
            },
        )
        with pytest.raises(
            VerificationContractError, match="operator_sets must be a list",
        ):
            VerificationContract.from_plugin(plugin)


class TestStageOneNonePolicyProjection:
    def test_phase_block_shows_derived_for_none_policy(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check {checkout}"},
                "schedule": [{"after_phase": "implement", "commands": ["lint"]}],
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        block = render_phase_block(contract, "implement", PlaceholderContext(checkout="/co"))
        assert block is not None
        assert "[after_phase/derived] lint: ruff check /co" in block

    def test_header_summary_shows_derived_for_none_policy(self) -> None:
        plugin = _contract_plugin(
            verification={
                "commands": {"lint": "ruff check ."},
                "schedule": [{"after_phase": "implement", "commands": ["lint"]}],
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        summary = render_header_summary(contract)
        assert summary is not None
        assert "schedule=derived" in summary


class TestResolvePlaceholders:
    def test_substitutes_known_tokens(self) -> None:
        ctx = PlaceholderContext(
            checkout="/co", project="/proj", workspace="/ws",
            dependencies={"shared": "/deps/shared"},
        )
        out = resolve_placeholders(
            "cd {checkout} && {project} && {workspace} && {dependency:shared}",
            ctx,
        )
        assert out == "cd /co && /proj && /ws && /deps/shared"

    def test_unknown_and_unavailable_tokens_stay_literal(self) -> None:
        ctx = PlaceholderContext(checkout="/co", run_dir=None)
        out = resolve_placeholders(
            "{checkout} {run_dir} {bogus} {dependency:missing}", ctx,
        )
        assert out == "/co {run_dir} {bogus} {dependency:missing}"

    def test_empty_text_does_not_raise(self) -> None:
        assert resolve_placeholders("", PlaceholderContext()) == ""


class TestRenderHeaderSummary:
    def test_none_contract_returns_none(self) -> None:
        assert render_header_summary(None) is None

    def test_compact_name_summary(self) -> None:
        contract = VerificationContract.from_plugin(_contract_plugin())
        summary = render_header_summary(contract)
        assert summary is not None
        assert "work_mode=governed" in summary
        assert "envs=ci" in summary
        assert "commands=lint,test" in summary
        assert "schedule=" in summary
        # names only — no resolved paths leak into the header.
        assert "{checkout}" not in summary
        assert "/shared" not in summary


class TestRenderPhaseBlock:
    def test_none_contract_returns_none(self) -> None:
        assert render_phase_block(None, "implement", PlaceholderContext()) is None

    def test_phase_limited_subset_with_resolved_placeholders(self) -> None:
        contract = VerificationContract.from_plugin(_contract_plugin())
        ctx = PlaceholderContext(checkout="/co")
        block = render_phase_block(contract, "implement", ctx)
        assert block is not None
        # implement only sees its after_phase entry.
        assert "[after_phase/warn] lint: ruff check /co" in block
        assert "[after_phase/warn] test: pytest -q" in block
        # before_delivery / manual_only entries are NOT shown for implement.
        assert "before_delivery" not in block
        assert "manual_only" not in block

    def test_before_delivery_shown_only_for_final_phase(self) -> None:
        contract = VerificationContract.from_plugin(_contract_plugin())
        ctx = PlaceholderContext()
        block = render_phase_block(contract, "final_acceptance", ctx)
        assert block is not None
        assert "[before_delivery/require] test" in block
        # implement-scoped after_phase entry is not shown on the final phase.
        assert "after_phase" not in block

    def test_returns_none_for_phase_without_entries(self) -> None:
        contract = VerificationContract.from_plugin(_contract_plugin())
        assert render_phase_block(contract, "plan", PlaceholderContext()) is None


def _prov_contract() -> VerificationContract:
    """A required ``env-provenance`` gate scheduled at after_phase(implement)
    under a ``require`` policy (the ADR 0108 overlay subject)."""
    plugin = PluginConfig(
        verification_envs={"ci": {}},
        verification={
            "default_env": "ci",
            "required": ["env-provenance"],
            "commands": {"env-provenance": {"run": "echo prov"}},
            "delivery_policy": "require",
            "schedule": [
                {"after_phase": "implement", "policy": "require",
                 "commands": ["env-provenance"]},
            ],
        },
    )
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    return contract


def _manual_prov_contract() -> VerificationContract:
    """Same phase-scheduled gate, but ALSO marked manual_only — never blocking.

    The ``after_phase(implement)`` entry keeps the env-provenance link (so the
    overlay can downgrade it), while the ``manual_only`` entry makes its effective
    policy manual, so each surface must keep it non-blocking.
    """
    plugin = PluginConfig(
        verification_envs={"ci": {}},
        verification={
            "default_env": "ci",
            "required": ["env-provenance"],
            "commands": {"env-provenance": {"run": "echo prov"}},
            "schedule": [
                {"after_phase": "implement", "policy": "warn",
                 "commands": ["env-provenance"]},
                {"manual_only": True, "commands": ["env-provenance"]},
            ],
        },
    )
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    return contract


def _write_passing_command_receipt(run_dir: Path, command: str) -> None:
    rdir = run_dir / "verification_command_receipts"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / f"{command}.json").write_text(
        json.dumps({
            "kind": "verification_command",
            "command": command,
            "env": "ci",
            "exit_code": 0,
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


class TestEnvironmentProvenanceOverlay:
    """ADR 0108 T1: the shared overlay downgrades a phase-scheduled require gate
    whose ``verification_environment`` receipt failed, even when its own command
    receipt passed, so readiness reports it failed and emits a release gap."""

    def test_failed_provenance_makes_readiness_required_failed(
        self, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _prov_contract()
        # A passing command receipt would otherwise read present ...
        _write_passing_command_receipt(run_dir, "env-provenance")
        # ... but the implement phase's environment provenance broke.
        _write_failed_phase_receipt(run_dir)

        summary = build_final_acceptance_readiness(
            contract, run_dir, PlaceholderContext(checkout=str(tmp_path)),
        )

        assert "env-provenance" in summary.required_failed
        assert "env-provenance" not in summary.required_present

    def test_failed_provenance_emits_require_release_gap(
        self, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _prov_contract()
        _write_passing_command_receipt(run_dir, "env-provenance")
        _write_failed_phase_receipt(run_dir)

        gaps = required_receipt_gaps(
            contract, run_dir, PlaceholderContext(checkout=str(tmp_path)),
        )

        assert any("env-provenance" in g["risk"] for g in gaps)

    def test_overlay_preserves_source_run_id_and_repoints_path(
        self, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _prov_contract()
        _write_passing_command_receipt(run_dir, "env-provenance")
        phase_path = _write_failed_phase_receipt(run_dir)

        base = classify_required_receipts(
            contract, run_dir, PlaceholderContext(checkout=str(tmp_path)),
            checkout=str(tmp_path),
        )
        overlaid = apply_environment_provenance(base, contract, run_dir)

        cls = overlaid["env-provenance"]
        assert cls.status == "failed"
        assert cls.reason.startswith("pipeline_import:")
        assert cls.path == str(phase_path)
        # The base receipt's provenance (source run id) is preserved.
        assert cls.source_run_id == base["env-provenance"].source_run_id

    def test_healthy_provenance_leaves_overlay_unchanged(
        self, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _prov_contract()
        _write_passing_command_receipt(run_dir, "env-provenance")
        # No phase receipt at all -> no provenance failure -> no downgrade.

        base = classify_required_receipts(
            contract, run_dir, PlaceholderContext(checkout=str(tmp_path)),
            checkout=str(tmp_path),
        )
        overlaid = apply_environment_provenance(base, contract, run_dir)

        assert overlaid["env-provenance"].status == "present"
        summary = build_final_acceptance_readiness(
            contract, run_dir, PlaceholderContext(checkout=str(tmp_path)),
        )
        assert summary.required_failed == ()

    def test_manual_only_provenance_gate_stays_non_blocking(
        self, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        contract = _manual_prov_contract()
        _write_passing_command_receipt(run_dir, "env-provenance")
        _write_failed_phase_receipt(run_dir)

        summary = build_final_acceptance_readiness(
            contract, run_dir, PlaceholderContext(checkout=str(tmp_path)),
        )

        # Manual gate is surfaced as manual-only, never a required/blocking gap.
        assert "env-provenance" in summary.manual_only_gaps
        assert "env-provenance" not in summary.required_failed
        # And it never becomes a require release gap.
        gaps = required_receipt_gaps(
            contract, run_dir, PlaceholderContext(checkout=str(tmp_path)),
        )
        assert gaps == []
