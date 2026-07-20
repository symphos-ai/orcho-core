# SPDX-License-Identifier: Apache-2.0
"""Lifecycle ownership tests for the scheduled-gate ledger runtime."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.plugins import PluginConfig
from pipeline.project.verification_ledger_runtime import (
    ResumeVerificationLedgerError,
    finalize,
    initialize,
    record_execution,
    select_epoch,
)
from pipeline.verification_contract import VerificationContract
from pipeline.verification_ledger_store import load_ledger
from pipeline.verification_selection import SelectionContext


def _contract(command: str = "check") -> VerificationContract:
    contract = VerificationContract.from_plugin(PluginConfig(verification={
        "commands": {command: {"run": "pytest"}},
        "gate_sets": {"core": {"commands": [command]}},
        "selection": [{"always": ["core"]}],
        "schedule": [{"after_phase": "implement", "gate_sets": ["core"], "policy": "require"}],
    }))
    assert contract is not None
    return contract


def _automatic_and_manual_contract() -> VerificationContract:
    contract = VerificationContract.from_plugin(PluginConfig(verification={
        "commands": {
            "cli-sdk-unit": {"run": "pytest tests/unit/cli"},
            "manual": {"run": "pytest"},
            "suggested": {"run": "pytest"},
        },
        "schedule": [
            {"before_delivery": True, "commands": ["cli-sdk-unit"], "policy": "require"},
            {"manual_only": True, "commands": ["manual"], "policy": "manual"},
            {"manual_only": True, "commands": ["suggested"], "policy": "suggest"},
        ],
    }))
    assert contract is not None
    return contract


def _path_contract() -> VerificationContract:
    contract = VerificationContract.from_plugin(PluginConfig(verification={
        "commands": {"cli-sdk-unit": {"run": "pytest tests/unit/cli"}},
        "gate_sets": {"cli": {"commands": ["cli-sdk-unit"]}},
        "selection": [{"paths": ["tests/unit/cli/**"], "include": ["cli"]}],
        "schedule": [{"before_delivery": True, "gate_sets": ["cli"], "policy": "require"}],
    }))
    assert contract is not None
    return contract


def _delivery_path_contract() -> VerificationContract:
    """A path gate selected only at ``after_phase:implement``."""
    contract = VerificationContract.from_plugin(PluginConfig(verification={
        "commands": {"cli-sdk-unit": {"run": "pytest tests/unit/cli"}},
        "gate_sets": {"cli": {"commands": ["cli-sdk-unit"]}},
        "selection": [{"paths": ["tests/unit/cli/**"], "include": ["cli"]}],
        "schedule": [
            {"after_phase": "implement", "gate_sets": ["cli"], "policy": "require"},
            {"before_delivery": True, "gate_sets": ["cli"], "policy": "require"},
        ],
    }))
    assert contract is not None
    return contract


def _run(tmp_path: Path, contract: VerificationContract, *, resume: bool = False):
    state = SimpleNamespace(output_dir=tmp_path, extras={"verification_contract": contract})
    return SimpleNamespace(state=state, checkpoint_resume=resume)


def test_fresh_snapshot_precedes_first_selection(tmp_path: Path) -> None:
    run = _run(tmp_path, _contract())
    initialize(run.state)
    assert load_ledger(tmp_path).trail == ()
    select_epoch(run, run.state.extras["verification_contract"], epoch="after_phase:implement", context=SelectionContext())
    assert any(event.kind == "selection" for event in load_ledger(tmp_path).trail)


def test_hook_selection_and_execution_are_full_identity_events(tmp_path: Path) -> None:
    run = _run(tmp_path, _contract())
    initialize(run.state)
    plan = select_epoch(run, run.state.extras["verification_contract"], epoch="after_phase:implement", context=SelectionContext())
    record_execution(run, plan.entries[0], passed=True)
    ledger = load_ledger(tmp_path)
    assert {event.identity for event in ledger.trail} == {("check", "after_phase", "implement")}
    assert [event.kind for event in ledger.trail] == ["selection", "execution"]


def test_fresh_epoch_writes_before_publishing_authoritative_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(tmp_path, _contract())
    initialize(run.state)
    from pipeline.project import verification_ledger_runtime as runtime

    original_write = runtime.write_ledger

    def assert_not_published(*args, **kwargs):
        assert "verification_gate_routing_plans" not in run.state.extras
        return original_write(*args, **kwargs)

    monkeypatch.setattr(runtime, "write_ledger", assert_not_published)
    plan = select_epoch(
        run, run.state.extras["verification_contract"],
        epoch="before_delivery:", context=SelectionContext(),
    )

    assert run.state.extras["verification_gate_routing_plans"]["before_delivery:"] is plan


def test_repeated_epoch_replays_recorded_plan_without_rebuilding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(tmp_path, _contract())
    initialize(run.state)
    first = select_epoch(
        run, run.state.extras["verification_contract"],
        epoch="after_phase:implement", context=SelectionContext(),
    )
    monkeypatch.setattr(
        "pipeline.project.verification_ledger_runtime.build_scheduled_gate_plan",
        lambda *_: pytest.fail("rebuilt recorded epoch"),
    )

    replayed = select_epoch(
        run, run.state.extras["verification_contract"],
        epoch="after_phase:implement", context=SelectionContext(touched_paths=("later.py",)),
    )

    assert [entry.command for entry in replayed.entries] == [entry.command for entry in first.entries]
    assert run.state.extras["verification_gate_routing_plans"]["after_phase:implement"] is replayed
    assert len(load_ledger(tmp_path).trail) == 1


def test_path_selection_is_stable_across_late_context_and_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _path_contract()
    first = _run(tmp_path, contract)
    initialize(first.state)
    selected = select_epoch(
        first, contract, epoch="before_delivery:",
        context=SelectionContext(touched_paths=("tests/unit/cli/test_command.py",)),
    )
    assert [entry.command for entry in selected.entries] == ["cli-sdk-unit"]

    monkeypatch.setattr(
        "pipeline.project.verification_ledger_runtime.build_scheduled_gate_plan",
        lambda *_: pytest.fail("rebuilt recorded path selection"),
    )
    assert [entry.command for entry in select_epoch(
        first, contract, epoch="before_delivery:", context=SelectionContext(),
    ).entries] == ["cli-sdk-unit"]

    resumed = _run(tmp_path, contract, resume=True)
    initialize(resumed.state, resume=True)
    assert [entry.command for entry in select_epoch(
        resumed, contract, epoch="before_delivery:", context=SelectionContext(touched_paths=("unrelated.py",)),
    ).entries] == ["cli-sdk-unit"]
    assert len(load_ledger(tmp_path).trail) == 1


def test_delivery_epoch_replays_recorded_phase_identity_without_live_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume keeps selected ``after_phase:implement`` in delivery coverage."""
    contract = _delivery_path_contract()
    first = _run(tmp_path, contract)
    initialize(first.state)
    select_epoch(
        first, contract, epoch="after_phase:implement",
        context=SelectionContext(touched_paths=("tests/unit/cli/test_orcho.py",)),
    )
    fresh = select_epoch(
        first, contract, epoch="before_delivery:", context=SelectionContext(),
    )
    expected = [("cli-sdk-unit", "after_phase", "implement")]
    assert [(entry.command, entry.hook, entry.phase) for entry in fresh.entries] == expected

    resumed = _run(tmp_path, contract, resume=True)
    monkeypatch.setattr(
        "pipeline.project.verification_ledger_runtime.build_scheduled_gate_plan",
        lambda *_: pytest.fail("resume rebuilt a recorded delivery plan"),
    )
    replayed = select_epoch(
        resumed, contract, epoch="before_delivery:",
        context=SelectionContext(touched_paths=("unrelated.py",)),
    )

    assert [(entry.command, entry.hook, entry.phase) for entry in replayed.entries] == expected


