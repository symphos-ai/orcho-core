# SPDX-License-Identifier: Apache-2.0
"""Engine-selected proof for criteria with no planner-authored gate identities."""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.contracts.criteria import AcceptanceCriterion, GateRef, validate_acceptance_criteria
from pipeline.criterion_evidence import criterion_matrix_for_run
from pipeline.criterion_gate_refs import plan_gate_ref_problems, validate_criterion_gate_refs
from pipeline.criterion_matrix import build_criterion_matrix
from pipeline.evidence.collector import collect_evidence
from pipeline.evidence.schema import EvidenceSchemaError, validate_bundle
from pipeline.plan_artifacts import write_parsed_plan_artifact
from pipeline.plan_parser import parse_plan
from pipeline.verification_ledger import GateLedgerRow, GateTrailEvent
from pipeline.verification_ledger_store import ScheduledGateLedger, load_ledger, write_ledger

UNIT = GateRef("unit", "after_phase", "implement")
CRITERION = AcceptanceCriterion("C1", "behavior works", "executable")


@pytest.mark.parametrize("extra", [{}, {"gate_refs": []}])
def test_omission_and_empty_refs_are_valid_without_a_planning_ledger(extra):
    criteria = validate_acceptance_criteria([
        {"id": "C1", "intent": "behavior works", "verify": "executable", **extra},
    ])
    assert criteria[0].gate_refs == ()
    validate_criterion_gate_refs(criteria, None)
    assert plan_gate_ref_problems(SimpleNamespace(acceptance_criteria=criteria), None) == []


@pytest.mark.parametrize("state", ["failed", "stale", "missing", "proven"])
def test_implied_binding_uses_the_same_proof_rules_as_explicit_refs(state):
    kwargs = {"gate_states": {UNIT.identity: state},
              "gate_proof_refs": {UNIT.identity: "receipt-unit"}}
    implicit = build_criterion_matrix([CRITERION], selected_gate_refs=[UNIT], **kwargs)
    explicit = build_criterion_matrix([replace(CRITERION, gate_refs=(UNIT,))], **kwargs)
    assert implicit.rows[0].state == explicit.rows[0].state == state
    assert implicit.rows[0].proof_refs == explicit.rows[0].proof_refs
    assert implicit.to_dict()["rows"][0]["method"] == {
        "kind": "gates", "gate_refs": [UNIT.to_dict()], "implied": True,
    }


@pytest.mark.parametrize("pending, expected", [(False, "missing"), (True, "pending")])
def test_no_selected_gates_never_proves_a_criterion(pending, expected):
    row = build_criterion_matrix([CRITERION], selection_pending=pending).rows[0]
    assert row.state == expected
    assert row.blocking


def test_unresolved_selection_blocks_a_pass_without_affecting_explicit_refs():
    kwargs = {"selected_gate_refs": [UNIT], "selection_pending": True,
              "gate_states": {UNIT.identity: "proven"},
              "gate_proof_refs": {UNIT.identity: "receipt-unit"}}
    assert build_criterion_matrix([CRITERION], **kwargs).rows[0].state == "pending"
    explicit = replace(CRITERION, gate_refs=(UNIT,))
    assert build_criterion_matrix([explicit], **kwargs).rows[0].state == "proven"


def test_a_selected_pass_without_a_receipt_is_missing():
    row = build_criterion_matrix(
        [CRITERION], selected_gate_refs=[UNIT], gate_states={UNIT.identity: "proven"},
    ).rows[0]
    assert row.state == "missing"
    assert row.blocking


def _run(tmp_path):
    run = tmp_path / "20260101_000000"
    run.mkdir()
    (run / "meta.json").write_text(json.dumps({"run_id": run.name, "status": "running"}))
    plan = {"short_summary": "s", "planning_context": "p",
            "acceptance_criteria": [CRITERION.to_dict()],
            "tasks": [{"id": "t1", "goal": "g", "acceptance_refs": ["C1"]}]}
    (run / "events.jsonl").write_text(json.dumps({
        "seq": 1, "ts": "2026-01-01T00:00:00", "kind": "plan.parsed",
        "phase": "plan", "payload": {
            "source": "json", "short_summary": "s", "planning_context": "p",
            "subtask_count": 1, "has_contract": True, "goal": "",
            "acceptance_criteria": plan["acceptance_criteria"],
            "subtasks": plan["tasks"],
        },
    }) + "\n")
    write_parsed_plan_artifact(run, parse_plan(json.dumps(plan)), attempt=1)
    row = GateLedgerRow(
        gate="unit", hook="after_phase", phase="implement", timing="after implement",
        run_mode="auto", gate_sets=("unit",), condition="always", selected=True,
    )
    write_ledger(run, ScheduledGateLedger(rows=(
        row, replace(row, gate="unselected", selected=False),
    ), trail=(GateTrailEvent(
        "unit", "after_phase", "implement", "execution", "pass",
        receipt_evidence="verification_command_receipts/unit.json",
    ),)))
    return run


