"""Release-gate schema + parser (ADR 0025 Phase 1)."""
from __future__ import annotations

import json

import pytest

from core.contracts.release_schema import (
    RELEASE_SUMMARY_MAX_CHARS,
    ReleaseSchemaError,
    validate_release_dict,
)
from pipeline.release_parser import (
    ParsedRelease,
    ReleaseBlocker,
    ReleaseParseError,
    parse_release,
)

# ── Schema fixtures ──────────────────────────────────────────────────────────

def _approved() -> dict:
    return {
        "verdict":            "APPROVED",
        "ship_ready":         True,
        "short_summary":      "Ready to ship.",
        "release_blockers":   [],
        "verification_gaps":  [],
        "contract_status": {
            "task_contract": "satisfied",
            "interfaces":    "compatible",
            "persistence":   "safe",
            "tests":         "sufficient",
        },
    }


def _rejected_with_blocker() -> dict:
    return {
        "verdict":       "REJECTED",
        "ship_ready":    False,
        "short_summary": "P0 contract regression.",
        "release_blockers": [{
            "id":                 "R1",
            "severity":           "P0",
            "title":              "Caller contract broken",
            "body":               "Callers depend on the old signature.",
            "required_fix":       "Restore the signature or migrate callers.",
            "why_blocks_release": "Production callers crash on the old shape.",
        }],
        "verification_gaps":  [],
        "contract_status": {
            "task_contract": "incomplete",
            "interfaces":    "broken",
            "persistence":   "safe",
            "tests":         "weak",
        },
    }


# ── Schema validation ────────────────────────────────────────────────────────

class TestReleaseSchemaApproved:
    def test_clean_approved_validates(self) -> None:
        assert validate_release_dict(_approved()) == _approved()

    def test_rejected_with_blocker_validates(self) -> None:
        validate_release_dict(_rejected_with_blocker())

    def test_rejected_with_only_gap_validates(self) -> None:
        payload = {
            "verdict":            "REJECTED",
            "ship_ready":         False,
            "short_summary":      "Missing verification of P0 path.",
            "release_blockers":   [],
            "verification_gaps":  [{
                "risk":             "Concurrent writes corrupt state",
                "missing_evidence": "No concurrency test exists",
                "required_check":   "Add a multi-writer integration test",
            }],
            "contract_status": {
                "task_contract": "unclear",
                "interfaces":    "compatible",
                "persistence":   "risky",
                "tests":         "missing",
            },
        }
        validate_release_dict(payload)


class TestReleaseSchemaCoherence:
    def test_approved_with_blocker_rejected(self) -> None:
        payload = _approved()
        payload["release_blockers"] = [{
            "id": "R1", "severity": "P1",
            "title": "x", "body": "x", "required_fix": "x",
            "why_blocks_release": "x",
        }]
        with pytest.raises(ReleaseSchemaError, match="empty when verdict is APPROVED"):
            validate_release_dict(payload)

    def test_approved_with_verification_gap_rejected_no_grey_zone(self) -> None:
        payload = _approved()
        payload["verification_gaps"] = [{
            "risk": "x", "missing_evidence": "x", "required_check": "x",
        }]
        with pytest.raises(ReleaseSchemaError, match="no grey zone"):
            validate_release_dict(payload)

    def test_rejected_with_empty_blockers_and_gaps_rejected(self) -> None:
        payload = _rejected_with_blocker()
        payload["release_blockers"] = []
        payload["verification_gaps"] = []
        with pytest.raises(ReleaseSchemaError, match="REJECTED verdict requires"):
            validate_release_dict(payload)

    def test_rejected_with_ship_ready_true_rejected(self) -> None:
        payload = _rejected_with_blocker()
        payload["ship_ready"] = True
        with pytest.raises(ReleaseSchemaError, match="ship_ready must be False"):
            validate_release_dict(payload)

    def test_approved_with_ship_ready_false_rejected(self) -> None:
        payload = _approved()
        payload["ship_ready"] = False
        with pytest.raises(ReleaseSchemaError, match="ship_ready must be True"):
            validate_release_dict(payload)


class TestReleaseSchemaEnums:
    def test_p3_severity_rejected(self) -> None:
        payload = _rejected_with_blocker()
        payload["release_blockers"][0]["severity"] = "P3"
        with pytest.raises(ReleaseSchemaError, match="severity must be one of"):
            validate_release_dict(payload)

    def test_unknown_contract_status_value_rejected(self) -> None:
        payload = _approved()
        payload["contract_status"]["interfaces"] = "weird"
        with pytest.raises(ReleaseSchemaError, match="contract_status.interfaces"):
            validate_release_dict(payload)

    def test_unknown_contract_status_key_rejected(self) -> None:
        payload = _approved()
        payload["contract_status"]["mystery"] = "x"
        with pytest.raises(ReleaseSchemaError, match="unknown keys"):
            validate_release_dict(payload)

    def test_missing_contract_status_key_rejected(self) -> None:
        payload = _approved()
        del payload["contract_status"]["tests"]
        with pytest.raises(ReleaseSchemaError, match="missing required keys"):
            validate_release_dict(payload)

    def test_approved_with_broken_interfaces_rejected(self) -> None:
        """When APPROVED, contract_status must be positive or
        not_applicable — a broken interface can't ship."""
        payload = _approved()
        payload["contract_status"]["interfaces"] = "broken"
        with pytest.raises(ReleaseSchemaError, match="incompatible with verdict=APPROVED"):
            validate_release_dict(payload)

    def test_approved_with_weak_tests_rejected(self) -> None:
        payload = _approved()
        payload["contract_status"]["tests"] = "weak"
        with pytest.raises(ReleaseSchemaError, match="incompatible with verdict=APPROVED"):
            validate_release_dict(payload)

    def test_approved_with_not_applicable_interfaces_accepted(self) -> None:
        payload = _approved()
        payload["contract_status"]["interfaces"] = "not_applicable"
        validate_release_dict(payload)


