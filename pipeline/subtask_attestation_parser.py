"""
pipeline/subtask_attestation_parser.py — Parse + validate the developer's
done-criteria self-attestation (P7 / ADR 0068).

Two distinct concerns, kept separate (mirrors the reviewer parser):

* :func:`parse_subtask_attestation` — is the appended object a *well-formed*
  ``subtask_attestation`` (schema), recovered from surrounding build prose via
  the shared JSON-contract recovery path? Raises on malformed / missing /
  multiple objects.
* :func:`validate_subtask_attestation` — does the parsed object *match the
  current subtask's criteria* (right id, one entry per criterion index, all
  met)? This is the delivery-gate decision.

Neither judges whether the evidence is TRUE — that stays with the review /
final_acceptance / test gates. P7 only forces an explicit, complete claim.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents.entities import SubTask
from core.contracts.subtask_attestation_schema import (
    ATTESTATION_TYPE,
    validate_subtask_attestation_dict,
)
from pipeline.json_contract import parse_json_contract_object


class SubtaskAttestationParseError(ValueError):
    """Raised when attestation output cannot be parsed as JSON at all."""


@dataclass(frozen=True)
class CriterionAttestation:
    index: int
    criterion: str
    met: bool
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "criterion": self.criterion,
            "met": self.met,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class SubtaskAttestation:
    subtask_id: str
    criteria: tuple[CriterionAttestation, ...]
    summary: str
    parse_warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "subtask_id": self.subtask_id,
            "criteria": [c.to_dict() for c in self.criteria],
            "summary": self.summary,
        }


def _is_attestation_shape(data: Any) -> bool:
    """Loose candidate test for the recovery path: a dict tagged as ours."""
    return isinstance(data, dict) and data.get("type") == ATTESTATION_TYPE


def parse_subtask_attestation(text: str) -> SubtaskAttestation:
    """Recover + schema-validate exactly one attestation object from ``text``.

    Raises :class:`SubtaskAttestationParseError` for malformed / non-object /
    multiple-object output and ``SubtaskAttestationSchemaError`` for shape
    violations.
    """
    payload = parse_json_contract_object(
        text,
        label="subtask_attestation",
        parse_error_cls=SubtaskAttestationParseError,
        is_candidate=_is_attestation_shape,
        validate=validate_subtask_attestation_dict,
    )
    data = payload.data
    criteria = tuple(
        CriterionAttestation(
            index=int(c["index"]),
            criterion=str(c["criterion"]),
            met=bool(c["met"]),
            evidence=str(c["evidence"]),
        )
        for c in data["criteria"]
    )
    return SubtaskAttestation(
        subtask_id=str(data["subtask_id"]),
        criteria=criteria,
        summary=str(data["summary"]),
        parse_warnings=tuple(payload.parse_warnings),
    )


def _normalize_criterion(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def validate_subtask_attestation(
    parsed: SubtaskAttestation, subtask: SubTask,
) -> tuple[bool, str | None]:
    """Decide whether ``parsed`` completely and affirmatively covers
    ``subtask.done_criteria``.

    Returns ``(ok, reason)``. ``ok`` is True only when: the subtask_id matches,
    there is exactly one attestation entry per original criterion **index**
    (1-based), and every entry is ``met=true``.

    The criterion *text* is NOT a gating key — the agent may reword/translate a
    criterion while still addressing the right one by index, so a text mismatch
    is tolerated (the index is the binding key). Hard-failing on text drift
    would produce false incompletes.
    """
    if parsed.subtask_id != subtask.id:
        return (
            False,
            f"attestation subtask_id {parsed.subtask_id!r} does not match the "
            f"current subtask {subtask.id!r}",
        )

    expected = set(range(1, len(subtask.done_criteria) + 1))
    got = {c.index for c in parsed.criteria}
    if len(got) != len(parsed.criteria):
        return (False, "attestation has duplicate criterion indexes")
    if got != expected:
        return (
            False,
            f"attestation criterion indexes {sorted(got)} do not match the "
            f"{len(subtask.done_criteria)} declared done_criteria "
            f"{sorted(expected)}",
        )

    unmet = sorted(c.index for c in parsed.criteria if not c.met)
    if unmet:
        return (False, f"done_criteria not met (by index): {unmet}")

    return (True, None)


__all__ = [
    "CriterionAttestation",
    "SubtaskAttestation",
    "SubtaskAttestationParseError",
    "parse_subtask_attestation",
    "validate_subtask_attestation",
]
