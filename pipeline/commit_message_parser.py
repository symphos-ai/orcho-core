"""
pipeline/commit_message_parser.py — Parser for LLM-generated commit messages.

The ``llm_generate`` strategy on the commit-decision gate asks the runtime
to emit exactly one JSON object validated against
:mod:`core.contracts.commit_decision_schema`. This module parses the raw
model output into a typed :class:`ParsedCommitMessage` and renders the
final commit text the executor passes to ``git commit -m``.

Parser discipline matches :mod:`pipeline.release_parser` and
:mod:`pipeline.review_parser`: JSON-only, no markdown fences, no prose
preamble. A clean Conventional Commits subject is the load-bearing part
of the contract — the executor never invents one from prose.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.contracts.commit_decision_schema import (
    _CC_HEADER_RE,
    CommitMessageSchemaError,
    validate_commit_message_dict,
)
from pipeline.json_contract import parse_json_contract_object

__all__ = [
    "CommitMessageParseError",
    "CommitMessageSchemaError",
    "ParsedCommitMessage",
    "parse_commit_message",
    "render_commit_text",
]


class CommitMessageParseError(ValueError):
    """Raised when commit-message output cannot be parsed as JSON at all."""


@dataclass(frozen=True)
class ParsedCommitMessage:
    """A validated commit message from the ``llm_generate`` strategy."""
    subject: str
    body: str
    type: str
    breaking: bool
    scope: str | None = None
    parse_warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "subject": self.subject,
            "body": self.body,
            "type": self.type,
            "breaking": self.breaking,
        }
        if self.scope is not None:
            out["scope"] = self.scope
        return out

    def render(self) -> str:
        """Render the commit message as a single string ready for ``git commit -m``."""
        return render_commit_text(
            subject=self.subject,
            body=self.body,
            type=self.type,
            scope=self.scope,
            breaking=self.breaking,
        )


def parse_commit_message(text: str) -> ParsedCommitMessage:
    """Parse commit-message output into a :class:`ParsedCommitMessage`.

    Raises :class:`CommitMessageParseError` for malformed or non-object
    JSON and :class:`CommitMessageSchemaError` for schema violations.
    """
    payload = parse_json_contract_object(
        text,
        label="commit_message",
        parse_error_cls=CommitMessageParseError,
        is_candidate=_is_commit_message_json_shape,
        validate=validate_commit_message_dict,
    )
    return _from_dict(payload.data, parse_warnings=payload.parse_warnings)


def render_commit_text(
    *,
    subject: str,
    body: str,
    type: str,
    scope: str | None,
    breaking: bool,
) -> str:
    """Compose a Conventional Commits message string.

    Layout::

        <type>[(scope)][!]: <subject>
        <blank line>
        <body>
        <blank line>
        BREAKING CHANGE: <body>          # only when breaking=True and body is non-empty

    The subject is already formatted as a Conventional Commits header by
    the LLM contract; this composer only handles the type/scope/bang
    prefix and the body / breaking-change footer.

    Idempotent on subjects that already include the ``<type>: `` prefix:
    if the subject starts with ``"<type>: "`` (or ``"<type>(scope): "``),
    the prefix is not duplicated. This keeps the executor friendly to
    operator-edited subjects that already follow the convention.
    """
    bang = "!" if breaking else ""
    header_prefix = f"{type}({scope}){bang}: " if scope else f"{type}{bang}: "

    # Avoid duplicating the prefix if the model already produced it.
    normalized_subject = subject.lstrip()
    if _starts_with_conventional_header(normalized_subject, type):
        header = normalized_subject
    else:
        header = header_prefix + normalized_subject

    parts = [header]
    body_text = (body or "").strip()
    if body_text:
        parts.extend(("", body_text))
    if breaking and body_text:
        parts.extend(("", f"BREAKING CHANGE: {body_text}"))
    return "\n".join(parts).rstrip() + "\n"


def _starts_with_conventional_header(subject: str, type: str) -> bool:
    """True when ``subject`` already begins with a *fully-formed*
    Conventional Commits header whose type matches ``type``.

    Uses the same strict ``_CC_HEADER_RE`` the schema validates against
    rather than a loose ``startswith`` check. Loose prefix detection
    used to accept malformed headers like ``feat(api: drop X`` (no
    closing paren) as "already-prefixed" and pass them through verbatim
    — dropping any ``!`` or scope correction the structured fields
    implied. The schema now rejects malformed headers up-front, but
    keeping the render layer strict closes the same gap from the
    other side (defense-in-depth: a future schema regression cannot
    silently slip a malformed subject past the renderer).
    """
    match = _CC_HEADER_RE.match(subject)
    return match is not None and match.group("type") == type


def _from_dict(
    data: dict[str, Any],
    *,
    parse_warnings: tuple[str, ...] = (),
) -> ParsedCommitMessage:
    return ParsedCommitMessage(
        subject=data["subject"],
        body=data["body"],
        type=data["type"],
        breaking=bool(data["breaking"]),
        scope=data.get("scope"),
        parse_warnings=parse_warnings,
    )


def _is_commit_message_json_shape(data: Any) -> bool:
    return (
        isinstance(data, dict)
        and "subject" in data
        and "body" in data
        and "type" in data
        and "breaking" in data
    )
