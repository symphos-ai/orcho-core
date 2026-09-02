# SPDX-License-Identifier: Apache-2.0
"""T3 (F2 + F3) — durable typed human decisions and supersession.

Writer-to-reader coverage on BOTH levels: the durable writer directly, and the
public SDK/CLI boundary. Covers C6 of ADR 0188.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.contracts.criteria import AcceptanceCriterion
from pipeline.criterion_decisions import (
    DECISION_SCHEMA_VERSION,
    HumanDecision,
    HumanDecisionError,
    decision_chain_head,
    decisions_lock_path,
    decisions_path,
    human_decision_facts,
    load_human_decisions,
    record_human_decision,
    to_recorded_at,
)
from pipeline.criterion_matrix import build_criterion_matrix
from pipeline.plan_artifacts import write_parsed_plan_artifact
from pipeline.plan_parser import parse_plan
from sdk.errors import CriterionDecisionRejected

RUN_ID = "20260101_000000"

#: The accepted plan every decision is checked against. ``C3`` / ``C4`` are the
#: human criteria; ``C1`` and ``C2`` exist so the non-human negatives have a
#: real declared criterion of the wrong class to aim at.
_PLAN = {
    "short_summary": "s",
    "planning_context": "p",
    "acceptance_criteria": [
        {"id": "C1", "intent": "regression tested", "verify": "executable",
         "gate_refs": [
             {"command": "unit", "hook": "after_phase", "phase": "implement"},
         ]},
        {"id": "C2", "intent": "reads coherently", "verify": "agent_assertion"},
        {"id": "C3", "intent": "operator accepts the journey", "verify": "human",
         "human_instructions": "Exercise it and record the outcome."},
        {"id": "C4", "intent": "operator accepts rollback", "verify": "human",
         "human_instructions": "Roll back and record the outcome."},
    ],
    "tasks": [{"id": "t1", "goal": "g", "acceptance_refs": ["C1"]}],
}


def _write_run(root, run_id: str = RUN_ID):
    d = root / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps({"run_id": run_id, "status": "done"}), encoding="utf-8",
    )
    write_parsed_plan_artifact(d, parse_plan(json.dumps(_PLAN)), attempt=1)
    return d


@pytest.fixture()
def run_dir(tmp_path):
    return _write_run(tmp_path)


def _accept(run_dir, **kw):
    return record_human_decision(
        run_dir, run_id=RUN_ID, criterion_id="C3", decision="accept", **kw,
    )


def _wire(decision_id: str, criterion_id: str, decision: str, **extra):
    """One durable decision dict, for hand-written (malformed) journals."""
    return {
        "decision_id": decision_id,
        "run_id": RUN_ID,
        "criterion_id": criterion_id,
        "decision": decision,
        "recorded_at": "2026-01-01T00:00:00Z",
        **extra,
    }


def _write_journal(run_dir, decisions) -> None:
    decisions_path(run_dir).write_text(
        json.dumps({
            "schema_version": DECISION_SCHEMA_VERSION,
            "decisions": decisions,
        }),
        encoding="utf-8",
    )


# ── F2: full typing of the durable record ────────────────────────────────────


class TestRecordTyping:
    def test_recorded_at_is_rfc3339_utc_with_a_z_suffix(self, run_dir) -> None:
        record = _accept(run_dir)
        assert record.recorded_at.endswith("Z")
        assert "+" not in record.recorded_at

    def test_aware_non_utc_input_is_normalized_to_utc(self) -> None:
        moment = datetime(
            2026, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=3)),
        )
        assert to_recorded_at(moment) == "2026-01-01T09:00:00Z"

    def test_naive_datetime_is_rejected_before_write(self, run_dir) -> None:
        with pytest.raises(HumanDecisionError, match="aware datetime"):
            _accept(run_dir, recorded_at=datetime(2026, 1, 1, 12, 0, 0))  # noqa: DTZ001
        assert not decisions_path(run_dir).exists()

    def test_microseconds_are_preserved_when_present(self) -> None:
        moment = datetime(2026, 1, 1, 0, 0, 0, 500000, tzinfo=UTC)
        assert to_recorded_at(moment) == "2026-01-01T00:00:00.500000Z"

    def test_unused_optional_keys_are_absent_never_null(self, run_dir) -> None:
        record = _accept(run_dir)
        wire = record.to_dict()
        assert set(wire) == {
            "decision_id", "run_id", "criterion_id", "decision", "recorded_at",
        }

    def test_present_optional_keys_are_trimmed(self, run_dir) -> None:
        record = _accept(run_dir, note="  looks good  ", actor=" eu ")
        assert record.to_dict()["note"] == "looks good"
        assert record.to_dict()["actor"] == "eu"

    @pytest.mark.parametrize("field", ["note", "actor"])
    def test_empty_optional_value_is_rejected_before_write(
        self, run_dir, field,
    ) -> None:
        with pytest.raises(HumanDecisionError, match="non-empty when present"):
            _accept(run_dir, **{field: "   "})
        assert not decisions_path(run_dir).exists()

    def test_unknown_decision_value_is_rejected(self, run_dir) -> None:
        with pytest.raises(HumanDecisionError, match="decision must be one of"):
            record_human_decision(
                run_dir, run_id=RUN_ID, criterion_id="C3", decision="maybe",
            )

    def test_reader_rejects_null_optional_keys(self, run_dir) -> None:
        decisions_path(run_dir).write_text(json.dumps({
            "schema_version": DECISION_SCHEMA_VERSION,
            "decisions": [{
                "decision_id": "hd-C3-1", "run_id": "r", "criterion_id": "C3",
                "decision": "accept", "recorded_at": "2026-01-01T00:00:00Z",
                "note": None,
            }],
        }), encoding="utf-8")
        with pytest.raises(HumanDecisionError, match="null"):
            load_human_decisions(run_dir)

    def test_reader_rejects_unknown_keys(self, run_dir) -> None:
        decisions_path(run_dir).write_text(json.dumps({
            "schema_version": DECISION_SCHEMA_VERSION,
            "decisions": [{
                "decision_id": "hd-C3-1", "run_id": "r", "criterion_id": "C3",
                "decision": "accept", "recorded_at": "2026-01-01T00:00:00Z",
                "sneaky": 1,
            }],
        }), encoding="utf-8")
        with pytest.raises(HumanDecisionError, match="unknown keys"):
            load_human_decisions(run_dir)

    @pytest.mark.parametrize(
        "stamp",
        [
            "2026-99-99T99:99:99Z",   # nothing about this is a real instant
            "2026-13-01T00:00:00Z",   # month 13
            "2026-02-30T00:00:00Z",   # February 30th
            "2026-01-01T24:00:00Z",   # hour 24
            "2026-01-01T00:60:00Z",   # minute 60
            "2026-01-01T00:00:60Z",   # second 60
        ],
    )
    def test_reader_rejects_an_impossible_utc_instant(self, run_dir, stamp) -> None:
        """Digit layout is not validation — the digits must name a real time."""
        _write_journal(run_dir, [
            {**_wire("hd-C3-1", "C3", "accept"), "recorded_at": stamp},
        ])
        with pytest.raises(HumanDecisionError, match="real instant"):
            load_human_decisions(run_dir)

    def test_a_verified_stamp_is_handed_on_byte_for_byte(self, run_dir) -> None:
        """Readers verify the string; they never reparse or reformat it."""
        _write_journal(run_dir, [
            {**_wire("hd-C3-1", "C3", "accept"),
             "recorded_at": "2026-01-01T00:00:00.000000Z"},
        ])
        record = load_human_decisions(run_dir)[0]
        assert record.recorded_at == "2026-01-01T00:00:00.000000Z"
        assert record.to_dict()["recorded_at"] == "2026-01-01T00:00:00.000000Z"

    def test_reader_rejects_non_canonical_recorded_at(self, run_dir) -> None:
        decisions_path(run_dir).write_text(json.dumps({
            "schema_version": DECISION_SCHEMA_VERSION,
            "decisions": [{
                "decision_id": "hd-C3-1", "run_id": "r", "criterion_id": "C3",
                "decision": "accept", "recorded_at": "2026-01-01T00:00:00+00:00",
            }],
        }), encoding="utf-8")
        with pytest.raises(HumanDecisionError, match="RFC 3339"):
            load_human_decisions(run_dir)

    def test_json_round_trip_is_canonical_both_with_and_without_options(
        self, run_dir,
    ) -> None:
        full = _accept(run_dir, note="n", actor="a")
        record_human_decision(
            run_dir, run_id=RUN_ID, criterion_id="C4",
            decision="reject",
        )
        reread = load_human_decisions(run_dir)
        assert json.dumps([r.to_dict() for r in reread]) == json.dumps(
            [full.to_dict(), reread[1].to_dict()],
        )
        assert "note" not in json.dumps(reread[1].to_dict())
        assert "null" not in decisions_path(run_dir).read_text(encoding="utf-8")


# ── F3: supersession invariant, durable-writer level ─────────────────────────


class TestSupersessionAtTheWriter:
    def test_first_decision_omits_supersedes(self, run_dir) -> None:
        record = _accept(run_dir)
        assert record.supersedes is None
        assert "supersedes" not in record.to_dict()

    def test_first_decision_naming_a_supersedes_is_rejected(self, run_dir) -> None:
        with pytest.raises(HumanDecisionError, match="must omit supersedes"):
            _accept(run_dir, supersedes="hd-C3-0")
        assert not decisions_path(run_dir).exists()

    def test_valid_replacement_names_the_current_head(self, run_dir) -> None:
        first = _accept(run_dir)
        second = record_human_decision(
            run_dir, run_id=RUN_ID, criterion_id="C3",
            decision="reject", supersedes=first.decision_id,
        )
        records = load_human_decisions(run_dir)
        assert len(records) == 2
        assert decision_chain_head(records, "C3") == second

    def test_replacement_without_supersedes_is_rejected(self, run_dir) -> None:
        _accept(run_dir)
        before = decisions_path(run_dir).read_bytes()
        with pytest.raises(HumanDecisionError, match="must name it in supersedes"):
            record_human_decision(
                run_dir, run_id=RUN_ID, criterion_id="C3",
                decision="reject",
            )
        assert decisions_path(run_dir).read_bytes() == before

    def test_stale_supersession_is_rejected_without_touching_the_artifact(
        self, run_dir,
    ) -> None:
        first = _accept(run_dir)
        record_human_decision(
            run_dir, run_id=RUN_ID, criterion_id="C3",
            decision="reject", supersedes=first.decision_id,
        )
        record_human_decision(
            run_dir, run_id=RUN_ID, criterion_id="C3",
            decision="accept", supersedes="hd-C3-2",
        )
        before = decisions_path(run_dir).read_bytes()
        with pytest.raises(HumanDecisionError, match="stale supersession"):
            record_human_decision(
                run_dir, run_id=RUN_ID, criterion_id="C3",
                decision="reject", supersedes=first.decision_id,
            )
        assert decisions_path(run_dir).read_bytes() == before

    def test_branched_supersession_is_rejected_without_touching_the_artifact(
        self, run_dir,
    ) -> None:
        first = _accept(run_dir)
        record_human_decision(
            run_dir, run_id=RUN_ID, criterion_id="C3",
            decision="reject", supersedes=first.decision_id,
        )
        before = decisions_path(run_dir).read_bytes()
        with pytest.raises(HumanDecisionError, match="branched supersession"):
            record_human_decision(
                run_dir, run_id=RUN_ID, criterion_id="C3",
                decision="accept", supersedes=first.decision_id,
            )
        assert decisions_path(run_dir).read_bytes() == before

    def test_unknown_supersedes_target_is_rejected(self, run_dir) -> None:
        _accept(run_dir)
        before = decisions_path(run_dir).read_bytes()
        with pytest.raises(HumanDecisionError, match="not a decision of criterion"):
            record_human_decision(
                run_dir, run_id=RUN_ID, criterion_id="C3",
                decision="reject", supersedes="hd-C9-1",
            )
        assert decisions_path(run_dir).read_bytes() == before

    def test_chain_reload_selects_one_deterministic_head(self, run_dir) -> None:
        first = _accept(run_dir)
        second = record_human_decision(
            run_dir, run_id=RUN_ID, criterion_id="C3",
            decision="reject", supersedes=first.decision_id,
        )
        # Simulate resume: drop every in-memory object and re-read the artifact.
        reloaded = load_human_decisions(run_dir)
        assert decision_chain_head(reloaded, "C3").decision_id == second.decision_id
        assert human_decision_facts(run_dir)["C3"].decision_id == second.decision_id

    def test_a_branched_artifact_is_reported_not_silently_resolved(
        self, run_dir,
    ) -> None:
        _write_journal(run_dir, [
            _wire("hd-C3-1", "C3", "accept"),
            # Second record for C3 that names no head at all: a fork.
            _wire("hd-C3-2", "C3", "reject"),
        ])
        with pytest.raises(HumanDecisionError, match="must supersede"):
            load_human_decisions(run_dir)
        # Defence in depth: the head selector itself refuses a branched
        # sequence handed to it directly, rather than picking a side.
        with pytest.raises(HumanDecisionError, match="branched"):
            decision_chain_head(
                (
                    HumanDecision("hd-C3-1", RUN_ID, "C3", "accept",
                                  "2026-01-01T00:00:00Z"),
                    HumanDecision("hd-C3-2", RUN_ID, "C3", "reject",
                                  "2026-01-01T00:00:01Z"),
                ),
                "C3",
            )

    def test_matrix_proof_ref_points_at_the_head_decision_id(self, run_dir) -> None:
        first = _accept(run_dir)
        head = record_human_decision(
            run_dir, run_id=RUN_ID, criterion_id="C3",
            decision="reject", supersedes=first.decision_id,
        )
        matrix = build_criterion_matrix(
            (AcceptanceCriterion("C3", "i", "human", human_instructions="do"),),
            human_decisions=human_decision_facts(run_dir),
        )
        assert matrix.rows[0].state == "rejected"
        assert matrix.rows[0].proof_refs[0].to_dict() == {
            "kind": "human_decision", "id": head.decision_id,
        }


# ── F3 negatives: unknown / non-human / wrong-run never reach the log ────────


class TestDecisionAdmission:
    """A decision is only ever written for a declared ``human`` criterion."""

    def test_unknown_criterion_is_rejected_with_no_artifact(self, run_dir) -> None:
        with pytest.raises(HumanDecisionError, match="unknown criterion"):
            record_human_decision(
                run_dir, run_id=RUN_ID, criterion_id="C999", decision="accept",
            )
        assert not decisions_path(run_dir).exists()

    @pytest.mark.parametrize(
        ("criterion_id", "verify"), [("C1", "executable"), ("C2", "agent_assertion")],
    )
    def test_non_human_criterion_is_rejected_with_no_artifact(
        self, run_dir, criterion_id, verify,
    ) -> None:
        with pytest.raises(HumanDecisionError, match="not 'human'"):
            record_human_decision(
                run_dir, run_id=RUN_ID, criterion_id=criterion_id,
                decision="accept",
            )
        assert not decisions_path(run_dir).exists()

    def test_wrong_run_id_is_rejected_with_no_artifact(self, run_dir) -> None:
        with pytest.raises(HumanDecisionError, match="does not identify run"):
            record_human_decision(
                run_dir, run_id="20991231_235959", criterion_id="C3",
                decision="accept",
            )
        assert not decisions_path(run_dir).exists()

    def test_admission_negatives_leave_an_existing_log_byte_identical(
        self, run_dir,
    ) -> None:
        _accept(run_dir)
        before = decisions_path(run_dir).read_bytes()
        for kwargs in (
            {"run_id": RUN_ID, "criterion_id": "C999", "decision": "accept"},
            {"run_id": RUN_ID, "criterion_id": "C2", "decision": "accept"},
            {"run_id": "20991231_235959", "criterion_id": "C3",
             "decision": "accept"},
        ):
            with pytest.raises(HumanDecisionError):
                record_human_decision(run_dir, **kwargs)
        assert decisions_path(run_dir).read_bytes() == before

    def test_a_run_without_an_accepted_plan_cannot_record_a_decision(
        self, tmp_path,
    ) -> None:
        bare = tmp_path / RUN_ID
        bare.mkdir()
        with pytest.raises(HumanDecisionError, match="no accepted plan artifact"):
            record_human_decision(
                bare, run_id=RUN_ID, criterion_id="C3", decision="accept",
            )
        assert not decisions_path(bare).exists()

    def test_run_identity_prefers_meta_over_the_directory_name(
        self, tmp_path,
    ) -> None:
        # A cross-project child lives under ``<parent>/<alias>/``, so its
        # directory name is the alias, not the run id.
        child = tmp_path / "20260101_000000" / "core"
        child.mkdir(parents=True)
        (child / "meta.json").write_text(
            json.dumps({"run_id": "20260101_000000"}), encoding="utf-8",
        )
        write_parsed_plan_artifact(child, parse_plan(json.dumps(_PLAN)), attempt=1)
        record = record_human_decision(
            child, run_id="20260101_000000", criterion_id="C3", decision="accept",
        )
        assert record.run_id == "20260101_000000"
        assert load_human_decisions(child)[0].decision_id == record.decision_id


# ── F7: the reader replays the whole journal ─────────────────────────────────


class TestJournalValidationOnRead:
    def test_dangling_supersedes_on_a_first_record_is_rejected(
        self, run_dir,
    ) -> None:
        _write_journal(run_dir, [_wire("hd-C3-1", "C3", "accept",
                                       supersedes="ghost")])
        with pytest.raises(HumanDecisionError, match="names supersedes"):
            load_human_decisions(run_dir)

    def test_duplicate_decision_ids_are_rejected(self, run_dir) -> None:
        _write_journal(run_dir, [
            _wire("hd-1", "C3", "accept"),
            _wire("hd-1", "C4", "accept"),
        ])
        with pytest.raises(HumanDecisionError, match="repeats decision_id"):
            load_human_decisions(run_dir)

    def test_a_cross_criterion_supersedes_is_rejected(self, run_dir) -> None:
        _write_journal(run_dir, [
            _wire("hd-C3-1", "C3", "accept"),
            _wire("hd-C4-1", "C4", "reject", supersedes="hd-C3-1"),
        ])
        with pytest.raises(HumanDecisionError, match="names supersedes"):
            load_human_decisions(run_dir)

    def test_a_wrong_run_record_is_rejected(self, run_dir) -> None:
        _write_journal(run_dir, [
            {**_wire("hd-C3-1", "C3", "accept"), "run_id": "20991231_235959"},
        ])
        with pytest.raises(HumanDecisionError, match="belongs to run"):
            load_human_decisions(run_dir)

    def test_a_stale_supersedes_deeper_in_the_chain_is_rejected(
        self, run_dir,
    ) -> None:
        _write_journal(run_dir, [
            _wire("hd-C3-1", "C3", "accept"),
            _wire("hd-C3-2", "C3", "reject", supersedes="hd-C3-1"),
            _wire("hd-C3-3", "C3", "accept", supersedes="hd-C3-1"),
        ])
        with pytest.raises(HumanDecisionError, match="must supersede"):
            load_human_decisions(run_dir)

    def test_independent_criteria_keep_independent_chains(self, run_dir) -> None:
        _write_journal(run_dir, [
            _wire("hd-C3-1", "C3", "accept"),
            _wire("hd-C4-1", "C4", "reject"),
            _wire("hd-C3-2", "C3", "reject", supersedes="hd-C3-1"),
        ])
        facts = human_decision_facts(run_dir)
        assert facts["C3"].decision_id == "hd-C3-2"
        assert facts["C4"].decision_id == "hd-C4-1"


# ── F3: the same invariant at the public SDK / CLI boundary ──────────────────


class TestSupersessionAtTheSdkBoundary:
    @pytest.fixture()
    def runs_dir(self, tmp_path):
        rd = tmp_path / "runspace" / "runs"
        rd.mkdir(parents=True)
        _write_run(rd)
        return rd

    def _sdk_record(self, runs_dir, **kw):
        from sdk.criterion_decisions import record_criterion_decision

        return record_criterion_decision(
            RUN_ID, runs_dir=runs_dir, criterion_id="C3", **kw,
        )

    def test_first_write_then_valid_replacement(self, runs_dir) -> None:
        from sdk.criterion_decisions import list_criterion_decisions

        first = self._sdk_record(runs_dir, decision="accept")
        assert "supersedes" not in first
        second = self._sdk_record(
            runs_dir, decision="reject", supersedes=first["decision_id"],
        )
        log = list_criterion_decisions(RUN_ID, runs_dir=runs_dir)
        assert [r["decision_id"] for r in log] == [
            first["decision_id"], second["decision_id"],
        ]

    def test_sdk_writer_assigns_run_id_decision_id_and_recorded_at(
        self, runs_dir,
    ) -> None:
        record = self._sdk_record(runs_dir, decision="accept")
        assert record["run_id"] == RUN_ID
        assert record["decision_id"] == "hd-C3-1"
        assert record["recorded_at"].endswith("Z")

    @pytest.mark.parametrize(
        ("criterion_id", "match"),
        [
            ("C999", "unknown criterion"),
            ("C2", "not 'human'"),
        ],
    )
    def test_sdk_refuses_unknown_and_non_human_criteria(
        self, runs_dir, criterion_id, match,
    ) -> None:
        from sdk.criterion_decisions import record_criterion_decision

        path = runs_dir / RUN_ID / "criterion_decisions.json"
        with pytest.raises(CriterionDecisionRejected, match=match):
            record_criterion_decision(
                RUN_ID, runs_dir=runs_dir, criterion_id=criterion_id,
                decision="accept",
            )
        assert not path.exists()

    def test_sdk_cannot_write_into_another_run(self, tmp_path, runs_dir) -> None:
        from sdk.criterion_decisions import record_criterion_decision

        other = _write_run(runs_dir, "20260202_000000")
        record_criterion_decision(
            "20260202_000000", runs_dir=runs_dir, criterion_id="C3",
            decision="accept",
        )
        # The decision landed in its own run's log and nowhere else.
        assert (other / "criterion_decisions.json").is_file()
        assert not (runs_dir / RUN_ID / "criterion_decisions.json").exists()

    def _chain_of_three(self, runs_dir) -> list[dict]:
        """Head is ``hd-C3-3``; ``hd-C3-1`` is deep-stale, ``hd-C3-2`` is the
        head's own predecessor (a branch target)."""
        first = self._sdk_record(runs_dir, decision="accept")
        second = self._sdk_record(
            runs_dir, decision="reject", supersedes=first["decision_id"],
        )
        third = self._sdk_record(
            runs_dir, decision="accept", supersedes=second["decision_id"],
        )
        return [first, second, third]

    def test_branched_supersession_is_rejected_with_no_artifact_change(
        self, runs_dir,
    ) -> None:
        path = runs_dir / RUN_ID / "criterion_decisions.json"
        first = self._sdk_record(runs_dir, decision="accept")
        self._sdk_record(
            runs_dir, decision="reject", supersedes=first["decision_id"],
        )
        before = path.read_bytes()
        with pytest.raises(
            CriterionDecisionRejected, match="branched supersession",
        ):
            self._sdk_record(
                runs_dir, decision="accept", supersedes=first["decision_id"],
            )
        assert path.read_bytes() == before

    def test_deep_stale_supersession_is_rejected_with_no_artifact_change(
        self, runs_dir,
    ) -> None:
        path = runs_dir / RUN_ID / "criterion_decisions.json"
        first, _second, _third = self._chain_of_three(runs_dir)
        before = path.read_bytes()
        with pytest.raises(
            CriterionDecisionRejected, match="stale supersession",
        ):
            self._sdk_record(
                runs_dir, decision="reject", supersedes=first["decision_id"],
            )
        assert path.read_bytes() == before

    def test_a_missing_supersedes_on_a_replacement_is_rejected(
        self, runs_dir,
    ) -> None:
        path = runs_dir / RUN_ID / "criterion_decisions.json"
        self._sdk_record(runs_dir, decision="accept")
        before = path.read_bytes()
        with pytest.raises(
            CriterionDecisionRejected, match="must name it in supersedes",
        ):
            self._sdk_record(runs_dir, decision="reject")
        assert path.read_bytes() == before

    def test_reload_after_resume_selects_the_single_head_deterministically(
        self, runs_dir,
    ) -> None:
        """Nothing is in memory: re-read the log through the public SDK twice
        and reduce it into a matrix from durable facts alone."""
        from sdk.criterion_decisions import list_criterion_decisions

        _first, _second, third = self._chain_of_three(runs_dir)
        log_a = list_criterion_decisions(RUN_ID, runs_dir=runs_dir)
        log_b = list_criterion_decisions(RUN_ID, runs_dir=runs_dir)
        assert log_a == log_b
        assert [r["decision_id"] for r in log_a] == [
            "hd-C3-1", "hd-C3-2", "hd-C3-3",
        ]
        facts = human_decision_facts(runs_dir / RUN_ID)
        assert facts["C3"].decision_id == third["decision_id"]
        assert facts["C3"].decision == "accept"

    def test_the_matrix_proof_ref_names_the_head_at_the_public_boundary(
        self, runs_dir,
    ) -> None:
        from sdk.criterion_matrix import get_criterion_matrix

        _first, _second, third = self._chain_of_three(runs_dir)
        rows = get_criterion_matrix(RUN_ID, runs_dir=runs_dir)["rows"]
        c3 = next(r for r in rows if r["criterion_id"] == "C3")
        assert c3["state"] == "accepted"
        assert c3["proof_refs"] == [
            {"kind": "human_decision", "id": third["decision_id"]},
        ]

    def test_canonical_json_round_trip_with_all_optional_fields(
        self, runs_dir,
    ) -> None:
        from sdk.criterion_decisions import list_criterion_decisions
        from sdk.criterion_matrix import canonical_criterion_json

        first = self._sdk_record(
            runs_dir, decision="accept", note="n", actor="a",
        )
        replacement = self._sdk_record(
            runs_dir, decision="reject", note="n2", actor="a2",
            supersedes=first["decision_id"],
        )
        assert set(replacement) == {
            "decision_id", "run_id", "criterion_id", "decision", "recorded_at",
            "note", "actor", "supersedes",
        }
        read_back = list_criterion_decisions(RUN_ID, runs_dir=runs_dir)
        assert canonical_criterion_json(read_back) == canonical_criterion_json(
            [first, replacement],
        )

    def test_canonical_json_round_trip_without_optional_fields(
        self, runs_dir,
    ) -> None:
        from sdk.criterion_decisions import list_criterion_decisions
        from sdk.criterion_matrix import canonical_criterion_json

        written = self._sdk_record(runs_dir, decision="accept")
        assert set(written) == {
            "decision_id", "run_id", "criterion_id", "decision", "recorded_at",
        }
        read_back = list_criterion_decisions(RUN_ID, runs_dir=runs_dir)
        assert canonical_criterion_json(read_back) == canonical_criterion_json(
            [written],
        )
        # The omission survives to the byte level, not just to the dict.
        raw = (runs_dir / RUN_ID / "criterion_decisions.json").read_text(
            encoding="utf-8",
        )
        for key in ("note", "actor", "supersedes"):
            assert f'"{key}"' not in raw
        assert "null" not in raw

    def test_cli_rejects_stale_and_branched_without_touching_the_artifact(
        self, runs_dir, capsys,
    ) -> None:
        import argparse

        from cli._criterion_cli import cmd_criterion_decide

        path = runs_dir / RUN_ID / "criterion_decisions.json"
        first, second, _third = self._chain_of_three(runs_dir)
        before = path.read_bytes()

        def _decide(supersedes: str) -> int:
            return cmd_criterion_decide(argparse.Namespace(
                run_id=RUN_ID, criterion="C3", decision="reject",
                note=None, actor=None, supersedes=supersedes,
                workspace=str(runs_dir.parent.parent),
            ))

        assert _decide(first["decision_id"]) == 2
        assert "stale supersession" in capsys.readouterr().err
        assert _decide(second["decision_id"]) == 2
        assert "branched supersession" in capsys.readouterr().err
        assert path.read_bytes() == before

    def test_cli_decisions_reads_back_the_chain_the_sdk_wrote(
        self, runs_dir, capsys,
    ) -> None:
        import argparse

        from cli._criterion_cli import cmd_criterion_decisions

        self._chain_of_three(runs_dir)
        rc = cmd_criterion_decisions(argparse.Namespace(
            run_id=RUN_ID, workspace=str(runs_dir.parent.parent),
        ))
        assert rc == 0
        log = json.loads(capsys.readouterr().out)
        assert [r["decision_id"] for r in log] == [
            "hd-C3-1", "hd-C3-2", "hd-C3-3",
        ]
        assert "supersedes" not in log[0]
        assert log[2]["supersedes"] == "hd-C3-2"

    def test_cli_decide_records_and_reports_rejection(self, runs_dir, capsys) -> None:
        import argparse

        from cli._criterion_cli import cmd_criterion_decide

        args = argparse.Namespace(
            run_id=RUN_ID, criterion="C3", decision="accept",
            note=None, actor=None, supersedes=None, workspace=None,
        )
        # The CLI resolves through the workspace; point it at the runs dir by
        # writing through the SDK first, then assert the CLI rejects a branch.
        first = self._sdk_record(runs_dir, decision="accept")
        args.supersedes = first["decision_id"]
        args.decision = "reject"
        monkey_ws = runs_dir.parent.parent
        args.workspace = str(monkey_ws)
        assert cmd_criterion_decide(args) == 0
        args.decision = "accept"
        assert cmd_criterion_decide(args) == 2
        assert "decision rejected" in capsys.readouterr().err


