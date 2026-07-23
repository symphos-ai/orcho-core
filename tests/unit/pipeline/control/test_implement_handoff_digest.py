"""Classifier + renderer for the implement-incomplete handoff digest (T1)."""

from __future__ import annotations

from core.io.ansi import strip_ansi
from pipeline.control.implement_handoff_digest import (
    ImplementIncompleteDigest,
    classify_implement_incomplete,
    render_implement_incomplete_digest,
)

_ACTIONS = ("retry_feedback", "continue_with_waiver", "halt")


class TestClassifier:
    def test_real_gap_recommends_retry_feedback(self) -> None:
        artifacts = {
            "incomplete_subtasks": ["T2-wire"],
            "attestation_incomplete": {"T2-wire": "criterion 3: tests not added"},
            "missing_subtask_receipts": [],
        }
        digest = classify_implement_incomplete(artifacts, "", _ACTIONS)
        assert digest.incomplete_subtasks == ("T2-wire",)
        assert digest.unmet_criteria == (
            ("T2-wire", "criterion 3: tests not added"),
        )
        assert digest.is_verification_exception is False
        assert digest.recommended_action == "retry_feedback"

    def test_baseline_marker_in_reason_recommends_waiver(self) -> None:
        artifacts = {
            "incomplete_subtasks": ["T1-mod"],
            "attestation_incomplete": {
                "T1-mod": "test_foo red but baseline also red, unrelated",
            },
            "missing_subtask_receipts": [],
        }
        digest = classify_implement_incomplete(artifacts, "", _ACTIONS)
        assert digest.is_verification_exception is True
        assert digest.recommended_action == "continue_with_waiver"

    def test_baseline_marker_in_last_output_recommends_waiver(self) -> None:
        artifacts = {
            "incomplete_subtasks": ["T1-mod"],
            "attestation_incomplete": {"T1-mod": "criteria not closed"},
            "missing_subtask_receipts": [],
        }
        last_output = "Note: the failing suite is pre-existing on main."
        digest = classify_implement_incomplete(artifacts, last_output, _ACTIONS)
        assert digest.is_verification_exception is True
        assert digest.recommended_action == "continue_with_waiver"

    def test_baseline_exception_without_waiver_action_falls_back_to_retry(self) -> None:
        artifacts = {
            "incomplete_subtasks": ["T1-mod"],
            "attestation_incomplete": {"T1-mod": "baseline-identical failure"},
            "missing_subtask_receipts": [],
        }
        digest = classify_implement_incomplete(
            artifacts, "", ("retry_feedback", "halt"),
        )
        assert digest.is_verification_exception is True
        assert digest.recommended_action == "retry_feedback"

    def test_missing_receipts_captured(self) -> None:
        artifacts = {
            "incomplete_subtasks": [],
            "attestation_incomplete": {},
            "missing_subtask_receipts": ["T3-x", "T4-y"],
        }
        digest = classify_implement_incomplete(artifacts, "", _ACTIONS)
        assert digest.missing_receipts == ("T3-x", "T4-y")
        assert digest.recommended_action == "retry_feedback"

    def test_malformed_artifacts_degrade_to_empty(self) -> None:
        digest = classify_implement_incomplete({}, "", _ACTIONS)
        assert digest.incomplete_subtasks == ()
        assert digest.unmet_criteria == ()
        assert digest.unmet_evidence == ()
        assert digest.repaired_gates == ()
        assert digest.missing_receipts == ()
        assert digest.is_verification_exception is False
        assert digest.is_environment_blocker is False

    def test_environment_evidence_recommends_halt_not_blind_retry(self) -> None:
        artifacts = {
            "incomplete_subtasks": ["T2-route"],
            "attestation_incomplete": {
                "T2-route": "done_criteria not met (by index): [2]",
            },
            "unmet_done_criteria": [
                {
                    "subtask_id": "T2-route",
                    "index": 2,
                    "criterion": "Functional route test passes.",
                    "evidence": (
                        "Functional execution is blocked because PostgreSQL "
                        "port 5433 is already allocated."
                    ),
                },
            ],
        }
        digest = classify_implement_incomplete(artifacts, "", _ACTIONS)
        assert digest.is_environment_blocker is True
        assert digest.recommended_action == "halt"
        assert digest.unmet_evidence == ((
            "T2-route",
            2,
            "Functional route test passes.",
            "Functional execution is blocked because PostgreSQL port 5433 "
            "is already allocated.",
        ),)

    def test_gate_repair_context_is_kept_separate_from_blocker(self) -> None:
        artifacts = {
            "incomplete_subtasks": ["T2-route"],
            "attestation_incomplete": {"T2-route": "criterion 2 unverified"},
            "post_phase_gate_repair": {
                "status": "passed",
                "commands": ["cs", "rector"],
            },
        }
        digest = classify_implement_incomplete(artifacts, "", _ACTIONS)
        assert digest.repaired_gates == ("cs", "rector")
        assert digest.unmet_criteria == (("T2-route", "criterion 2 unverified"),)


