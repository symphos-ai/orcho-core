# SPDX-License-Identifier: Apache-2.0
"""ADR 0188 — the reviewer contract can express a typed criterion link.

A criterion link is only real if the *published* output contract lets an author
emit it, the schema validates it, the parser keeps it, and a real phase writer
persists it. This module pins all four.
"""
from __future__ import annotations

import json

import pytest

from core.contracts.release_schema import (
    RELEASE_BLOCKER_OPTIONAL_KEYS,
    RELEASE_SCHEMA_DOC,
    ReleaseSchemaError,
    validate_release_dict,
)
from core.contracts.review_schema import (
    FINDING_OPTIONAL_KEYS,
    REVIEW_SCHEMA_DOC,
    ReviewSchemaError,
    validate_review_dict,
)
from pipeline.release_parser import parse_release
from pipeline.review_parser import parse_review


def _review(**finding_extra):
    return {
        "verdict": "REJECTED",
        "short_summary": "s",
        "findings": [{
            "id": "F1", "severity": "P1", "title": "t", "body": "b",
            "required_fix": "fix", **finding_extra,
        }],
    }


def _release(**blocker_extra):
    return {
        "verdict": "REJECTED",
        "ship_ready": False,
        "short_summary": "s",
        "release_blockers": [{
            "id": "R1", "severity": "P1", "title": "t", "body": "b",
            "required_fix": "fix", "why_blocks_release": "w", **blocker_extra,
        }],
        "verification_gaps": [],
        "contract_status": {
            "task_contract": "incomplete", "interfaces": "not_applicable",
            "persistence": "not_applicable", "tests": "sufficient",
        },
    }


class TestPublishedContract:
    def test_criterion_id_is_an_advertised_optional_finding_key(self) -> None:
        assert "criterion_id" in FINDING_OPTIONAL_KEYS
        assert "criterion_id" in RELEASE_BLOCKER_OPTIONAL_KEYS

    def test_both_schema_docs_teach_the_link(self) -> None:
        for doc in (REVIEW_SCHEMA_DOC, RELEASE_SCHEMA_DOC):
            assert '"criterion_id"' in doc
            assert "links the" in doc
            assert "never a restatement" in doc

    def test_the_review_doc_says_a_finding_never_proves_an_executable(
        self,
    ) -> None:
        assert "never proves an `executable` criterion" in REVIEW_SCHEMA_DOC


class TestSchemaValidation:
    def test_a_well_formed_link_validates(self) -> None:
        validate_review_dict(_review(criterion_id="C2"))
        validate_release_dict(_release(criterion_id="C2"))

    def test_an_absent_link_still_validates(self) -> None:
        validate_review_dict(_review())
        validate_release_dict(_release())

    @pytest.mark.parametrize("value", ["", "   ", "bogus", "c2", "C0", 3])
    def test_a_malformed_link_is_rejected(self, value) -> None:
        with pytest.raises(ReviewSchemaError):
            validate_review_dict(_review(criterion_id=value))
        with pytest.raises(ReleaseSchemaError):
            validate_release_dict(_release(criterion_id=value))

    def test_an_explicit_null_link_is_treated_as_absent(self) -> None:
        validate_review_dict(_review(criterion_id=None))
        validate_release_dict(_release(criterion_id=None))


class TestParserRoundTrip:
    def test_the_review_parser_keeps_and_omits_the_link(self) -> None:
        linked = parse_review(json.dumps(_review(criterion_id="C2")))
        assert linked.findings[0].criterion_id == "C2"
        assert linked.findings_as_dicts()[0]["criterion_id"] == "C2"

        bare = parse_review(json.dumps(_review()))
        assert bare.findings[0].criterion_id is None
        assert "criterion_id" not in bare.findings_as_dicts()[0]

    def test_the_release_parser_keeps_the_link_on_both_projections(self) -> None:
        parsed = parse_release(json.dumps(_release(criterion_id="C2")))
        blocker = parsed.release_blockers[0]
        assert blocker.criterion_id == "C2"
        assert blocker.to_dict()["criterion_id"] == "C2"
        # The review-shape mirror is what lands in the durable phase entry.
        assert blocker.to_finding_dict()["criterion_id"] == "C2"

    def test_an_unlinked_blocker_omits_the_key_everywhere(self) -> None:
        blocker = parse_release(json.dumps(_release())).release_blockers[0]
        assert "criterion_id" not in blocker.to_dict()
        assert "criterion_id" not in blocker.to_finding_dict()


class TestDurableMirror:
    """The link is also written to the run's claim log by a real phase."""

    def test_linked_findings_are_mirrored_once_and_only_once(
        self, tmp_path,
    ) -> None:
        from pipeline.criterion_claims import (
            load_criterion_claims,
            record_finding_links,
        )

        findings = [
            {"id": "R1", "criterion_id": "C2"},
            {"id": "R2"},
        ]
        first = record_finding_links(
            tmp_path, run_id="20260101_000000", findings=findings,
            actor="reviewer",
        )
        assert [r.claim_id for r in first] == ["R1"]
        assert first[0].kind == "finding"
        # Re-running the phase must not grow the log.
        assert record_finding_links(
            tmp_path, run_id="20260101_000000", findings=findings,
            actor="reviewer",
        ) == []
        assert [r.claim_id for r in load_criterion_claims(tmp_path)] == ["R1"]

    def test_no_linked_finding_writes_no_artifact(self, tmp_path) -> None:
        from pipeline.criterion_claims import claims_path, record_finding_links

        assert record_finding_links(
            tmp_path, run_id="20260101_000000",
            findings=[{"id": "R2"}], actor="reviewer",
        ) == []
        assert not claims_path(tmp_path).exists()

    def test_the_mirror_never_double_counts_in_the_reducer(self, tmp_path) -> None:
        from pipeline.criterion_claims import record_finding_links, reducer_claims

        record_finding_links(
            tmp_path, run_id="20260101_000000",
            findings=[{"id": "R1", "criterion_id": "C2"}], actor="reviewer",
        )
        facts = reducer_claims(
            tmp_path, findings=[{"id": "R1", "criterion_id": "C2"}],
        )
        assert [(f.kind, f.id) for f in facts] == [("finding", "R1")]
