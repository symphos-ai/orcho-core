"""Typed command-receipt failure classification."""

from __future__ import annotations

import pytest

from pipeline.evidence.verification_receipt import command_receipt_passed
from pipeline.verification_failure import classify_receipt, format_receipt_failure


def _receipt(**overrides: object) -> dict[str, object]:
    receipt: dict[str, object] = {
        "exit_code": 0,
        "assertions": [],
        "detail": "",
        "git": {"changed_files_fingerprint": "same", "checkout_head": "head"},
    }
    receipt.update(overrides)
    return receipt


@pytest.mark.parametrize(
    ("receipt", "fingerprint", "head", "status", "failure_kind"),
    [
        (_receipt(), "same", "head", "present", None),
        (None, "same", "head", "missing", "missing"),
        (_receipt(exit_code=2), "same", "head", "failed", "test_failure"),
        (
            _receipt(
                assertions=[
                    {
                        "name": "pipeline",
                        "kind": "import_path_equals",
                        "expected": "/work/pipeline/__init__.py",
                        "actual": "/installed/pipeline/__init__.py",
                        "passed": False,
                    }
                ]
            ),
            "same",
            "head",
            "failed",
            "provenance_failure",
        ),
        (_receipt(exit_code=None), "same", "head", "failed", "env_failure"),
        (_receipt(detail="subprocess could not start"), "same", "head", "failed", "env_failure"),
        (_receipt(), "other", "head", "stale", "stale"),
    ],
)
def test_classifies_all_receipt_outcomes(
    receipt: dict[str, object] | None,
    fingerprint: str,
    head: str,
    status: str,
    failure_kind: str | None,
) -> None:
    result = classify_receipt(
        receipt,
        current_fingerprint=fingerprint,
        current_head=head,
    )

    assert result.status == status
    assert result.failure_kind == failure_kind
    # ``command_receipt_passed`` intentionally has no checkout identity, so a
    # stale but otherwise valid receipt remains execution-passed there.
    assert command_receipt_passed(receipt) is (status in {"present", "stale"})


def test_failed_import_assertion_has_structured_evidence_and_compact_output() -> None:
    receipt = _receipt(
        assertions=[
            {
                "name": "pipeline",
                "kind": "import_path_equals",
                "expected": "/work/pipeline/__init__.py",
                "actual": "/installed/pipeline/__init__.py",
                "passed": False,
            }
        ],
        stdout_tail="noise\nuseful stdout",
        stderr_tail="first error\nactual import came from installed tree",
    )
    result = classify_receipt(receipt)

    assert result.failure_kind == "provenance_failure"
    assert (result.assertions_total, result.assertions_passed, result.assertions_failed) == (
        1,
        0,
        1,
    )
    assert result.failed_assertions[0].expected == "/work/pipeline/__init__.py"
    assert result.failed_assertions[0].actual == "/installed/pipeline/__init__.py"
    rendered = format_receipt_failure(result, receipt, max_output_chars=20)
    assert "class=provenance_failure" in rendered
    assert "kind=import_path_equals" in rendered
    assert "actual='/installed/pipeline/__init__.py'" in rendered
    assert "output=actual import came f" in rendered


def test_truthy_external_assertion_remains_passing() -> None:
    """Classifier matches the legacy command receipt truthiness rollup."""
    receipt = _receipt(assertions=[{"name": "third_party", "passed": 1}])

    result = classify_receipt(receipt)

    assert result.status == "present"
    assert result.failure_kind is None
    assert command_receipt_passed(receipt) is True
