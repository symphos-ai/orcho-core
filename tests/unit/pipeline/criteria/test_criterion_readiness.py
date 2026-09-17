# SPDX-License-Identifier: Apache-2.0
"""C8 — final readiness consumes the same reducer summary evidence projects."""
from __future__ import annotations

import json

import pytest

from pipeline.criterion_decisions import record_human_decision
from pipeline.criterion_evidence import criterion_matrix_for_run
from pipeline.evidence.collector import collect_evidence
from pipeline.plan_artifacts import write_parsed_plan_artifact
from pipeline.plan_parser import parse_plan
from pipeline.verification_readiness import (
    CRITERION_INTEGRITY_RISK,
    _criterion_readiness,
    criterion_release_gaps,
)

RUN_ID = "20260101_000000"

_PLAN = {
    "short_summary": "s",
    "planning_context": "p",
    "acceptance_criteria": [
        {"id": "C1", "intent": "docs read coherently", "verify": "agent_assertion"},
        {"id": "C2", "intent": "operator accepts the journey", "verify": "human",
         "human_instructions": "Exercise it and record the outcome."},
    ],
    "tasks": [{"id": "t1", "goal": "g"}],
}


@pytest.fixture()
def run_dir(tmp_path):
    d = tmp_path / RUN_ID
    d.mkdir()
    (d / "meta.json").write_text("{}", encoding="utf-8")
    (d / "events.jsonl").write_text("", encoding="utf-8")
    write_parsed_plan_artifact(d, parse_plan(json.dumps(_PLAN)), attempt=1)
    return d


def test_readiness_consumes_the_reducer_summary_verbatim(run_dir) -> None:
    summary, gaps = _criterion_readiness(run_dir)
    assert summary == criterion_matrix_for_run(run_dir).summary.to_dict()
    assert summary == collect_evidence(run_dir)["criterion_matrix"]["summary"]
    assert gaps == ("C2 pending: awaiting a typed operator decision",)


def test_a_pending_human_criterion_is_a_release_gap(run_dir) -> None:
    gaps = criterion_release_gaps(run_dir)
    assert [g["risk"] for g in gaps] == ["acceptance criterion C2 is pending"]


def test_an_advisory_assertion_never_becomes_a_gap(run_dir) -> None:
    gaps = criterion_release_gaps(run_dir)
    assert all("C1" not in g["risk"] for g in gaps)


def test_accepting_the_human_criterion_clears_readiness(run_dir) -> None:
    record_human_decision(
        run_dir, run_id=RUN_ID, criterion_id="C2", decision="accept",
    )
    summary, gaps = _criterion_readiness(run_dir)
    assert summary["ready"] is True
    assert gaps == ()
    assert criterion_release_gaps(run_dir) == []


def test_a_run_without_a_plan_artifact_keeps_readiness_unchanged(tmp_path) -> None:
    assert _criterion_readiness(tmp_path) == (None, ())
    assert criterion_release_gaps(tmp_path) == []


def test_the_readiness_block_renders_the_consumed_summary(run_dir) -> None:
    from pipeline.verification_readiness import (
        ReadinessSummary,
        render_readiness_block,
    )

    summary, gaps = _criterion_readiness(run_dir)
    block = render_readiness_block(
        ReadinessSummary(criterion_summary=summary, criterion_gaps=gaps),
    )
    assert "Acceptance criteria:" in block
    assert "pending 2" in block
    assert "awaiting operator decision: C2" in block
    assert "open criterion: C2 pending" in block


def test_a_summary_without_criteria_renders_byte_identically(run_dir) -> None:
    from pipeline.verification_readiness import (
        ReadinessSummary,
        render_readiness_block,
    )

    baseline = ReadinessSummary(gate_statuses=("unit: pass",))
    assert "Acceptance criteria:" not in render_readiness_block(baseline)


# ── F2: corrupt durable facts never degrade into "no criteria" ───────────────


