"""
core/contracts/subtask_attestation_schema.py — JSON schema for the developer
subtask done-criteria self-attestation (P7 / ADR 0068).

A ``subtask_dag`` developer keeps its normal human-readable build output and
appends exactly one machine-readable attestation object that reports, per
``SubTask.done_criteria`` item (by index), whether it was met plus a short
evidence claim. Orcho validates this object's SHAPE here; whether it matches
the *current* subtask's criteria is a separate validator
(``pipeline.subtask_attestation_parser.validate_subtask_attestation``), and
whether the evidence is TRUE is the job of the downstream quality gates
(review / final_acceptance / tests).

Schema is dependency-free (no pydantic) so the core stays importable on a bare
stdlib install. Validation lives in :func:`validate_subtask_attestation_dict`.
"""
from __future__ import annotations

from typing import Any

ATTESTATION_TYPE = "subtask_attestation"
ATTESTATION_SUMMARY_MAX_CHARS = 280

ATTESTATION_REQUIRED_KEYS = ("type", "subtask_id", "criteria", "summary")
CRITERION_REQUIRED_KEYS = ("index", "criterion", "met", "evidence")

#: Human/agent-facing schema doc embedded into the subtask prompt contract so
#: the developer agent knows exactly what attestation object to append. Kept
#: next to the validator it describes so the doc and the checks cannot drift.
ATTESTATION_SCHEMA_DOC = (
    "{\n"
    '  "type": "subtask_attestation",\n'
    '  "subtask_id": "<the Current Executable Subtask id, verbatim>",\n'
    '  "criteria": [\n'
    "    {\n"
    '      "index": 1,            // 1-based position of the done-criterion,\n'
    "                             // in the order listed under Done criteria\n"
    '      "criterion": "<the criterion text>",\n'
    '      "met": true,           // true ONLY if you actually satisfied it\n'
    '      "evidence": "<one concrete sentence: what you did / where>"\n'
    "    }\n"
    "    // ...exactly one entry per done-criterion, indexes 1..N, no gaps\n"
    "  ],\n"
    '  "summary": "<=280 chars, one line on the overall result>"\n'
    "}"
)


class SubtaskAttestationSchemaError(ValueError):
    """Raised when an attestation dict does not match the expected schema."""


def validate_subtask_attestation_dict(data: Any) -> dict[str, Any]:
    """Validate ``data`` against the attestation schema. Returns it on success.

    Shape-only: this does NOT check the claims against the current subtask's
    criteria (that is ``validate_subtask_attestation``) and does NOT judge
    whether the evidence is true (that is the quality gates).
    """
    if not isinstance(data, dict):
        raise SubtaskAttestationSchemaError(
            f"attestation must be a JSON object, got {type(data).__name__}"
        )

    missing = [k for k in ATTESTATION_REQUIRED_KEYS if k not in data]
    if missing:
        raise SubtaskAttestationSchemaError(
            f"attestation missing required keys: {missing}"
        )

    if data["type"] != ATTESTATION_TYPE:
        raise SubtaskAttestationSchemaError(
            f"type must be {ATTESTATION_TYPE!r}, got {data['type']!r}"
        )

    subtask_id = data["subtask_id"]
    if not isinstance(subtask_id, str) or not subtask_id.strip():
        raise SubtaskAttestationSchemaError(
            "subtask_id must be a non-empty string"
        )

    criteria = data["criteria"]
    if not isinstance(criteria, list):
        raise SubtaskAttestationSchemaError("criteria must be a list")
    for i, entry in enumerate(criteria):
        _validate_criterion(entry, i)

    summary = data["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise SubtaskAttestationSchemaError("summary must be a non-empty string")
    if len(summary) > ATTESTATION_SUMMARY_MAX_CHARS:
        data["summary"] = (
            summary[: ATTESTATION_SUMMARY_MAX_CHARS - 1].rstrip() + "…"
        )

    return data


def _validate_criterion(entry: Any, i: int) -> None:
    if not isinstance(entry, dict):
        raise SubtaskAttestationSchemaError(
            f"criteria[{i}] must be an object, got {type(entry).__name__}"
        )
    missing = [k for k in CRITERION_REQUIRED_KEYS if k not in entry]
    if missing:
        raise SubtaskAttestationSchemaError(
            f"criteria[{i}] missing required keys: {missing}"
        )
    index = entry["index"]
    # bool is an int subclass — reject it explicitly so a flag never poses as an
    # index.
    if not isinstance(index, int) or isinstance(index, bool) or index < 1:
        raise SubtaskAttestationSchemaError(
            f"criteria[{i}].index must be a 1-based positive int, got {index!r}"
        )
    criterion = entry["criterion"]
    if not isinstance(criterion, str) or not criterion.strip():
        raise SubtaskAttestationSchemaError(
            f"criteria[{i}].criterion must be a non-empty string"
        )
    if not isinstance(entry["met"], bool):
        raise SubtaskAttestationSchemaError(
            f"criteria[{i}].met must be a boolean"
        )
    evidence = entry["evidence"]
    if not isinstance(evidence, str) or not evidence.strip():
        raise SubtaskAttestationSchemaError(
            f"criteria[{i}].evidence must be a non-empty string (a claim, "
            "not proof)"
        )


__all__ = [
    "ATTESTATION_TYPE",
    "ATTESTATION_SUMMARY_MAX_CHARS",
    "ATTESTATION_REQUIRED_KEYS",
    "CRITERION_REQUIRED_KEYS",
    "ATTESTATION_SCHEMA_DOC",
    "SubtaskAttestationSchemaError",
    "validate_subtask_attestation_dict",
]
