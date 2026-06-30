"""Tests for the reviewer JSON contract parser."""
from __future__ import annotations

import json

import pytest

from core.contracts.review_schema import (
    REVIEW_SUMMARY_MAX_CHARS,
    ReviewSchemaError,
    validate_review_dict,
)
from pipeline.review_parser import (
    ParsedReview,
    ReviewParseError,
    parse_review,
)


def _approved_payload(**overrides):
    payload = {
        "verdict": "APPROVED",
        "short_summary": "Reviewed scope; no blocking issues.",
        "findings": [],
        "risks": [],
        "checks": ["Reviewed scope"],
    }
    payload.update(overrides)
    return payload


def _rejected_payload(**overrides):
    payload = {
        "verdict": "REJECTED",
        "short_summary": "P1: missing rollback path.",
        "findings": [
            {
                "id": "F1",
                "severity": "P1",
                "title": "No rollback path",
                "body": "Verification step never describes what to do on failure.",
                "required_fix": "Add an explicit rollback section.",
            }
        ],
    }
    payload.update(overrides)
    return payload


# ── Happy paths ──────────────────────────────────────────────────────────────

class TestRawJSON:
    def test_approved_parses(self):
        out = parse_review(json.dumps(_approved_payload()))
        assert isinstance(out, ParsedReview)
        assert out.approved is True
        assert out.verdict == "APPROVED"
        assert out.findings == ()
        assert out.checks == ("Reviewed scope",)
        assert out.source == "json"

    def test_rejected_parses_with_finding(self):
        out = parse_review(json.dumps(_rejected_payload()))
        assert out.approved is False
        assert len(out.findings) == 1
        f = out.findings[0]
        assert f.id == "F1"
        assert f.severity == "P1"
        assert f.required_fix == "Add an explicit rollback section."
        assert out.source == "json"

    def test_optional_file_and_line_typed_correctly(self):
        payload = _rejected_payload()
        payload["findings"][0].update({"file": "x.py", "line": 42})
        out = parse_review(json.dumps(payload))
        assert out.findings[0].file == "x.py"
        assert out.findings[0].line == 42


class TestJsonFenceBackcompat:
    def test_fenced_json_recovers_with_warning(self):
        body = (
            "Some prose.\n```json\n"
            + json.dumps(_approved_payload())
            + "\n```\n"
        )
        out = parse_review(body)
        assert out.approved is True
        assert len(out.parse_warnings) == 1
        assert "stripped non-JSON text around review JSON" in out.parse_warnings[0]

    def test_prefixed_json_recovers_with_warning(self):
        body = "Reviewed the diff first.\n" + json.dumps(_approved_payload())
        out = parse_review(body)
        assert out.approved is True
        assert len(out.parse_warnings) == 1
        assert "stripped non-JSON text around review JSON" in out.parse_warnings[0]


# ── Schema rejections (raw JSON) ─────────────────────────────────────────────

class TestSchemaErrors:
    @pytest.mark.parametrize(
        "mutator",
        [
            lambda p: p.pop("short_summary"),
            lambda p: p.update({"short_summary": ""}),
            lambda p: p.update({"verdict": "MAYBE"}),
        ],
    )
    def test_approved_invalid(self, mutator):
        payload = _approved_payload()
        mutator(payload)
        with pytest.raises(ReviewSchemaError):
            parse_review(json.dumps(payload))

    def test_approved_with_findings_rejected(self):
        payload = _approved_payload(findings=[{
            "id": "F1", "severity": "P3", "title": "x", "body": "y",
        }])
        with pytest.raises(ReviewSchemaError):
            parse_review(json.dumps(payload))

    def test_rejected_with_empty_findings(self):
        payload = _rejected_payload(findings=[])
        with pytest.raises(ReviewSchemaError):
            parse_review(json.dumps(payload))

    def test_rejected_finding_missing_required_fix(self):
        payload = _rejected_payload()
        payload["findings"][0].pop("required_fix")
        with pytest.raises(ReviewSchemaError):
            parse_review(json.dumps(payload))

    def test_invalid_severity(self):
        payload = _rejected_payload()
        payload["findings"][0]["severity"] = "P9"
        with pytest.raises(ReviewSchemaError):
            parse_review(json.dumps(payload))

    def test_invalid_line_type(self):
        payload = _rejected_payload()
        payload["findings"][0]["line"] = -1
        with pytest.raises(ReviewSchemaError):
            parse_review(json.dumps(payload))

    def test_malformed_raw_json_fails_hard(self):
        with pytest.raises(ReviewParseError):
            parse_review("{not valid json")

    @pytest.mark.parametrize("raw", ["[]", "null", "true", "false", "[1, 2, 3]"])
    def test_non_object_json_root_fails_hard(self, raw: str):
        with pytest.raises(ReviewParseError):
            parse_review(raw)

    def test_malformed_fenced_json_fails_hard(self):
        body = "before\n```json\n{ broken json\n```\nafter"
        with pytest.raises(ReviewParseError):
            parse_review(body)

    def test_fenced_json_schema_invalid_fails_hard(self):
        payload = _approved_payload()
        payload.pop("short_summary")
        body = "context\n```json\n" + json.dumps(payload) + "\n```\n"
        with pytest.raises(ReviewParseError):
            parse_review(body)


# ── Non-JSON contract violations ─────────────────────────────────────────────

class TestNonJsonContractViolations:
    @pytest.mark.parametrize(
        "raw",
        [
            "All good.\nVERDICT: APPROVED",
            "LGTM, ship it.",
            "No substantive defects were found in the uncommitted diff.",
            "Missing rollback.\nVERDICT: REJECTED",
        ],
    )
    def test_prose_outputs_rejected(self, raw: str):
        with pytest.raises(ReviewParseError):
            parse_review(raw)


# ── Schema validator direct exercises ────────────────────────────────────────

class TestValidator:
    def test_returns_dict_on_success(self):
        payload = _approved_payload()
        assert validate_review_dict(payload) is payload

    def test_summary_length_boundary(self):
        ok = _approved_payload(short_summary="x" * REVIEW_SUMMARY_MAX_CHARS)
        validate_review_dict(ok)

    def test_summary_over_limit_is_auto_trimmed(self):
        # `short_summary` is a display-only field (CLI / dashboards / markdown
        # heading). Overflow must not abort the run — the validator trims
        # in place with a trailing ellipsis so the schema invariant holds.
        payload = _approved_payload(short_summary="x" * (REVIEW_SUMMARY_MAX_CHARS + 50))
        validate_review_dict(payload)
        assert len(payload["short_summary"]) == REVIEW_SUMMARY_MAX_CHARS
        assert payload["short_summary"].endswith("…")

    def test_non_dict(self):
        with pytest.raises(ReviewSchemaError):
            validate_review_dict([])


class TestParseWarnings:
    def test_short_summary_overflow_emits_warning(self):
        payload = _approved_payload(
            short_summary="x" * (REVIEW_SUMMARY_MAX_CHARS + 7)
        )
        out = parse_review(json.dumps(payload))
        assert len(out.parse_warnings) == 1
        assert "short_summary" in out.parse_warnings[0]
        assert out.approved is True  # Run continues.
        assert len(out.short_summary) == REVIEW_SUMMARY_MAX_CHARS

    def test_no_warning_when_summary_within_limit(self):
        out = parse_review(json.dumps(_approved_payload()))
        assert out.parse_warnings == ()