class TestIntegrityIsAGapNotSilence:
    """An unreadable artifact is unproven proof, not an absent contract."""

    def test_a_corrupt_plan_artifact_blocks_instead_of_vanishing(
        self, run_dir,
    ) -> None:
        (run_dir / "parsed_plan.json").write_text("{ not json", encoding="utf-8")
        summary, gaps = _criterion_readiness(run_dir)
        assert summary is None
        assert len(gaps) == 1
        assert CRITERION_INTEGRITY_RISK in gaps[0]
        assert [g["risk"] for g in criterion_release_gaps(run_dir)] == [
            CRITERION_INTEGRITY_RISK,
        ]

    def test_a_decision_journal_with_a_null_optional_blocks(self, run_dir) -> None:
        (run_dir / "criterion_decisions.json").write_text(
            json.dumps({
                "schema_version": "1",
                "decisions": [{
                    "decision_id": "hd-C2-1", "run_id": RUN_ID,
                    "criterion_id": "C2", "decision": "accept",
                    "recorded_at": "2026-01-01T00:00:00Z", "note": None,
                }],
            }),
            encoding="utf-8",
        )
        summary, gaps = _criterion_readiness(run_dir)
        assert summary is None
        assert len(gaps) == 1
        assert CRITERION_INTEGRITY_RISK in gaps[0]
        assert criterion_release_gaps(run_dir)[0]["risk"] == (
            CRITERION_INTEGRITY_RISK
        )

    def test_a_corrupt_claim_log_blocks(self, run_dir) -> None:
        (run_dir / "criterion_claims.json").write_text(
            json.dumps({"schema_version": "999", "claims": []}), encoding="utf-8",
        )
        _summary, gaps = _criterion_readiness(run_dir)
        assert len(gaps) == 1
        assert CRITERION_INTEGRITY_RISK in gaps[0]

    def test_a_corrupt_ledger_blocks(self, run_dir) -> None:
        (run_dir / "scheduled_gate_ledger.json").write_text(
            "{ not json", encoding="utf-8",
        )
        _summary, gaps = _criterion_readiness(run_dir)
        assert len(gaps) == 1
        assert CRITERION_INTEGRITY_RISK in gaps[0]

    def test_an_absent_ledger_is_not_an_integrity_error(self, run_dir) -> None:
        # No verification contract at all: the reducer honestly reports
        # ``missing`` for executable refs instead of an integrity failure.
        assert not (run_dir / "scheduled_gate_ledger.json").exists()
        summary, gaps = _criterion_readiness(run_dir)
        assert summary is not None
        assert all(CRITERION_INTEGRITY_RISK not in g for g in gaps)

    def test_a_corrupt_plan_makes_the_evidence_bundle_invalid(
        self, run_dir,
    ) -> None:
        from pipeline.evidence.collector import collect_evidence

        (run_dir / "parsed_plan.json").write_text("{ not json", encoding="utf-8")
        with pytest.raises(ValueError, match="parsed plan artifact"):
            collect_evidence(run_dir)

    def test_the_public_sdk_reports_evidence_invalid_not_a_missing_key(
        self, tmp_path,
    ) -> None:
        from sdk.errors import EvidenceInvalid
        from sdk.evidence import collect_evidence as sdk_collect_evidence

        runs_dir = tmp_path / "runspace" / "runs"
        run_dir = runs_dir / RUN_ID
        run_dir.mkdir(parents=True)
        (run_dir / "meta.json").write_text("{}", encoding="utf-8")
        (run_dir / "events.jsonl").write_text("", encoding="utf-8")
        write_parsed_plan_artifact(run_dir, parse_plan(json.dumps(_PLAN)), attempt=1)
        (run_dir / "parsed_plan.json").write_text("{ not json", encoding="utf-8")
        with pytest.raises(EvidenceInvalid):
            sdk_collect_evidence(RUN_ID, runs_dir=runs_dir)
