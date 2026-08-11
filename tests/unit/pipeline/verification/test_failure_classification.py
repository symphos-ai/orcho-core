from __future__ import annotations

import pytest

from pipeline.evidence.verification_receipt import command_receipt_passed
from pipeline.verification_failure import (
    classify_receipt,
    failed_receipt_refresh_eligible,
)
from pipeline.verification_subject import VerificationSubjectIdentity


def _subject(tree: str = "a", head: str = "b") -> dict:
    return {"status": "available", "identity": {"version": 1, "object_format": "sha1", "tree_oid": tree * 40, "observed_head_oid": head * 40, "baseline_oid": None}}


def _receipt(**overrides: object) -> dict:
    value = {"schema_version": 3, "exit_code": 0, "assertions": [], "detail": "", "subject": _subject(), "dependencies": []}
    value.update(overrides)
    return value


def test_v2_success_without_subject_is_unverifiable() -> None:
    receipt = {"schema_version": 2, "exit_code": 0, "assertions": [], "detail": "", "git": {"checkout_head": "same", "changed_files_fingerprint": "same"}}
    assert classify_receipt(receipt, current_subject=VerificationSubjectIdentity(1, "sha1", "a" * 40, "b" * 40, None)).status == "unverifiable"


def test_head_drift_is_stale_even_when_tree_matches() -> None:
    current = VerificationSubjectIdentity(1, "sha1", "a" * 40, "c" * 40, None)
    assert classify_receipt(_receipt(), current_subject=current).status == "stale"


def test_execution_failure_precedes_subject_verification() -> None:
    assert classify_receipt(_receipt(exit_code=2), current_subject=None).failure_kind == "test_failure"


@pytest.mark.parametrize(
    ("receipt", "current", "eligible"),
    [
        (_receipt(exit_code=1), VerificationSubjectIdentity(1, "sha1", "a" * 40, "c" * 40, None), True),
        (_receipt(exit_code=1), VerificationSubjectIdentity(1, "sha1", "a" * 40, "b" * 40, None), False),
        (_receipt(exit_code=1), None, False),
        (_receipt(exit_code=1, subject={"status": "unavailable"}), VerificationSubjectIdentity(1, "sha1", "a" * 40, "c" * 40, None), False),
        (_receipt(exit_code=1, subject={"status": "available", "identity": {}}), VerificationSubjectIdentity(1, "sha1", "a" * 40, "c" * 40, None), False),
        ({"schema_version": 2, "exit_code": 1, "assertions": [], "detail": ""}, VerificationSubjectIdentity(1, "sha1", "a" * 40, "c" * 40, None), False),
        (_receipt(), VerificationSubjectIdentity(1, "sha1", "a" * 40, "c" * 40, None), False),
    ],
)
def test_failed_receipt_refresh_requires_failed_execution_and_proven_stale_subject(
    receipt: dict, current: VerificationSubjectIdentity | None, eligible: bool,
) -> None:
    assert failed_receipt_refresh_eligible(receipt, current_subject=current) is eligible


@pytest.mark.parametrize(
    ("receipt", "status", "failure_kind"),
    [
        (_receipt(), "present", None),
        (None, "missing", "missing"),
        (_receipt(exit_code=2), "failed", "test_failure"),
        (_receipt(exit_code=None), "failed", "env_failure"),
        (_receipt(exit_code=None, outcome="timeout", detail="command timed out after 900s"), "failed", "timeout"),
        (_receipt(exit_code=None, outcome="error", detail="subprocess error: boom"), "failed", "env_failure"),
        (_receipt(detail="subprocess could not start"), "failed", "env_failure"),
        (_receipt(assertions=[{"name": "pipeline", "kind": "import_path_equals", "passed": False}]), "failed", "provenance_failure"),
    ],
)
def test_classifies_all_receipt_outcomes(
    receipt: dict | None, status: str, failure_kind: str | None,
) -> None:
    """Execution and assertion outcomes retain priority over subject freshness."""
    current = VerificationSubjectIdentity(1, "sha1", "a" * 40, "b" * 40, None)
    result = classify_receipt(receipt, current_subject=current)

    assert result.status == status
    assert result.failure_kind == failure_kind
    assert command_receipt_passed(receipt) is (status == "present")


def test_timeout_reason_carries_the_executor_detail() -> None:
    """The budget line is the only actionable fact a timed-out receipt has —
    it must survive into the classification reason and the formatted evidence
    (the receipt's output tails are empty by construction)."""
    from pipeline.verification_failure import format_receipt_failure

    receipt = _receipt(
        exit_code=None, outcome="timeout",
        detail="command timed out after 900s",
        stdout_tail="", stderr_tail="",
    )
    result = classify_receipt(receipt, current_subject=None)

    assert result.failure_kind == "timeout"
    assert result.reason == "command timed out after 900s"
    evidence = format_receipt_failure(result, receipt)
    assert "class=timeout" in evidence
    assert "command timed out after 900s" in evidence


def test_pre_outcome_receipt_without_exit_code_stays_env_failure() -> None:
    """A receipt written before the ``outcome`` field existed still classifies:
    no outcome + no exit code keeps the untyped env_failure path."""
    receipt = _receipt(exit_code=None, detail="command timed out after 600s")
    result = classify_receipt(receipt, current_subject=None)

    assert result.failure_kind == "env_failure"
    # but the detail is no longer discarded in favour of boilerplate
    assert result.reason == "command timed out after 600s"