def test_resume_replays_epoch_without_resolving_new_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _run(tmp_path, _contract())
    initialize(first.state)
    select_epoch(first, first.state.extras["verification_contract"], epoch="after_phase:implement", context=SelectionContext())
    resumed = _run(tmp_path, _contract(), resume=True)
    initialize(resumed.state, resume=True)
    monkeypatch.setattr("pipeline.project.verification_ledger_runtime.build_scheduled_gate_plan", lambda *_: pytest.fail("resolved historical plan"))
    assert select_epoch(resumed, resumed.state.extras["verification_contract"], epoch="after_phase:implement", context=SelectionContext()).entries


def test_resume_resolves_new_epoch_from_snapshot_not_plugin_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _run(tmp_path, _contract())
    initialize(first.state)
    resumed = _run(tmp_path, _contract(), resume=True)
    initialize(resumed.state, resume=True)
    monkeypatch.setattr(
        "pipeline.project.verification_ledger_runtime.build_scheduled_gate_plan",
        lambda *_: pytest.fail("resume resolved snapshot through plugin rules"),
    )

    plan = select_epoch(
        resumed, resumed.state.extras["verification_contract"],
        epoch="after_phase:implement", context=SelectionContext(),
    )

    assert [(entry.command, entry.hook, entry.phase) for entry in plan.entries] == [
        ("check", "after_phase", "implement"),
    ]