class TestReleaseSchemaRequiredKeys:
    def test_missing_verdict(self) -> None:
        payload = _approved()
        del payload["verdict"]
        with pytest.raises(ReleaseSchemaError, match="missing required keys"):
            validate_release_dict(payload)

    def test_missing_ship_ready(self) -> None:
        payload = _approved()
        del payload["ship_ready"]
        with pytest.raises(ReleaseSchemaError, match="missing required keys"):
            validate_release_dict(payload)

    def test_short_summary_overflow_is_auto_trimmed(self) -> None:
        # Display-only field — overflow must not abort the release gate.
        payload = _approved()
        payload["short_summary"] = "x" * (RELEASE_SUMMARY_MAX_CHARS + 12)
        validate_release_dict(payload)
        assert len(payload["short_summary"]) == RELEASE_SUMMARY_MAX_CHARS
        assert payload["short_summary"].endswith("…")

    def test_short_summary_overflow_emits_parse_warning(self) -> None:
        payload = _approved()
        payload["short_summary"] = "x" * (RELEASE_SUMMARY_MAX_CHARS + 5)
        parsed = parse_release(json.dumps(payload))
        assert parsed.approved is True
        assert len(parsed.parse_warnings) == 1
        assert "short_summary" in parsed.parse_warnings[0]


# ── Parser ────────────────────────────────────────────────────────────────────

class TestReleaseParser:
    def test_parse_approved(self) -> None:
        p = parse_release(json.dumps(_approved()))
        assert isinstance(p, ParsedRelease)
        assert p.approved is True
        assert p.ship_ready is True
        assert p.release_blockers == ()
        assert p.verification_gaps == ()
        assert p.contract_status.tests == "sufficient"

    def test_parse_rejected_blocker(self) -> None:
        p = parse_release(json.dumps(_rejected_with_blocker()))
        assert p.approved is False
        assert p.ship_ready is False
        assert len(p.release_blockers) == 1
        b = p.release_blockers[0]
        assert isinstance(b, ReleaseBlocker)
        assert b.id == "R1"
        assert b.severity == "P0"
        assert b.why_blocks_release.startswith("Production")

    def test_parse_fenced_json_recovers_with_warning(self) -> None:
        p = parse_release("```json\n" + json.dumps(_approved()) + "\n```")
        assert p.approved is True
        assert len(p.parse_warnings) == 1
        assert "stripped non-JSON text around release JSON" in p.parse_warnings[0]

    def test_parse_prefixed_json_recovers_with_warning(self) -> None:
        p = parse_release("Release review complete.\n" + json.dumps(_approved()))
        assert p.approved is True
        assert len(p.parse_warnings) == 1
        assert "stripped non-JSON text around release JSON" in p.parse_warnings[0]

    def test_parse_non_object_raises(self) -> None:
        with pytest.raises(ReleaseParseError, match="exactly one JSON object"):
            parse_release("[]")

    def test_parse_prose_raises(self) -> None:
        with pytest.raises(ReleaseParseError, match="exactly one JSON object"):
            parse_release("This is not JSON at all.")

    def test_parse_malformed_json_raises(self) -> None:
        with pytest.raises(ReleaseParseError, match="raw JSON parse failed"):
            parse_release("{ bad json")

    def test_parse_schema_violation_raises(self) -> None:
        bad = _approved()
        bad["ship_ready"] = False  # incoherent with APPROVED
        with pytest.raises(ReleaseSchemaError, match="ship_ready must be True"):
            parse_release(json.dumps(bad))

    def test_approved_iff_ship_ready(self) -> None:
        p = parse_release(json.dumps(_approved()))
        assert p.approved == p.ship_ready is True
        p2 = parse_release(json.dumps(_rejected_with_blocker()))
        assert p2.approved is False and p2.ship_ready is False


class TestReleaseBlockerProjection:
    def test_to_finding_dict_matches_review_finding_shape(self) -> None:
        """``ReleaseBlocker.to_finding_dict()`` must yield the exact
        dict shape the review parser produces — this is the contract
        the dual-shape mirror relies on.
        """
        b = ReleaseBlocker(
            id="R1", severity="P1", title="t", body="b",
            required_fix="rf", why_blocks_release="wbr",
            file="path.py", line=42,
        )
        d = b.to_finding_dict()
        # All review-finding required + optional keys are present;
        # release-only field (why_blocks_release) is dropped.
        assert d == {
            "id": "R1", "severity": "P1", "title": "t", "body": "b",
            "required_fix": "rf", "file": "path.py", "line": 42,
        }
        assert "why_blocks_release" not in d

    def test_to_finding_dict_omits_optional_when_absent(self) -> None:
        b = ReleaseBlocker(
            id="R2", severity="P2", title="t", body="b",
            required_fix="rf", why_blocks_release="wbr",
        )
        d = b.to_finding_dict()
        assert "file" not in d
        assert "line" not in d