def test_durable_binding_matches_live_readiness_final_evidence_and_sdk(tmp_path):
    from pipeline.verification_readiness import criterion_release_gaps
    from sdk.criterion_matrix import get_criterion_matrix

    run = _run(tmp_path)
    live = criterion_matrix_for_run(run).to_dict()
    assert live["rows"][0]["state"] == "proven"
    assert live["rows"][0]["method"]["gate_refs"] == [UNIT.to_dict()]
    assert not criterion_release_gaps(run)
    write_ledger(run, load_ledger(run).finalize())
    bundle = collect_evidence(run)
    validate_bundle(bundle)
    assert bundle["criterion_matrix"] == live
    assert get_criterion_matrix(run.name, runs_dir=tmp_path, cwd=None) == live


def test_implied_method_flag_is_strict(tmp_path):
    run = _run(tmp_path)
    bundle = collect_evidence(run)
    bundle["criterion_matrix"]["rows"][0]["method"]["implied"] = False
    with pytest.raises(EvidenceSchemaError, match="implied must be true"):
        validate_bundle(bundle)


@pytest.mark.parametrize("disposition", ["suggested", "manual_available"])
@pytest.mark.parametrize("selected", [True, None])
def test_unexecuted_recommendations_do_not_become_proof_obligations(
    tmp_path, disposition, selected,
):
    run = _run(tmp_path)
    ledger = load_ledger(run).finalize()
    recommendation = replace(
        ledger.rows[0], gate="optional", hook="manual_only", phase="",
        disposition=disposition, receipt_evidence=None, selected=selected,
    )
    write_ledger(run, replace(ledger, rows=(*ledger.rows, recommendation)))
    matrix = criterion_matrix_for_run(run)
    assert matrix.rows[0].state == "proven"
    assert matrix.rows[0].method["gate_refs"] == [UNIT.to_dict()]


def test_an_executed_recommendation_contributes_its_failure(tmp_path):
    run = _run(tmp_path)
    ledger = load_ledger(run).finalize()
    executed = replace(
        ledger.rows[0], gate="optional", hook="manual_only", phase="",
        disposition="executed_fail", receipt_evidence="optional.json",
    )
    write_ledger(run, replace(ledger, rows=(*ledger.rows, executed)))
    assert criterion_matrix_for_run(run).rows[0].state == "failed"


@pytest.mark.parametrize("pending", [False, True])
def test_unbound_matrix_round_trips_through_evidence(tmp_path, pending):
    run = _run(tmp_path)
    ledger = load_ledger(run)
    rows = tuple(replace(row, selected=None if pending else False) for row in ledger.rows)
    write_ledger(run, replace(ledger, rows=rows, trail=()))
    bundle = collect_evidence(run)
    validate_bundle(bundle)
    row = bundle["criterion_matrix"]["rows"][0]
    assert row["state"] == ("pending" if pending else "missing")
    assert row["blocking"]
    assert row["method"] == {"kind": "gates", "gate_refs": [], "implied": True}


def test_plan_without_refs_requires_engine_provenance_in_evidence(tmp_path):
    run = _run(tmp_path)
    bundle = collect_evidence(run)
    bundle["criterion_matrix"]["rows"][0]["method"].pop("implied")
    with pytest.raises(EvidenceSchemaError, match="does not project the accepted plan"):
        validate_bundle(bundle)


def test_an_unbound_implied_row_says_why_and_what_to_do():
    # ADR 0191 (E2): a bare ``missing:`` told the operator nothing.
    row = build_criterion_matrix([CRITERION]).rows[0]
    assert row.state == "missing"
    assert row.blocking
    assert row.reason.startswith("missing: no official gate is bound")
    assert "reclassify" in row.reason