def test_resume_snapshot_keeps_task_kind_rule(tmp_path: Path) -> None:
    contract = VerificationContract.from_plugin(PluginConfig(verification={
        "commands": {"api": {"run": "pytest"}},
        "gate_sets": {"api": {"commands": ["api"]}},
        "selection": [{"task_kind": "api", "include": ["api"]}],
        "schedule": [{"after_phase": "implement", "gate_sets": ["api"], "policy": "require"}],
    }))
    assert contract is not None
    first = _run(tmp_path, contract)
    initialize(first.state)
    resumed = _run(tmp_path, contract, resume=True)

    selected = select_epoch(
        resumed, contract, epoch="after_phase:implement",
        context=SelectionContext(task_kind="api"),
    )
    assert [entry.command for entry in selected.entries] == ["api"]


def test_resume_mechanics_drift_fails_closed(tmp_path: Path) -> None:
    first = _run(tmp_path, _contract())
    initialize(first.state)
    with pytest.raises(ResumeVerificationLedgerError, match="no longer defines"):
        initialize(_run(tmp_path, _contract("other"), resume=True).state, resume=True)


def test_resume_policy_drift_fails_closed(tmp_path: Path) -> None:
    first = _run(tmp_path, _contract())
    initialize(first.state)
    drifted = VerificationContract.from_plugin(PluginConfig(verification={
        "commands": {"check": {"run": "pytest"}},
        "gate_sets": {"core": {"commands": ["check"]}},
        "selection": [{"always": ["core"]}],
        "schedule": [{"after_phase": "implement", "gate_sets": ["core"], "policy": "warn"}],
    }))
    assert drifted is not None
    with pytest.raises(ResumeVerificationLedgerError, match="policy drift"):
        initialize(_run(tmp_path, drifted, resume=True).state, resume=True)


def test_finalization_closes_artifact(tmp_path: Path) -> None:
    run = _run(tmp_path, _contract())
    initialize(run.state)
    plan = select_epoch(run, run.state.extras["verification_contract"], epoch="after_phase:implement", context=SelectionContext())
    record_execution(run, plan.entries[0], passed=True)
    assert finalize(run).finalized


def test_runtime_ledger_preserves_execution_facts_across_sdk_evidence_and_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All read surfaces consume the finalized runtime artifact unchanged."""
    from pipeline.evidence import collect_evidence
    from pipeline.project.verification_timeline import (
        build_verification_timeline,
        render_verification_gate_done_block,
    )
    from sdk.verification_timeline import get_verification_timeline

    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "20260101_000000"
    run = _run(run_dir, _automatic_and_manual_contract())
    initialize(run.state)
    plan = select_epoch(
        run, run.state.extras["verification_contract"],
        epoch="before_delivery:", context=SelectionContext(),
    )
    record_execution(run, plan.entries[0], passed=True)

    def identity_facts(rows):
        return [
            (
                row.gate if hasattr(row, "gate") else row.command,
                row.hook,
                row.phase,
                row.executor,
                row.trigger,
                row.consequence,
            )
            for row in rows
        ]

    expected_identity_facts = [
        ("cli-sdk-unit", "before_delivery", "", "engine", "pre_final", "required_action"),
        ("manual", "manual_only", "", "operator", "operator", "none"),
        ("suggested", "manual_only", "", "operator", "operator", "none"),
    ]
    assert identity_facts(load_ledger(run_dir).rows) == expected_identity_facts

    finalized = finalize(run)
    assert finalized is not None
    run_dir.joinpath("meta.json").write_text(
        json.dumps({"run_id": "20260101_000000", "status": "done"}),
        encoding="utf-8",
    )

    def facts(rows):
        return [
            (
                row.gate if hasattr(row, "gate") else row.command,
                row.hook,
                row.phase,
                row.executor,
                row.trigger,
                row.consequence,
                row.disposition,
            )
            for row in rows
        ]

    expected = [
        ("cli-sdk-unit", "before_delivery", "", "engine", "pre_final", "required_action", "executed_pass"),
        ("manual", "manual_only", "", "operator", "operator", "none", "manual_available"),
        ("suggested", "manual_only", "", "operator", "operator", "none", "suggested"),
    ]
    assert facts(finalized.rows) == expected

    monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))
    assert facts(get_verification_timeline(run_id="20260101_000000").rows) == expected

    evidence_rows = collect_evidence(run_dir)["scheduled_gate_ledger"]["rows"]
    assert [
        (row["gate"], row["hook"], row["phase"], row["executor"], row["trigger"], row["consequence"], row["disposition"])
        for row in evidence_rows
    ] == expected

    timeline = build_verification_timeline(run_dir=run_dir, extras={})
    assert timeline is not None
    assert facts(timeline.ledger_rows) == expected
    done = "\n".join(render_verification_gate_done_block(timeline))
    assert "cli-sdk-unit: selection=always trigger=pre_final executor=engine" in done
    assert "manual: selection=operator trigger=operator executor=operator" in done
    assert "suggested: selection=operator trigger=operator executor=operator" in done
