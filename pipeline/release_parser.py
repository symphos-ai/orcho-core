"""
pipeline/release_parser.py — Parser for release-gate JSON output.

The release gate (``final_acceptance`` phase, future
``cross_final_acceptance`` gate) emits a single JSON object validated
against :mod:`core.contracts.release_schema`. This module parses the raw
model output and yields a typed :class:`ParsedRelease` Orcho uses for
control flow.

Distinct from :mod:`pipeline.review_parser` because the release tier
asks a different question and carries different signal. Parse contract:
the raw model output must be exactly one JSON object — markdown fences,
prose verdict lines, and LGTM-style text are protocol violations.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.contracts.release_schema import (
    RELEASE_SUMMARY_MAX_CHARS,
    ReleaseSchemaError,
    validate_release_dict,
)
from pipeline.json_contract import parse_json_contract_object

__all__ = [
    "ContractStatus",
    "ParsedRelease",
    "ReleaseBlocker",
    "ReleaseParseError",
    "ReleaseSchemaError",
    "VerificationGap",
    "parse_release",
]


class ReleaseParseError(ValueError):
    """Raised when release output cannot be parsed as JSON at all."""


@dataclass(frozen=True)
class ReleaseBlocker:
    """A single ship-blocking item. Field set is a structural superset
    of :class:`pipeline.review_parser.ReviewFinding` so evidence
    collectors can project a blocker into a review-shape finding
    without bespoke mapping — ``why_blocks_release`` is the release-
    tier addition."""
    id: str
    severity: str
    title: str
    body: str
    required_fix: str
    why_blocks_release: str
    file: str | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "id": self.id,
            "severity": self.severity,
            "title": self.title,
            "body": self.body,
            "required_fix": self.required_fix,
            "why_blocks_release": self.why_blocks_release,
        }
        if self.file is not None:
            out["file"] = self.file
        if self.line is not None:
            out["line"] = self.line
        return out

    def to_finding_dict(self) -> dict[str, object]:
        """Project this blocker onto the review-shape ``finding`` dict
        (the shape :func:`pipeline.review_parser.ReviewFinding.to_dict`
        produces). Used by the dual-shape mirror written into
        ``state.phase_log["final_acceptance"]`` so existing review-
        finding consumers (Web phase card, MCP ``orcho_run_evidence``
        findings slice, ``sdk.evidence_slices.list_findings``) keep
        working without API changes. ``why_blocks_release`` is dropped
        from this projection — it belongs to the release surface only.
        """
        out: dict[str, object] = {
            "id": self.id,
            "severity": self.severity,
            "title": self.title,
            "body": self.body,
            "required_fix": self.required_fix,
        }
        if self.file is not None:
            out["file"] = self.file
        if self.line is not None:
            out["line"] = self.line
        return out


@dataclass(frozen=True)
class VerificationGap:
    """A risk with missing evidence the release reviewer flags as
    needing a verification step before ship."""
    risk: str
    missing_evidence: str
    required_check: str

    def to_dict(self) -> dict[str, object]:
        return {
            "risk": self.risk,
            "missing_evidence": self.missing_evidence,
            "required_check": self.required_check,
        }


@dataclass(frozen=True)
class ContractStatus:
    """Structured per-aspect status of the release."""
    task_contract: str
    interfaces: str
    persistence: str
    tests: str

    def to_dict(self) -> dict[str, object]:
        return {
            "task_contract": self.task_contract,
            "interfaces": self.interfaces,
            "persistence": self.persistence,
            "tests": self.tests,
        }


@dataclass(frozen=True)
class ParsedRelease:
    verdict: str
    ship_ready: bool
    short_summary: str
    release_blockers: tuple[ReleaseBlocker, ...]
    verification_gaps: tuple[VerificationGap, ...]
    contract_status: ContractStatus
    source: str = "json"
    # Non-fatal advisories raised while parsing (e.g. short_summary auto-trimmed).
    parse_warnings: tuple[str, ...] = ()

    @property
    def approved(self) -> bool:
        return self.verdict == "APPROVED"

    def blockers_as_dicts(self) -> list[dict[str, object]]:
        return [b.to_dict() for b in self.release_blockers]

    def gaps_as_dicts(self) -> list[dict[str, object]]:
        return [g.to_dict() for g in self.verification_gaps]


def parse_release(text: str) -> ParsedRelease:
    """Parse release-gate output into a :class:`ParsedRelease`.

    Raises :class:`ReleaseParseError` for malformed or non-object JSON
    and :class:`ReleaseSchemaError` for schema / coherence violations.
    """
    payload = parse_json_contract_object(
        text,
        label="release",
        parse_error_cls=ReleaseParseError,
        is_candidate=_is_release_json_shape,
        validate=validate_release_dict,
    )
    data = payload.data

    original_summary_len = (
        len(payload.original_data["short_summary"])
        if isinstance(payload.original_data.get("short_summary"), str)
        else 0
    )
    warnings: list[str] = list(payload.parse_warnings)
    if original_summary_len > RELEASE_SUMMARY_MAX_CHARS:
        warnings.append(
            f"short_summary was {original_summary_len} chars; "
            f"auto-trimmed to {RELEASE_SUMMARY_MAX_CHARS} "
            f"(target ≤ {RELEASE_SUMMARY_MAX_CHARS})."
        )
    return _from_dict(data, source="json", parse_warnings=tuple(warnings))


def _is_release_json_shape(data: Any) -> bool:
    return (
        isinstance(data, dict)
        and "verdict" in data
        and "ship_ready" in data
        and "short_summary" in data
        and "release_blockers" in data
        and "verification_gaps" in data
        and "contract_status" in data
    )


def _from_dict(
    data: dict[str, Any],
    *,
    source: str,
    parse_warnings: tuple[str, ...] = (),
) -> ParsedRelease:
    blockers = tuple(_blocker_from_dict(b) for b in data["release_blockers"])
    gaps = tuple(_gap_from_dict(g) for g in data["verification_gaps"])
    cs = data["contract_status"]
    return ParsedRelease(
        verdict=data["verdict"],
        ship_ready=bool(data["ship_ready"]),
        short_summary=data["short_summary"],
        release_blockers=blockers,
        verification_gaps=gaps,
        contract_status=ContractStatus(
            task_contract=cs["task_contract"],
            interfaces=cs["interfaces"],
            persistence=cs["persistence"],
            tests=cs["tests"],
        ),
        source=source,
        parse_warnings=parse_warnings,
    )


def _blocker_from_dict(b: dict[str, Any]) -> ReleaseBlocker:
    return ReleaseBlocker(
        id=b["id"],
        severity=b["severity"],
        title=b["title"],
        body=b["body"],
        required_fix=b["required_fix"],
        why_blocks_release=b["why_blocks_release"],
        file=b.get("file"),
        line=b.get("line"),
    )


def _gap_from_dict(g: dict[str, Any]) -> VerificationGap:
    return VerificationGap(
        risk=g["risk"],
        missing_evidence=g["missing_evidence"],
        required_check=g["required_check"],
    )
