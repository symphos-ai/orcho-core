# SPDX-License-Identifier: Apache-2.0
"""Durable scheduled-gate ledger artifact tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.verification_ledger import GateLedgerRow, GateTrailEvent
from pipeline.verification_ledger_store import (
    LedgerStoreError,
    ScheduledGateLedger,
    load_ledger,
    update_ledger,
    write_ledger,
)


def _row(command: str, hook: str, phase: str) -> GateLedgerRow:
    return GateLedgerRow(
        gate=command, hook=hook, phase=phase, timing=hook, run_mode="auto",
        gate_sets=(), condition="always", selected=True, execution_policy="require",
    )


def test_round_trip_preserves_order_and_exact_identities(tmp_path: Path) -> None:
    ledger = ScheduledGateLedger((_row("same", "after_phase", "implement"), _row("same", "before_phase", "plan")))
    write_ledger(tmp_path, ledger)
    loaded = load_ledger(tmp_path)
    assert [row.identity for row in loaded.rows] == [row.identity for row in ledger.rows]


def test_atomic_write_failure_keeps_previous_artifact_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = ScheduledGateLedger((_row("one", "after_phase", "implement"),))
    write_ledger(tmp_path, original)
    monkeypatch.setattr("pipeline.verification_ledger_store.os.replace", lambda *_: (_ for _ in ()).throw(OSError("no replace")))
    with pytest.raises(OSError, match="no replace"):
        write_ledger(tmp_path, ScheduledGateLedger((_row("two", "after_phase", "implement"),)))
    assert load_ledger(tmp_path) == original


def test_event_is_idempotent_and_finalized_semantic_update_is_rejected(tmp_path: Path) -> None:
    ledger = ScheduledGateLedger((_row("one", "after_phase", "implement"),))
    write_ledger(tmp_path, ledger)
    event = GateTrailEvent("one", "after_phase", "implement", "execution", "pass")
    once = update_ledger(tmp_path, event)
    twice = update_ledger(tmp_path, event)
    assert once.trail == twice.trail == (event,)
    update_ledger(tmp_path, finalize=True)
    with pytest.raises(LedgerStoreError, match="finalized"):
        update_ledger(tmp_path, GateTrailEvent("one", "after_phase", "implement", "receipt", "failed"))


def test_rerun_execution_round_trip_is_distinct_and_idempotent(tmp_path: Path) -> None:
    ledger = ScheduledGateLedger((_row("one", "after_phase", "implement"),))
    write_ledger(tmp_path, ledger)
    original = GateTrailEvent(
        "one", "after_phase", "implement", "execution", "fail",
        receipt_evidence="receipts/original.json",
    )
    rerun = GateTrailEvent(
        "one", "after_phase", "implement", "execution", "pass",
        receipt_evidence="receipts/rerun.json", rerun=True,
    )

    update_ledger(tmp_path, original)
    once = update_ledger(tmp_path, rerun)
    twice = update_ledger(tmp_path, rerun)

    assert twice == once
    assert twice.trail == (original, rerun)
    assert load_ledger(tmp_path).trail[1].rerun is True


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (("declared", "true", "declared must be bool"), ("receipt_evidence", 1, "receipt_evidence must be a string or null")),
)
def test_strict_loader_rejects_wrong_optional_and_boolean_wire_types(
    tmp_path: Path, field: str, value: object, message: str,
) -> None:
    ledger = ScheduledGateLedger((_row("one", "after_phase", "implement"),))
    write_ledger(tmp_path, ledger)
    path = tmp_path / "scheduled_gate_ledger.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["rows"][0][field] = value
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(LedgerStoreError, match=message):
        load_ledger(tmp_path)


def test_strict_loader_rejects_non_boolean_rerun(tmp_path: Path) -> None:
    ledger = ScheduledGateLedger((_row("one", "after_phase", "implement"),), (
        GateTrailEvent("one", "after_phase", "implement", "execution", "pass"),
    ))
    write_ledger(tmp_path, ledger)
    path = tmp_path / "scheduled_gate_ledger.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["trail"][0]["rerun"] = "true"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(LedgerStoreError, match="rerun must be bool"):
        load_ledger(tmp_path)


def test_store_rejects_invalid_finalized_snapshot_before_writing(tmp_path: Path) -> None:
    with pytest.raises(LedgerStoreError, match="open or invalid"):
        write_ledger(tmp_path, ScheduledGateLedger((_row("one", "after_phase", "implement"),), finalized=True))


def test_finalize_closes_each_duplicate_command_identity_independently(tmp_path: Path) -> None:
    rows = (_row("same", "after_phase", "implement"), _row("same", "before_phase", "plan"))
    write_ledger(tmp_path, ScheduledGateLedger(rows))
    update_ledger(tmp_path, GateTrailEvent("same", "after_phase", "implement", "execution", "pass"), finalize=True)
    closed = load_ledger(tmp_path)
    assert [row.disposition for row in closed.rows] == ["executed_pass", "residual_missing"]