class TestRenderer:
    def test_shows_subtasks_criteria_and_recommendation(self) -> None:
        digest = classify_implement_incomplete(
            {
                "incomplete_subtasks": ["T2-wire"],
                "attestation_incomplete": {"T2-wire": "criterion 3: tests not added"},
                "missing_subtask_receipts": ["T5-z"],
            },
            "",
            _ACTIONS,
        )
        text = strip_ansi(render_implement_incomplete_digest(digest))
        assert "Why paused" in text
        assert "Subtask: T2-wire" in text
        assert "Missing: T2-wire — criterion 3: tests not added" in text
        assert "Missing receipts: T5-z" in text
        assert "Recommended: retry_feedback" in text
        # Real gap: no verification-exception note.
        assert "verification exception" not in text

    def test_verification_exception_note_and_waiver_recommendation(self) -> None:
        digest = classify_implement_incomplete(
            {
                "incomplete_subtasks": ["T1-mod"],
                "attestation_incomplete": {"T1-mod": "baseline also red"},
                "missing_subtask_receipts": [],
            },
            "",
            _ACTIONS,
        )
        text = strip_ansi(render_implement_incomplete_digest(digest))
        assert "baseline / pre-existing, unrelated to this diff" in text
        assert "Recommended: continue_with_waiver" in text
        assert "not a dirty override" in text

    def test_does_not_crash_on_empty_artifacts(self) -> None:
        digest = classify_implement_incomplete({}, "", _ACTIONS)
        text = strip_ansi(render_implement_incomplete_digest(digest))
        assert "Why paused" in text
        # Still offers a recommendation even with no structured detail.
        assert "Recommended: retry_feedback" in text

    def test_render_accepts_digest_dataclass_directly(self) -> None:
        digest = ImplementIncompleteDigest(
            incomplete_subtasks=("A",),
            unmet_criteria=(("A", "reason"),),
            unmet_evidence=(),
            missing_receipts=(),
            repaired_gates=(),
            is_verification_exception=False,
            is_environment_blocker=False,
            recommended_action="retry_feedback",
        )
        text = strip_ansi(render_implement_incomplete_digest(digest, color=False))
        assert "Subtask: A" in text

    def test_environment_blocker_explains_green_repair_then_handoff(self) -> None:
        digest = classify_implement_incomplete(
            {
                "incomplete_subtasks": ["T2-route"],
                "attestation_incomplete": {
                    "T2-route": "done_criteria not met (by index): [2]",
                },
                "unmet_done_criteria": [
                    {
                        "subtask_id": "T2-route",
                        "index": 2,
                        "criterion": "Functional route test passes.",
                        "evidence": "PostgreSQL port 5433 is already allocated.",
                    },
                ],
                "post_phase_gate_repair": {
                    "status": "passed",
                    "commands": ["cs"],
                },
            },
            "",
            _ACTIONS,
        )
        text = strip_ansi(render_implement_incomplete_digest(digest))
        assert "Gate repair passed: cs" in text
        assert "Separate blocker remains" in text
        assert "Functional route test passes." in text
        assert "PostgreSQL port 5433 is already allocated." in text
        assert "Recommended: halt" in text
        assert "another implementation retry cannot repair the environment" in text
