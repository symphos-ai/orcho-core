# SPDX-License-Identifier: Apache-2.0
"""Tests for the SDK's artifact-only scheduled-gate ledger projection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdk import to_jsonable
from sdk.errors import RunNotFound
from sdk.verification_timeline import (
    ReceiptEvidence,
    VerificationTimelineProjection,
    get_verification_timeline,
)


@pytest.fixture
def runs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))
    return runs


def _run_dir(runs_dir: Path, *, project: Path) -> Path:
    run_dir = runs_dir / "20260101_000000"
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(
        json.dumps({"task": "t", "status": "done", "project": str(project)}),
        encoding="utf-8",
    )
    return run_dir


def _write_ledger(run_dir: Path) -> None:
    from pipeline.verification_ledger import GateLedgerRow, GateTrailEvent
    from pipeline.verification_ledger_store import ScheduledGateLedger, write_ledger

    rows = (
        GateLedgerRow(
            gate="check", hook="after_phase", phase="implement",
            timing="after_implement", run_mode="auto", gate_sets=("core",),
            condition="always", selected=True, execution_policy="require",
            consequence="required_action", executor="engine", trigger="after_phase",
            disposition="executed_pass",
        ),
        GateLedgerRow(
            gate="check", hook="manual_only", phase="",
            timing="operator", run_mode="manual", gate_sets=("optional",),
            condition="operator", selected=True, execution_policy="manual",
            consequence="none", executor="operator", trigger="manual_only",
            disposition="manual_available",
        ),
        GateLedgerRow(
            gate="paths", hook="before_delivery", phase="",
            timing="delivery", run_mode="auto", gate_sets=(), condition="on_path",
            condition_paths=("src/**",), selected=False,
            execution_policy="require", consequence="required_action",
            selection_reason="paths", executor="engine", trigger="before_delivery",
            disposition="not_selected",
        ),
    )
    trail = (
        GateTrailEvent("check", "after_phase", "implement", "selection", "selected"),
        GateTrailEvent("check", "after_phase", "implement", "execution", "pass"),
        GateTrailEvent(
            "check", "after_phase", "implement", "execution", "pass",
            receipt_evidence="verification_command_receipts/check-rerun.json", rerun=True,
        ),
        GateTrailEvent("check", "manual_only", "", "selection", "selected"),
        GateTrailEvent("paths", "before_delivery", "", "selection", "not_selected", "paths"),
    )
    write_ledger(run_dir, ScheduledGateLedger(rows, trail, finalized=True))


def test_reads_exact_durable_rows_and_identity_scoped_events(
    tmp_path: Path, runs_dir: Path,
) -> None:
    run_dir = _run_dir(runs_dir, project=tmp_path / "project")
    _write_ledger(run_dir)

    projection = get_verification_timeline(run_id="20260101_000000")

    assert projection.finalized is True
    assert projection.schema_version == "1"
    assert [(row.command, row.hook, row.phase) for row in projection.rows] == [
        ("check", "after_phase", "implement"),
        ("check", "manual_only", ""),
        ("paths", "before_delivery", ""),
    ]
    assert [row.disposition for row in projection.rows] == [
        "executed_pass", "manual_available", "not_selected",
    ]
    assert projection.rows[0].execution_policy == "require"
    assert projection.rows[0].consequence == "required_action"
    assert projection.rows[1].executor == "operator"
    assert projection.rows[2].selection_reason == "paths"
    assert [(event.command, event.hook, event.phase, event.kind) for event in projection.events] == [
        ("check", "after_phase", "implement", "selection"),
        ("check", "after_phase", "implement", "execution"),
        ("check", "after_phase", "implement", "execution"),
        ("check", "manual_only", "", "selection"),
        ("paths", "before_delivery", "", "selection"),
    ]
    rerun = projection.events[2]
    assert rerun.receipt_evidence == ReceiptEvidence(
        path="verification_command_receipts/check-rerun.json", rerun=True,
    )


def test_completed_projection_is_stable_after_plugin_is_deleted(
    tmp_path: Path, runs_dir: Path,
) -> None:
    project = tmp_path / "project"
    plugin = project / ".orcho" / "multiagent" / "plugin.py"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("PLUGIN = {'verification': {}}\n", encoding="utf-8")
    run_dir = _run_dir(runs_dir, project=project)
    _write_ledger(run_dir)

    before = to_jsonable(get_verification_timeline(run_id="20260101_000000"))
    plugin.unlink()
    after = to_jsonable(get_verification_timeline(run_id="20260101_000000"))

    assert after == before


def test_missing_artifact_is_explicitly_empty_without_plugin_reconstruction(
    tmp_path: Path, runs_dir: Path,
) -> None:
    _run_dir(runs_dir, project=tmp_path / "project-without-plugin")

    projection = get_verification_timeline(run_id="20260101_000000")

    assert projection == VerificationTimelineProjection(
        schema_version="1", run_id="20260101_000000",
        project=str(tmp_path / "project-without-plugin"),
    )


def test_projection_is_jsonable_and_old_wire_is_not_exported(
    tmp_path: Path, runs_dir: Path,
) -> None:
    run_dir = _run_dir(runs_dir, project=tmp_path / "project")
    _write_ledger(run_dir)

    payload = to_jsonable(get_verification_timeline(run_id="20260101_000000"))

    assert json.loads(json.dumps(payload))["rows"][0]["disposition"] == "executed_pass"
    assert "scheduled_trail_available" not in payload
    assert "scheduled_trail_gap" not in payload
    assert not hasattr(__import__("sdk.verification_timeline", fromlist=["*"]), "GATE_STATUSES")
    assert not hasattr(__import__("sdk.verification_timeline", fromlist=["*"]), "AutorunEvent")
    assert ReceiptEvidence(path="receipt.json").path == "receipt.json"


def test_run_not_found_raises(runs_dir: Path) -> None:
    with pytest.raises(RunNotFound):
        get_verification_timeline(run_id="does_not_exist")