def test_the_decision_record_dataclass_matches_the_wire_contract() -> None:
    record = HumanDecision(
        decision_id="hd-C3-1", run_id="r", criterion_id="C3",
        decision="accept", recorded_at="2026-01-01T00:00:00Z",
    )
    assert list(record.to_dict()) == [
        "decision_id", "run_id", "criterion_id", "decision", "recorded_at",
    ]


# ── concurrency (F3): the append cycle is serialized across processes ────────


_CONCURRENT_WRITER = """
import json, sys
sys.path.insert(0, {core!r})
from pipeline.criterion_decisions import HumanDecisionError, record_human_decision

run_dir, run_id, criterion_id, decision, supersedes, barrier = sys.argv[1:7]

# Release only when BOTH writers have arrived, so the two append cycles really
# overlap instead of running one after the other by accident.
from pathlib import Path
import time
Path(barrier).joinpath(decision).write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    if len(list(Path(barrier).iterdir())) >= 2:
        break
    time.sleep(0.01)

try:
    record = record_human_decision(
        run_dir, run_id=run_id, criterion_id=criterion_id,
        decision=decision, supersedes=supersedes,
    )
except HumanDecisionError as exc:
    print(json.dumps({{"outcome": "rejected", "reason": str(exc)}}))
else:
    print(json.dumps({{"outcome": "recorded", "record": record.to_dict()}}))
"""


