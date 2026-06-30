"""
pipeline/review_parser.py — Parser for reviewer-phase JSON output.

Reviewer phases (``validate_plan``, ``review``, ``final_acceptance``) emit a single JSON
object validated against :mod:`core.contracts.review_schema`. This module
parses the raw model output and yields a typed :class:`ParsedReview` Orcho
uses for control flow.

Parse contract: the raw model output must be exactly one JSON object. Markdown
fences, prose verdict lines, and LGTM-style text are protocol violations.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.contracts.review_schema import (
    REVIEW_SUMMARY_MAX_CHARS,
    validate_review_dict,
)
from pipeline.json_contract import parse_json_contract_object


class ReviewParseError(ValueError):
    """Raised when reviewer output cannot be parsed as JSON at all."""


@dataclass(frozen=True)
class ReviewFinding:
    id: str
    severity: str
    title: str
    body: str
    required_fix: str = ""
    file: str | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, object]:
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
class ParsedReview:
    verdict: str
    short_summary: str
    findings: tuple[ReviewFinding, ...] = ()
    risks: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    source: str = "json"
    # Non-fatal advisories raised while parsing (e.g. short_summary auto-trimmed).
    parse_warnings: tuple[str, ...] = ()

    @property
    def approved(self) -> bool:
        return self.verdict == "APPROVED"

    def findings_as_dicts(self) -> list[dict[str, object]]:
        return [f.to_dict() for f in self.findings]


def parse_review(text: str) -> ParsedReview:
    """Parse reviewer output into a :class:`ParsedReview`.

    Raises :class:`ReviewParseError` for malformed or non-object JSON and
    :class:`ReviewSchemaError` for schema violations.
    """
    payload = parse_json_contract_object(
        text,
        label="review",
        parse_error_cls=ReviewParseError,
        is_candidate=_is_review_json_shape,
        validate=validate_review_dict,
    )
    data = payload.data

    original_summary_len = (
        len(payload.original_data["short_summary"])
        if isinstance(payload.original_data.get("short_summary"), str)
        else 0
    )
    warnings: list[str] = list(payload.parse_warnings)
    if original_summary_len > REVIEW_SUMMARY_MAX_CHARS:
        warnings.append(
            f"short_summary was {original_summary_len} chars; "
            f"auto-trimmed to {REVIEW_SUMMARY_MAX_CHARS} "
            f"(target ≤ {REVIEW_SUMMARY_MAX_CHARS})."
        )
    return _from_dict(data, source="json", parse_warnings=tuple(warnings))


def _is_review_json_shape(data: Any) -> bool:
    return (
        isinstance(data, dict)
        and "verdict" in data
        and "short_summary" in data
        and "findings" in data
    )


def _from_dict(
    data: dict[str, Any],
    *,
    source: str,
    parse_warnings: tuple[str, ...] = (),
) -> ParsedReview:
    findings = tuple(_finding_from_dict(f) for f in data.get("findings", []))
    risks = tuple(data.get("risks") or ())
    checks = tuple(data.get("checks") or ())
    return ParsedReview(
        verdict=data["verdict"],
        short_summary=data["short_summary"],
        findings=findings,
        risks=risks,
        checks=checks,
        source=source,
        parse_warnings=parse_warnings,
    )


def _finding_from_dict(f: dict[str, Any]) -> ReviewFinding:
    return ReviewFinding(
        id=f["id"],
        severity=f["severity"],
        title=f["title"],
        body=f["body"],
        required_fix=f.get("required_fix") or "",
        file=f.get("file"),
        line=f.get("line"),
    )
