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

    The subject is an unprefixed imperative summary; structured ``type``,
    ``scope``, and ``breaking`` fields always compose the header.
    """
    bang = "!" if breaking else ""
    header_prefix = f"{type}({scope}){bang}: " if scope else f"{type}{bang}: "

    header = header_prefix + subject.lstrip()

    parts = [header]
    body_text = (body or "").strip()
    if body_text:
        parts.extend(("", body_text))
    if breaking and body_text:
        parts.extend(("", f"BREAKING CHANGE: {body_text}"))
    return "\n".join(parts).rstrip() + "\n"
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