def _spawn_writer(script: Path, run_dir, supersedes: str, decision: str, barrier):
    return subprocess.Popen(
        [
            sys.executable, str(script), str(run_dir), RUN_ID, "C3",
            decision, supersedes, str(barrier),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def test_two_concurrent_replacements_admit_exactly_one_and_lose_nothing(
    run_dir, tmp_path,
) -> None:
    """Two processes replace the same head at once; the log must not fork.

    This is the failure the file lock exists for, and it is invisible to a
    single-threaded test. Without serialization both writers read the same
    head, both pass the branch validator, and the second whole-file rename
    silently discards the first record — an operator decision vanishes from an
    audit log that is supposed to be append-only.

    The contract: exactly one writer is admitted, the other is rejected as a
    branched supersession, the journal holds both the original head and the
    winner, and the reducer sees the winner as the single head.
    """
    core_root = str(Path(__file__).resolve().parents[4])
    first = record_human_decision(
        run_dir, run_id=RUN_ID, criterion_id="C3", decision="accept",
    )

    script = tmp_path / "writer.py"
    script.write_text(_CONCURRENT_WRITER.format(core=core_root), encoding="utf-8")
    barrier = tmp_path / "barrier"
    barrier.mkdir()

    procs = [
        _spawn_writer(script, run_dir, first.decision_id, "reject", barrier),
        _spawn_writer(script, run_dir, first.decision_id, "accept", barrier),
    ]
    results = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        results.append(json.loads(out.strip().splitlines()[-1]))

    outcomes = sorted(r["outcome"] for r in results)
    assert outcomes == ["recorded", "rejected"], results
    winner = next(r for r in results if r["outcome"] == "recorded")["record"]
    loser = next(r for r in results if r["outcome"] == "rejected")
    assert "branched supersession" in loser["reason"]

    # Nothing was lost: the original head and the winner are both on file, in
    # write order, and the journal still validates as one unbroken chain.
    persisted = load_human_decisions(run_dir)
    assert [r.decision_id for r in persisted] == [
        first.decision_id, winner["decision_id"],
    ]
    assert persisted[-1].supersedes == first.decision_id

    head = decision_chain_head(persisted, "C3")
    assert head is not None
    assert head.decision_id == winner["decision_id"]
    assert head.decision == winner["decision"]
    assert human_decision_facts(run_dir)["C3"].decision_id == winner["decision_id"]


def test_the_lock_file_is_not_the_journal(run_dir) -> None:
    """The lock lives beside the journal, never on the replaced inode.

    The journal is swapped by an atomic rename, so a lock held on *its* inode
    would stop guarding the file the next writer opens. Keeping the two paths
    distinct is what makes the lock meaningful.
    """
    record_human_decision(
        run_dir, run_id=RUN_ID, criterion_id="C3", decision="accept",
    )

    assert decisions_lock_path(run_dir) != decisions_path(run_dir)
    assert decisions_lock_path(run_dir).is_file()
    # The lock file carries no decision data — the journal is the record.
    assert decisions_lock_path(run_dir).read_text(encoding="utf-8") == ""
