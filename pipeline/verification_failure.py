# SPDX-License-Identifier: Apache-2.0
"""Typed classification and compact evidence for command verification receipts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pipeline.evidence.verification_receipt import subject_identity
from pipeline.verification_subject import (
    VerificationSubjectAvailable,
    VerificationSubjectIdentity,
    compare_verification_subjects,
)

FailureKind = Literal[
    "test_failure",
    "provenance_failure",
    "env_failure",
    "stale",
    "unverifiable",
    "missing",
]

__all__ = [
    "FailureKind",
    "FailedAssertion",
    "ReceiptClassification",
    "classify_receipt",
    "failed_receipt_refresh_eligible",
    "format_receipt_failure",
]


@dataclass(frozen=True)
class FailedAssertion:
    """A normalized failed assertion retained as bounded receipt evidence."""

    name: str
    kind: str
    expected: Any = None
    actual: Any = None


@dataclass(frozen=True)
class ReceiptClassification:
    """One receipt's stable status plus a typed failure and receipt evidence.

    ``status`` intentionally preserves the public readiness vocabulary
    (``present`` / ``missing`` / ``failed`` / ``stale``).  ``failure_kind``
    distinguishes why a non-present result occurred without widening that
    vocabulary.
    """

    status: Literal["present", "missing", "failed", "stale", "unverifiable"]
    failure_kind: FailureKind | None = None
    reason: str = ""
    source_run_id: str = ""
    path: str = ""
    exit_code: int | None = None
    assertions_total: int = 0
    assertions_passed: int = 0
    assertions_failed: int = 0
    failed_assertions: tuple[FailedAssertion, ...] = ()


def _receipt_evidence(
    receipt: Mapping[str, Any] | None,
) -> tuple[int | None, int, int, int, tuple[FailedAssertion, ...]]:
    if not isinstance(receipt, Mapping):
        return None, 0, 0, 0, ()
    raw_exit = receipt.get("exit_code")
    exit_code = raw_exit if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else None
    raw_assertions = receipt.get("assertions")
    assertions = raw_assertions if isinstance(raw_assertions, list) else []
    normalized = [item for item in assertions if isinstance(item, Mapping)]
    failed = tuple(
        FailedAssertion(
            name=str(item.get("name") or ""),
            kind=str(item.get("kind") or ""),
            expected=item.get("expected"),
            actual=item.get("actual"),
        )
        for item in normalized
        if not item.get("passed", False)
    )
    return exit_code, len(normalized), len(normalized) - len(failed), len(failed), failed


def _assertion_failure_kind(failed: tuple[FailedAssertion, ...]) -> FailureKind:
    provenance_kinds = {"import_path_equals", "import_path_under"}
    if any(assertion.kind in provenance_kinds for assertion in failed):
        return "provenance_failure"
    return "env_failure"


def classify_receipt(
    receipt: Mapping[str, Any] | None,
    *,
    current_subject: VerificationSubjectIdentity | VerificationSubjectAvailable | None = None,
    dependency_subjects: Mapping[str, VerificationSubjectIdentity | VerificationSubjectAvailable | None] | None = None,
) -> ReceiptClassification:
    """Classify a receipt before checking freshness.

    Execution and assertion evidence wins over staleness: a non-zero exit is a
    test failure; a missing exit code or execution detail is an environment
    failure; and an exit-0 failed assertion is provenance/environment failure.
    """
    exit_code, total, passed, failed_count, failed = _receipt_evidence(receipt)
    evidence = dict(
        exit_code=exit_code,
        assertions_total=total,
        assertions_passed=passed,
        assertions_failed=failed_count,
        failed_assertions=failed,
    )
    if receipt is None:
        return ReceiptClassification("missing", "missing", **evidence)
    if exit_code is None:
        return ReceiptClassification(
            "failed", "env_failure", "command execution did not report an exit code", **evidence
        )
    if exit_code != 0:
        return ReceiptClassification(
            "failed", "test_failure", f"command exited {exit_code}", **evidence
        )
    if failed:
        kind = _assertion_failure_kind(failed)
        return ReceiptClassification(
            "failed", kind, "declared verification assertion failed", **evidence
        )
    if str(receipt.get("detail") or "").strip():
        return ReceiptClassification(
            "failed", "env_failure", "command execution detail reported", **evidence
        )

    recorded_subject = _recorded_identity(receipt.get("subject"))
    current_identity = _identity(current_subject)
    comparison = compare_verification_subjects(recorded_subject, current_identity)
    if comparison.verdict.value == "unverifiable":
        return ReceiptClassification("unverifiable", "unverifiable", comparison.reason, **evidence)
    if comparison.verdict.value == "stale":
        return ReceiptClassification("stale", "stale", comparison.reason, **evidence)
    dependencies = receipt.get("dependencies")
    for entry in dependencies if isinstance(dependencies, list) else []:
        if not isinstance(entry, Mapping) or not entry.get("depends_on"):
            continue
        name = str(entry.get("name") or "")
        dep_comparison = compare_verification_subjects(
            _recorded_identity(entry.get("subject")), _identity((dependency_subjects or {}).get(name)),
        )
        if dep_comparison.verdict.value == "unverifiable":
            return ReceiptClassification("unverifiable", "unverifiable", f"dependency {name}: {dep_comparison.reason}", **evidence)
        if dep_comparison.verdict.value == "stale":
            return ReceiptClassification("stale", "stale", f"dependency {name}: {dep_comparison.reason}", **evidence)
    return ReceiptClassification("present", None, **evidence)


def _identity(value: VerificationSubjectIdentity | VerificationSubjectAvailable | None) -> VerificationSubjectIdentity | None:
    return value.identity if isinstance(value, VerificationSubjectAvailable) else value


def _recorded_identity(value: Any) -> VerificationSubjectIdentity | None:
    return _identity(value) if isinstance(value, (VerificationSubjectIdentity, VerificationSubjectAvailable)) else subject_identity(value)


def failed_receipt_refresh_eligible(
    receipt: Mapping[str, Any] | None,
    *,
    current_subject: VerificationSubjectIdentity | VerificationSubjectAvailable | None,
) -> bool:
    """Whether a failed current-run receipt may be refreshed once.

    Execution failure remains ``failed`` regardless of provenance. Automatic
    refresh is permitted only when usable typed subjects prove the failed
    execution covered different content; legacy, malformed, and unavailable
    subjects therefore fail closed.
    """
    if classify_receipt(receipt, current_subject=current_subject).status != "failed":
        return False
    comparison = compare_verification_subjects(
        _recorded_identity(receipt.get("subject")) if receipt is not None else None,
        _identity(current_subject),
    )
    return comparison.verdict.value == "stale"


def _last_meaningful_line(receipt: Mapping[str, Any]) -> str:
    for key in ("stderr_tail", "stdout_tail"):
        raw = str(receipt.get(key) or "")
        for line in reversed(raw.splitlines()):
            if line.strip():
                return line.strip()
    return ""


def format_receipt_failure(
    classification: ReceiptClassification,
    receipt: Mapping[str, Any] | None,
    *,
    max_output_chars: int = 240,
) -> str:
    """Render compact receipt-only failure evidence for handoffs and logs."""
    parts = [
        f"class={classification.failure_kind or classification.status}",
        f"exit_code={classification.exit_code}",
        "assertions="
        + f"{classification.assertions_passed}/{classification.assertions_total} passed",
    ]
    if classification.failed_assertions:
        assertion = classification.failed_assertions[0]
        parts.append(
            "failed_assertion="
            f"name={assertion.name or '<unnamed>'} kind={assertion.kind or '<unknown>'} "
            f"expected={assertion.expected!r} actual={assertion.actual!r}"
        )
    if isinstance(receipt, Mapping):
        line = _last_meaningful_line(receipt)
        if line:
            parts.append(f"output={line[:max_output_chars]}")
    return "; ".join(parts)
