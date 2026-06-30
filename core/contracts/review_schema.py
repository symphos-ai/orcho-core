"""
core/contracts/review_schema.py — JSON schema for reviewer phase output.

Reviewer phases (``validate_plan``, ``review``, ``final_acceptance``) emit one JSON object.
That object is the machine ground truth; the parser validates it against the
schema below before Orcho drives control flow. Orcho then renders human-
readable review markdown deterministically from the parsed object.

Schema is dependency-free (no pydantic) so the core stays importable on a
bare stdlib install. Validation lives in :func:`validate_review_dict`.
"""
from __future__ import annotations

from typing import Any

REVIEW_SUMMARY_MAX_CHARS = 280
REVIEW_VERDICTS = ("APPROVED", "REJECTED")
REVIEW_SEVERITIES = ("P0", "P1", "P2", "P3")

REVIEW_REQUIRED_KEYS = ("verdict", "short_summary", "findings")
REVIEW_OPTIONAL_KEYS = ("risks", "checks")

FINDING_REQUIRED_KEYS = ("id", "severity", "title", "body")
FINDING_OPTIONAL_KEYS = ("required_fix", "file", "line")


class ReviewSchemaError(ValueError):
    """Raised when a review dict does not match the expected schema."""


def validate_review_dict(data: Any) -> dict[str, Any]:
    """Validate ``data`` against the reviewer schema. Returns the dict on success."""
    if not isinstance(data, dict):
        raise ReviewSchemaError(
            f"review must be a JSON object, got {type(data).__name__}"
        )

    missing = [k for k in REVIEW_REQUIRED_KEYS if k not in data]
    if missing:
        raise ReviewSchemaError(f"review missing required keys: {missing}")

    verdict = data["verdict"]
    if verdict not in REVIEW_VERDICTS:
        raise ReviewSchemaError(
            f"verdict must be one of {REVIEW_VERDICTS}, got {verdict!r}"
        )

    short_summary = data["short_summary"]
    if not isinstance(short_summary, str) or not short_summary.strip():
        raise ReviewSchemaError("short_summary must be a non-empty string")
    if len(short_summary) > REVIEW_SUMMARY_MAX_CHARS:
        data["short_summary"] = (
            short_summary[: REVIEW_SUMMARY_MAX_CHARS - 1].rstrip() + "…"
        )

    findings = data["findings"]
    if not isinstance(findings, list):
        raise ReviewSchemaError("findings must be a list")

    if verdict == "APPROVED" and findings:
        raise ReviewSchemaError(
            "findings must be empty when verdict is APPROVED"
        )
    if verdict == "REJECTED" and not findings:
        raise ReviewSchemaError(
            "findings must contain at least one entry when verdict is REJECTED"
        )

    for i, finding in enumerate(findings):
        _validate_finding(finding, i, verdict)

    for key in ("risks", "checks"):
        if key in data and data[key] is not None:
            value = data[key]
            if not isinstance(value, list) or not all(
                isinstance(x, str) for x in value
            ):
                raise ReviewSchemaError(f"{key} must be a list of strings")

    return data


def _validate_finding(f: Any, index: int, verdict: str) -> None:
    where = f"findings[{index}]"
    if not isinstance(f, dict):
        raise ReviewSchemaError(
            f"{where} must be an object, got {type(f).__name__}"
        )

    missing = [k for k in FINDING_REQUIRED_KEYS if k not in f]
    if missing:
        raise ReviewSchemaError(f"{where} missing required keys: {missing}")

    for key in FINDING_REQUIRED_KEYS:
        value = f[key]
        if key == "severity":
            if value not in REVIEW_SEVERITIES:
                raise ReviewSchemaError(
                    f"{where}.severity must be one of {REVIEW_SEVERITIES}, "
                    f"got {value!r}"
                )
            continue
        if not isinstance(value, str) or not value.strip():
            raise ReviewSchemaError(f"{where}.{key} must be a non-empty string")

    if verdict == "REJECTED":
        rf = f.get("required_fix")
        if not isinstance(rf, str) or not rf.strip():
            raise ReviewSchemaError(
                f"{where}.required_fix must be a non-empty string when verdict is REJECTED"
            )
    elif "required_fix" in f and f["required_fix"] is not None:
        if not isinstance(f["required_fix"], str):
            raise ReviewSchemaError(f"{where}.required_fix must be a string or null")

    if "file" in f and f["file"] is not None and not isinstance(f["file"], str):
        raise ReviewSchemaError(f"{where}.file must be a string or null")

    if "line" in f and f["line"] is not None:
        line = f["line"]
        if not isinstance(line, int) or isinstance(line, bool) or line <= 0:
            raise ReviewSchemaError(
                f"{where}.line must be a positive integer or null"
            )


REVIEW_SCHEMA_DOC = """
Emit exactly one JSON object with this shape:

{
  "verdict": "APPROVED" | "REJECTED",
  "short_summary": "<one or two sentences, target 280 chars>",
  "findings": [
    {
      "id": "<short stable id, e.g. 'F1'>",
      "severity": "P0" | "P1" | "P2" | "P3",
      "title": "<short finding title>",
      "file": "path/to/file.py",
      "line": 123,
      "body": "<concrete issue and why it matters>",
      "required_fix": "<what must change before approval>"
    }
  ],
  "risks": ["<residual risk or test gap>"],
  "checks": ["<what was reviewed or verified>"]
}

Rules:
- Required: `verdict`, `short_summary`, `findings`; `risks` / `checks` are optional string arrays.
- `short_summary`, finding `id` / `title` / `body` are non-empty strings; keep summary <=280 chars.
- APPROVED requires `findings=[]`; REJECTED requires findings and each finding needs `required_fix`.
- `severity` is P0, P1, P2, or P3; optional `file` is a string path and optional `line` is a positive integer.
""".strip()
