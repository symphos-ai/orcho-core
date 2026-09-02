# SPDX-License-Identifier: Apache-2.0
"""T5 — evidence JSON / Markdown / SDK projection of the criterion matrix.

Covers C8, C10, the F1 mixed-state conformance revision, and the QA-risk
durable round trip of typed claims/findings after resume.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.contracts.criteria import criteria_to_wire
from pipeline.criterion_claims import record_criterion_claim
from pipeline.criterion_decisions import record_human_decision
from pipeline.criterion_evidence import criterion_matrix_for_run
from pipeline.criterion_matrix import CRITERION_STATE_ORDER
from pipeline.evidence.collector import collect_evidence
from pipeline.evidence.render_md import render_evidence_md
from pipeline.evidence.schema import EvidenceSchemaError, validate_bundle
from pipeline.plan_artifacts import write_parsed_plan_artifact
from pipeline.plan_parser import parse_plan
from pipeline.verification_ledger import GateLedgerRow
from pipeline.verification_ledger_store import ScheduledGateLedger, write_ledger

RUN_ID = "20260101_000000"

_UNIT = {"command": "unit", "hook": "after_phase", "phase": "implement"}
_LINT = {"command": "lint", "hook": "after_phase", "phase": "implement"}

_PLAN = {
    "short_summary": "s",
    "planning_context": "p",
    "acceptance_criteria": [
        {"id": "C1", "intent": "regression tested", "verify": "executable",
         "gate_refs": [dict(_UNIT)]},
        {"id": "C2", "intent": "hygiene proven", "verify": "executable",
         "gate_refs": [dict(_LINT)]},
        {"id": "C3", "intent": "docs read coherently", "verify": "agent_assertion"},
        {"id": "C4", "intent": "operator accepts migration", "verify": "human",
         "human_instructions": "Migrate once and record the outcome."},
        {"id": "C5", "intent": "operator accepts rollback", "verify": "human",
         "human_instructions": "Roll back once and record the outcome."},
    ],
    "tasks": [
        {"id": "t1", "goal": "g1", "acceptance_refs": ["C1"]},
        {"id": "t2", "goal": "g2", "acceptance_refs": ["C2"]},
    ],
}


def _row(command: str, disposition: str, receipt: str | None) -> GateLedgerRow:
    return GateLedgerRow(
        gate=command, hook="after_phase", phase="implement",
        timing="after implement", run_mode="auto", gate_sets=("smoke",),
        condition="always", disposition=disposition, receipt_evidence=receipt,
    )


@pytest.fixture()
def run_dir(tmp_path):
    d = tmp_path / RUN_ID
    d.mkdir()
    (d / "meta.json").write_text(
        json.dumps({"run_id": RUN_ID, "status": "success"}), encoding="utf-8",
    )
    (d / "events.jsonl").write_text("", encoding="utf-8")
    write_parsed_plan_artifact(d, parse_plan(json.dumps(_PLAN)), attempt=1)
    write_ledger(d, ScheduledGateLedger(rows=(
        _row("unit", "executed_pass", "verification_command_receipts/unit.json"),
        _row("lint", "executed_fail", "verification_command_receipts/lint.json"),
    )))
    record_criterion_claim(
        d, run_id=RUN_ID, criterion_id="C3", actor="reviewer",
        statement="The authoring workflow reads coherently.",
    )
    record_human_decision(
        d, run_id=RUN_ID, criterion_id="C4", decision="accept",
    )
    return d


class TestMixedStateConformance:
    def test_all_three_classes_reach_the_durable_bundle(self, run_dir) -> None:
        bundle = collect_evidence(run_dir)
        matrix = bundle["criterion_matrix"]
        assert [r["state"] for r in matrix["rows"]] == [
            "proven", "failed", "advisory", "accepted", "pending",
        ]
        assert matrix["summary"]["blocking_open"] == 2
        assert matrix["summary"]["ready"] is False
        assert matrix["summary"]["pending_human_ids"] == ["C5"]

    def test_counts_by_state_key_order_matches_the_canonical_constant(
        self, run_dir,
    ) -> None:
        counts = collect_evidence(run_dir)["criterion_matrix"]["summary"][
            "counts_by_state"
        ]
        assert list(counts) == [s for s in CRITERION_STATE_ORDER if s in counts]
        assert list(counts) == [
            "proven", "failed", "advisory", "accepted", "pending",
        ]

    def test_durable_evidence_and_sdk_canonical_json_are_byte_equivalent(
        self, run_dir, tmp_path,
    ) -> None:
        from sdk.criterion_matrix import canonical_criterion_json

        durable = collect_evidence(run_dir)["criterion_matrix"]
        via_reducer = criterion_matrix_for_run(run_dir).to_dict()
        assert canonical_criterion_json(durable) == canonical_criterion_json(
            via_reducer,
        )

    def test_sdk_versioned_mixed_state_example_matches_the_reducer(self) -> None:
        from sdk.criterion_examples import criterion_matrix_example
        from sdk.criterion_matrix import canonical_criterion_json

        example = criterion_matrix_example("mixed_state")
        counts = example["summary"]["counts_by_state"]
        assert list(counts) == [s for s in CRITERION_STATE_ORDER if s in counts]
        assert set(counts) >= {"proven", "failed", "advisory", "accepted", "pending"}
        assert canonical_criterion_json(example).startswith('{"rows":[')

    def test_proof_refs_cite_receipts_claims_and_the_decision_head(
        self, run_dir,
    ) -> None:
        rows = collect_evidence(run_dir)["criterion_matrix"]["rows"]
        by_id = {r["criterion_id"]: r for r in rows}
        assert by_id["C1"]["proof_refs"] == [
            {"kind": "receipt",
             "id": "verification_command_receipts/unit.json"},
        ]
        assert by_id["C3"]["proof_refs"] == [
            {"kind": "claim", "id": "claim-C3-1"},
        ]
        assert by_id["C4"]["proof_refs"] == [
            {"kind": "human_decision", "id": "hd-C4-1"},
        ]
        assert by_id["C5"]["proof_refs"] == []


class TestSchemaAndRendering:
    def test_bundle_with_a_matrix_validates(self, run_dir) -> None:
        validate_bundle(collect_evidence(run_dir))

    def test_out_of_order_counts_by_state_fails_validation(self, run_dir) -> None:
        bundle = collect_evidence(run_dir)
        counts = bundle["criterion_matrix"]["summary"]["counts_by_state"]
        bundle["criterion_matrix"]["summary"]["counts_by_state"] = dict(
            reversed(list(counts.items())),
        )
        with pytest.raises(EvidenceSchemaError, match="canonical state order"):
            validate_bundle(bundle)

    def test_null_matrix_is_never_valid(self, run_dir) -> None:
        bundle = collect_evidence(run_dir)
        bundle["criterion_matrix"] = None
        with pytest.raises(EvidenceSchemaError, match="null is never written"):
            validate_bundle(bundle)

    def test_markdown_renders_the_user_facing_table(self, run_dir) -> None:
        md = render_evidence_md(collect_evidence(run_dir))
        assert "## Criterion matrix" in md
        assert "| Criterion | Executor | Verification | Proof | State |" in md
        assert "| C1 | t1 | unit @ after_phase implement |" in md
        assert "| C3 | reviewer | inspection | claim:claim-C3-1 | advisory |" in md
        assert "| C5 | human | manual | - | pending |" in md
        assert "**Ready:** no" in md

    def test_markdown_omits_the_section_for_a_legacy_bundle(self, run_dir) -> None:
        bundle = collect_evidence(run_dir)
        bundle.pop("criterion_matrix")
        assert "## Criterion matrix" not in render_evidence_md(bundle)


class TestAbsentVersusEmpty:
    def test_a_run_with_no_plan_artifact_omits_the_key(self, tmp_path) -> None:
        d = tmp_path / RUN_ID
        d.mkdir()
        (d / "meta.json").write_text("{}", encoding="utf-8")
        (d / "events.jsonl").write_text("", encoding="utf-8")
        bundle = collect_evidence(d)
        assert "criterion_matrix" not in bundle
        validate_bundle(bundle)

    def test_a_new_plan_with_no_criteria_writes_the_explicit_empty_matrix(
        self, tmp_path,
    ) -> None:
        d = tmp_path / RUN_ID
        d.mkdir()
        (d / "meta.json").write_text("{}", encoding="utf-8")
        # A real plan projection that declares no criteria: the explicit empty
        # matrix is authoritative here, not an absent projection.
        (d / "events.jsonl").write_text(
            json.dumps({
                "seq": 1, "ts": "2026-01-01T00:00:00", "kind": "plan.parsed",
                "phase": "PLAN",
                "payload": {
                    "source": "json", "short_summary": "s",
                    "planning_context": "p", "subtask_count": 1,
                    "has_contract": False, "goal": "",
                    "acceptance_criteria": [], "owned_files": [],
                    "commands_to_run": [], "risks": [], "review_focus": [],
                    "mcp_context": [], "subtasks": [],
                },
            }) + "\n",
            encoding="utf-8",
        )
        plan = parse_plan(json.dumps({
            "short_summary": "s", "planning_context": "p",
            "tasks": [{"id": "t1", "goal": "g"}],
        }))
        write_parsed_plan_artifact(d, plan, attempt=1)
        bundle = collect_evidence(d)
        assert bundle["plan"]["source"] == "json"
        assert bundle["plan"]["acceptance_criteria"] == []
        assert bundle["criterion_matrix"] == {
            "rows": [],
            "summary": {
                "total": 0, "blocking_open": 0, "ready": True,
                "counts_by_state": {}, "pending_human_ids": [],
            },
        }
        validate_bundle(bundle)

    def test_the_sdk_slice_distinguishes_absent_from_empty(self) -> None:
        from sdk.criterion_examples import criterion_matrix_example

        assert criterion_matrix_example("absent_matrix") is None
        assert criterion_matrix_example("explicit_empty") == {
            "rows": [],
            "summary": {
                "total": 0, "blocking_open": 0, "ready": True,
                "counts_by_state": {}, "pending_human_ids": [],
            },
        }


class TestDurableClaimRoundTrip:
    """QA-risk: the criterion link must survive artifact/event/resume."""

    def test_a_reviewer_link_survives_parser_phase_persistence_and_resume(
        self, run_dir,
    ) -> None:
        """The criterion link travels the real writer chain, not a hand edit.

        release JSON -> ``parse_release`` -> phase_log -> the real
        ``FinalAcceptanceAdapter`` -> session/meta -> ``collect_evidence`` ->
        the criterion matrix. Nothing in this path is stubbed, so a contract
        that cannot express the link, or a writer that drops it, fails here.
        """
        from pipeline.release_parser import parse_release
        from pipeline.session_adapters import FinalAcceptanceAdapter

        raw = json.dumps({
            "verdict": "REJECTED",
            "ship_ready": False,
            "short_summary": "The docs still read as internals.",
            "release_blockers": [{
                "id": "R1", "severity": "P1", "title": "Docs unclear",
                "body": "The public explanation assumes internal context.",
                "required_fix": "Rewrite the overview.",
                "why_blocks_release": "A user cannot follow the workflow.",
                "criterion_id": "C3",
            }],
            "verification_gaps": [],
            "contract_status": {
                "task_contract": "incomplete", "interfaces": "not_applicable",
                "persistence": "not_applicable", "tests": "sufficient",
            },
        })
        parsed = parse_release(raw)
        assert parsed.release_blockers[0].criterion_id == "C3"

        # The handler's dual-shape mirror is what the adapter persists.
        state = SimpleNamespace(phase_log={"final_acceptance": {
            "output": "body",
            "verdict": parsed.verdict,
            "approved": parsed.approved,
            "short_summary": parsed.short_summary,
            "findings": [b.to_finding_dict() for b in parsed.release_blockers],
            "ship_ready": parsed.ship_ready,
            "release_blockers": parsed.blockers_as_dicts(),
            "verification_gaps": parsed.gaps_as_dicts(),
            "contract_status": parsed.contract_status.to_dict(),
        }})
        session: dict = {}
        FinalAcceptanceAdapter().write("final_acceptance", state, session)

        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        meta["phases"] = session["phases"]
        (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        # Resume: nothing is in memory any more; the matrix is rebuilt from
        # the durable artifacts alone.
        bundle = collect_evidence(run_dir)
        finding = next(
            f for f in bundle["findings"] if f["id"] == "R1"
        )
        assert finding["criterion_id"] == "C3"
        c3 = next(
            r for r in bundle["criterion_matrix"]["rows"]
            if r["criterion_id"] == "C3"
        )
        assert c3["state"] == "advisory"
        assert {p["kind"] for p in c3["proof_refs"]} == {"claim", "finding"}
        assert {"kind": "finding", "id": "R1"} in c3["proof_refs"]

    def test_a_dangling_criterion_link_is_inert(self, run_dir) -> None:
        """A link to an id the plan never declared contributes no proof."""
        from pipeline.criterion_claims import claims_from_findings

        facts = claims_from_findings([
            {"id": "R9", "criterion_id": "C999"},
            {"id": "R8"},
        ])
        assert [f.criterion_id for f in facts] == ["C999"]
        rows = collect_evidence(run_dir)["criterion_matrix"]["rows"]
        assert all(
            all(p["id"] != "R9" for p in row["proof_refs"]) for row in rows
        )

    def test_the_matrix_is_rebuilt_from_artifacts_not_from_a_live_plan(
        self, run_dir,
    ) -> None:
        # Nothing in this process holds the plan; the reducer reads
        # parsed_plan.json, the ledger, the claim log, and the decision log.
        matrix = criterion_matrix_for_run(run_dir)
        assert matrix is not None
        assert [r.criterion_id for r in matrix.rows] == [
            "C1", "C2", "C3", "C4", "C5",
        ]
        assert matrix.rows[0].executors == ("t1",)

    def test_the_plan_artifact_stores_typed_criteria_not_prose(
        self, run_dir,
    ) -> None:
        artifact = json.loads(
            (run_dir / "parsed_plan.json").read_text(encoding="utf-8"),
        )
        assert artifact["plan"]["acceptance_criteria"] == _PLAN[
            "acceptance_criteria"
        ]
        assert artifact["plan"]["tasks"][0]["acceptance_refs"] == ["C1"]
        plan = parse_plan(json.dumps(_PLAN))
        assert criteria_to_wire(plan.acceptance_criteria) == _PLAN[
            "acceptance_criteria"
        ]
