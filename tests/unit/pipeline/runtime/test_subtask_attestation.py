"""Tests for the subtask done-criteria self-attestation (P7 / ADR 0068).

Three separable concerns, mirroring the reviewer parser's split:

* SHAPE — :func:`validate_subtask_attestation_dict` (schema only).
* RECOVERY — :func:`parse_subtask_attestation` (one object out of build prose).
* MATCH — :func:`validate_subtask_attestation` (does it cover THIS subtask).

The gate is deterministic and never judges whether the evidence is true.
"""
from __future__ import annotations

import json

import pytest

from agents.entities import SubTask
from core.contracts.subtask_attestation_schema import (
    ATTESTATION_SUMMARY_MAX_CHARS,
    SubtaskAttestationSchemaError,
    validate_subtask_attestation_dict,
)
from pipeline.subtask_attestation_parser import (
    SubtaskAttestationParseError,
    parse_subtask_attestation,
    validate_subtask_attestation,
)

pytestmark = pytest.mark.prompts


def _criterion(index=1, criterion="crit", met=True, evidence="did it"):
    return {
        "index": index,
        "criterion": criterion,
        "met": met,
        "evidence": evidence,
    }


def _payload(subtask_id="t1", criteria=None, summary="all met"):
    return {
        "type": "subtask_attestation",
        "subtask_id": subtask_id,
        "criteria": criteria if criteria is not None else [_criterion()],
        "summary": summary,
    }


def _subtask(*criteria, sid="t1"):
    return SubTask(
        id=sid, goal="g", spec="s", files=(), done_criteria=tuple(criteria),
        depends_on=(),
    )


# ── SHAPE ──────────────────────────────────────────────────────────────────

class TestSchemaShape:
    def test_valid_payload_passes(self):
        data = _payload()
        assert validate_subtask_attestation_dict(data) is data

    @pytest.mark.parametrize("bad", ["[]", "1", '"x"', "null"])
    def test_non_object_rejected(self, bad):
        with pytest.raises(SubtaskAttestationSchemaError):
            validate_subtask_attestation_dict(json.loads(bad))

    def test_wrong_type_tag_rejected(self):
        data = _payload()
        data["type"] = "review"
        with pytest.raises(SubtaskAttestationSchemaError):
            validate_subtask_attestation_dict(data)

    @pytest.mark.parametrize("key", ["type", "subtask_id", "criteria", "summary"])
    def test_missing_required_key_rejected(self, key):
        data = _payload()
        del data[key]
        with pytest.raises(SubtaskAttestationSchemaError):
            validate_subtask_attestation_dict(data)

    def test_empty_subtask_id_rejected(self):
        with pytest.raises(SubtaskAttestationSchemaError):
            validate_subtask_attestation_dict(_payload(subtask_id="  "))

    def test_bool_index_rejected(self):
        # bool is an int subclass — a flag must never pose as an index.
        data = _payload(criteria=[_criterion(index=True)])
        with pytest.raises(SubtaskAttestationSchemaError):
            validate_subtask_attestation_dict(data)

    @pytest.mark.parametrize("idx", [0, -1])
    def test_non_positive_index_rejected(self, idx):
        data = _payload(criteria=[_criterion(index=idx)])
        with pytest.raises(SubtaskAttestationSchemaError):
            validate_subtask_attestation_dict(data)

    def test_non_bool_met_rejected(self):
        data = _payload(criteria=[_criterion(met="yes")])
        with pytest.raises(SubtaskAttestationSchemaError):
            validate_subtask_attestation_dict(data)

    def test_empty_evidence_rejected(self):
        data = _payload(criteria=[_criterion(evidence="")])
        with pytest.raises(SubtaskAttestationSchemaError):
            validate_subtask_attestation_dict(data)

    def test_summary_truncated_not_rejected(self):
        long = "x" * (ATTESTATION_SUMMARY_MAX_CHARS + 50)
        data = validate_subtask_attestation_dict(_payload(summary=long))
        assert len(data["summary"]) <= ATTESTATION_SUMMARY_MAX_CHARS


# ── RECOVERY ────────────────────────────────────────────────────────────────

class TestParse:
    def test_recovers_object_after_build_prose(self):
        text = "## Build output\n\nDid the work.\n\n" + json.dumps(_payload())
        parsed = parse_subtask_attestation(text)
        assert parsed.subtask_id == "t1"
        assert parsed.criteria[0].index == 1
        assert parsed.criteria[0].met is True

    def test_no_json_raises_parse_error(self):
        with pytest.raises(SubtaskAttestationParseError):
            parse_subtask_attestation("just prose, no object at all")

    def test_malformed_shape_raises_schema_error(self):
        text = json.dumps({"type": "subtask_attestation", "subtask_id": "t1"})
        with pytest.raises(SubtaskAttestationSchemaError):
            parse_subtask_attestation(text)


# ── MATCH ───────────────────────────────────────────────────────────────────

class TestValidateAgainstSubtask:
    def test_all_met_and_ids_match_ok(self):
        sub = _subtask("a", "b")
        parsed = parse_subtask_attestation(json.dumps(_payload(criteria=[
            _criterion(index=1, criterion="a"),
            _criterion(index=2, criterion="b"),
        ])))
        assert validate_subtask_attestation(parsed, sub) == (True, None)

    def test_index_is_binding_text_drift_tolerated(self):
        # The agent reworded both criteria but addressed both indexes.
        sub = _subtask("write docs", "add tests")
        parsed = parse_subtask_attestation(json.dumps(_payload(criteria=[
            _criterion(index=1, criterion="documented the flag"),
            _criterion(index=2, criterion="ДОБАВИЛ ТЕСТЫ"),
        ])))
        ok, reason = validate_subtask_attestation(parsed, sub)
        assert ok is True
        assert reason is None

    def test_subtask_id_mismatch_fails(self):
        sub = _subtask("a", sid="t1")
        parsed = parse_subtask_attestation(
            json.dumps(_payload(subtask_id="other")))
        ok, reason = validate_subtask_attestation(parsed, sub)
        assert ok is False
        assert "subtask_id" in reason

    def test_missing_criterion_index_fails(self):
        sub = _subtask("a", "b")
        parsed = parse_subtask_attestation(json.dumps(_payload(criteria=[
            _criterion(index=1, criterion="a"),
        ])))
        ok, reason = validate_subtask_attestation(parsed, sub)
        assert ok is False
        assert "indexes" in reason

    def test_extra_criterion_index_fails(self):
        sub = _subtask("a")
        parsed = parse_subtask_attestation(json.dumps(_payload(criteria=[
            _criterion(index=1, criterion="a"),
            _criterion(index=2, criterion="phantom"),
        ])))
        ok, reason = validate_subtask_attestation(parsed, sub)
        assert ok is False
        assert "indexes" in reason

    def test_duplicate_index_fails(self):
        sub = _subtask("a", "b")
        parsed = parse_subtask_attestation(json.dumps(_payload(criteria=[
            _criterion(index=1, criterion="a"),
            _criterion(index=1, criterion="a again"),
        ])))
        ok, reason = validate_subtask_attestation(parsed, sub)
        assert ok is False
        assert "duplicate" in reason

    def test_unmet_criterion_fails_by_index(self):
        sub = _subtask("a", "b")
        parsed = parse_subtask_attestation(json.dumps(_payload(criteria=[
            _criterion(index=1, criterion="a", met=True),
            _criterion(index=2, criterion="b", met=False),
        ])))
        ok, reason = validate_subtask_attestation(parsed, sub)
        assert ok is False
        assert "[2]" in reason
