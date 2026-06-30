"""Tests for ``pipeline.commit_message_parser`` and
:func:`core.contracts.commit_decision_schema.validate_commit_message_dict`.

Scope: the LLM-output contract (subject/body/type/scope/breaking) only.
The persisted ``validate_pending_dict`` / ``validate_decision_dict``
contracts live in :mod:`tests.unit.pipeline.test_commit_decision_schema`.
"""
from __future__ import annotations

import json

import pytest

from core.contracts.commit_decision_schema import (
    COMMIT_MESSAGE_BODY_MAX_CHARS,
    COMMIT_MESSAGE_SUBJECT_MAX_CHARS,
    COMMIT_MESSAGE_TYPES,
    CommitMessageSchemaError,
    validate_commit_message_dict,
)
from pipeline.commit_message_parser import (
    CommitMessageParseError,
    ParsedCommitMessage,
    parse_commit_message,
    render_commit_text,
)


def _ok(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "subject": "feat: do a thing",
        "body": "",
        "type": "feat",
        "breaking": False,
    }
    base.update(overrides)
    return base


class TestValidateCommitMessageDict:
    def test_happy_path_minimal(self) -> None:
        out = validate_commit_message_dict(_ok())
        assert out["subject"] == "feat: do a thing"

    def test_happy_path_full(self) -> None:
        # CC header carries ``!`` to match breaking=True; coherence
        # check enforces this — see ``test_breaking_field_without_bang_rejected``.
        out = validate_commit_message_dict(_ok(
            subject="feat(auth)!: rotate tokens",
            body="Tokens now expire every 24h.",
            scope="auth",
            breaking=True,
        ))
        assert out["scope"] == "auth"
        assert out["breaking"] is True

    def test_happy_path_scope_null(self) -> None:
        validate_commit_message_dict(_ok(scope=None))

    @pytest.mark.parametrize(
        "missing_key", ["subject", "body", "type", "breaking"],
    )
    def test_missing_required_key_rejected(self, missing_key: str) -> None:
        data = _ok()
        del data[missing_key]
        with pytest.raises(CommitMessageSchemaError, match="missing required keys"):
            validate_commit_message_dict(data)

    def test_unknown_extra_key_rejected(self) -> None:
        with pytest.raises(CommitMessageSchemaError, match="unknown keys"):
            validate_commit_message_dict(_ok(extra="x"))

    def test_non_object_rejected(self) -> None:
        with pytest.raises(CommitMessageSchemaError, match="JSON object"):
            validate_commit_message_dict(["not", "an", "object"])

    def test_empty_subject_rejected(self) -> None:
        with pytest.raises(CommitMessageSchemaError, match="subject"):
            validate_commit_message_dict(_ok(subject=""))

    def test_whitespace_subject_rejected(self) -> None:
        with pytest.raises(CommitMessageSchemaError, match="subject"):
            validate_commit_message_dict(_ok(subject="   "))

    def test_multiline_subject_rejected(self) -> None:
        with pytest.raises(CommitMessageSchemaError, match="single line"):
            validate_commit_message_dict(_ok(subject="a\nb"))

    def test_oversized_subject_rejected(self) -> None:
        oversized = "f" * (COMMIT_MESSAGE_SUBJECT_MAX_CHARS + 1)
        with pytest.raises(CommitMessageSchemaError, match="exceeds"):
            validate_commit_message_dict(_ok(subject=oversized))

    def test_subject_at_limit_accepted(self) -> None:
        at_limit = "f" * COMMIT_MESSAGE_SUBJECT_MAX_CHARS
        validate_commit_message_dict(_ok(subject=at_limit))

    def test_non_string_body_rejected(self) -> None:
        with pytest.raises(CommitMessageSchemaError, match="body"):
            validate_commit_message_dict(_ok(body=123))

    def test_oversized_body_rejected(self) -> None:
        oversized = "b" * (COMMIT_MESSAGE_BODY_MAX_CHARS + 1)
        with pytest.raises(CommitMessageSchemaError, match="exceeds"):
            validate_commit_message_dict(_ok(body=oversized))

    def test_invalid_type_rejected(self) -> None:
        assert "bogus" not in COMMIT_MESSAGE_TYPES
        with pytest.raises(CommitMessageSchemaError, match="type"):
            validate_commit_message_dict(_ok(type="bogus"))

    @pytest.mark.parametrize("bad_breaking", ["true", 1, 0, None])
    def test_non_bool_breaking_rejected(self, bad_breaking: object) -> None:
        with pytest.raises(CommitMessageSchemaError, match="boolean"):
            validate_commit_message_dict(_ok(breaking=bad_breaking))

    def test_scope_with_space_rejected(self) -> None:
        with pytest.raises(CommitMessageSchemaError, match="single token"):
            validate_commit_message_dict(_ok(scope="scope name"))

    def test_scope_with_newline_rejected(self) -> None:
        with pytest.raises(CommitMessageSchemaError, match="single token"):
            validate_commit_message_dict(_ok(scope="a\nb"))

    @pytest.mark.parametrize(
        "ws_scope",
        ["api\tdb", "a b", "a b"],  # tab, NBSP, em-space
        ids=["tab", "nbsp", "em-space"],
    )
    def test_scope_with_any_unicode_whitespace_rejected(
        self, ws_scope: str,
    ) -> None:
        """``scope`` must be a single token; any unicode whitespace
        (tab, NBSP, em-space, …) disqualifies it. Pinned because the
        original ``" " in scope or "\\n" in scope`` check missed tabs
        and unicode spaces — caught in review."""
        with pytest.raises(CommitMessageSchemaError, match="single token"):
            validate_commit_message_dict(_ok(scope=ws_scope))

    def test_empty_scope_rejected(self) -> None:
        with pytest.raises(CommitMessageSchemaError, match="scope"):
            validate_commit_message_dict(_ok(scope=""))

    # ── subject / type / scope / breaking coherence ───────────────────────

    def test_breaking_field_without_bang_rejected(self) -> None:
        """If ``breaking=true`` but the CC-style subject has no ``!``,
        the renderer would emit a commit that silently lies about its
        breaking-change status. Pinned because we caught this in
        review: ``{subject: "feat: drop old API", breaking: true}``
        used to render as ``feat: drop old API\\n`` with no ``!`` and
        no ``BREAKING CHANGE:`` footer."""
        with pytest.raises(
            CommitMessageSchemaError, match="breaking-marker",
        ):
            validate_commit_message_dict(_ok(
                subject="feat: drop old API",
                breaking=True,
            ))

    def test_breaking_false_with_bang_subject_rejected(self) -> None:
        """Mirror: subject says ``!`` (breaking change) but the field
        flag says ``breaking=false``. Same disagreement, opposite
        direction."""
        with pytest.raises(
            CommitMessageSchemaError, match="breaking-marker",
        ):
            validate_commit_message_dict(_ok(
                subject="feat!: drop old API",
                breaking=False,
            ))

    def test_subject_type_disagreement_rejected(self) -> None:
        """CC-style subject ``fix: ...`` must not coexist with
        ``type="feat"``. The header is the load-bearing wire signal
        for downstream `git log` consumers — disagreement is a
        contract violation."""
        with pytest.raises(
            CommitMessageSchemaError, match="subject header type",
        ):
            validate_commit_message_dict(_ok(
                subject="fix: rotate tokens",
                type="feat",
            ))

    def test_subject_scope_disagreement_rejected(self) -> None:
        """CC-style subject ``feat(api): ...`` must agree with
        ``scope`` field. ``feat(auth): ...`` with ``scope="api"`` is
        a lie."""
        with pytest.raises(
            CommitMessageSchemaError, match="subject header scope",
        ):
            validate_commit_message_dict(_ok(
                subject="feat(auth): rotate",
                scope="api",
            ))

    def test_subject_scope_present_in_subject_field_null_rejected(
        self,
    ) -> None:
        """CC-style subject ``feat(auth): ...`` with ``scope=None``
        is a lie (subject claims a scope; field says no scope)."""
        with pytest.raises(
            CommitMessageSchemaError, match="subject header scope",
        ):
            validate_commit_message_dict(_ok(
                subject="feat(auth): rotate",
                scope=None,
            ))

    def test_raw_summary_subject_accepted(self) -> None:
        """Subjects without a CC-style header are treated as raw
        summaries — ``render_commit_text`` will prepend the header
        from fields. No coherence check fires, no error."""
        validate_commit_message_dict(_ok(
            subject="drop old API",
            type="feat",
            breaking=True,
        ))

    def test_coherent_full_header_accepted(self) -> None:
        """Subject with full CC header that matches every field is
        the happy path for explicit-header style — no rewrite by
        the renderer."""
        validate_commit_message_dict(_ok(
            subject="refactor(prompts)!: drop legacy contract",
            type="refactor",
            scope="prompts",
            breaking=True,
        ))

    # ── malformed-CC-header gate (P2 follow-up) ───────────────────────────

    @pytest.mark.parametrize(
        "malformed_subject",
        [
            "feat(api: drop old API",      # unclosed scope paren
            "feat(api)drop old API",        # missing `: `
            "feat:drop old API",            # missing space after colon
            "feat!drop old API",            # bang without colon
            "feat(: drop X",                # empty scope
        ],
        ids=[
            "unclosed-scope-paren",
            "scope-no-separator",
            "no-space-after-colon",
            "bang-no-colon",
            "empty-scope-parens",
        ],
    )
    def test_malformed_cc_header_subject_rejected(
        self, malformed_subject: str,
    ) -> None:
        """A subject that ``looks like`` a Conventional Commits header
        attempt (starts with ``<type>`` followed by ``(``, ``!``, or
        ``:``) but doesn't fully parse must be rejected up-front.

        Pinned because the original P2 coherence check only fired on
        subjects that matched the strict header regex. Malformed
        prefixes (e.g. ``feat(api: drop X``) used to pass schema
        AND used to be treated as "already-prefixed" by the renderer's
        loose ``startswith`` detection — silently dropping the ``!``
        the ``breaking=true`` field implied. Defense-in-depth: schema
        rejects up-front; renderer also tightened to strict regex
        (see ``test_render_does_not_treat_malformed_prefix_as_header``).
        """
        with pytest.raises(
            CommitMessageSchemaError, match="malformed",
        ):
            validate_commit_message_dict(_ok(
                subject=malformed_subject,
                type="feat",
                breaking=True,
            ))


class TestParseCommitMessage:
    def test_happy_path_returns_frozen_dataclass(self) -> None:
        text = json.dumps({
            "subject": "feat(api): rotate token",
            "body": "Daily rotation.",
            "type": "feat",
            "scope": "api",
            "breaking": False,
        })
        out = parse_commit_message(text)
        assert isinstance(out, ParsedCommitMessage)
        assert out.subject == "feat(api): rotate token"
        assert out.body == "Daily rotation."
        assert out.type == "feat"
        assert out.scope == "api"
        assert out.breaking is False
        # Frozen dataclass: cannot reassign.
        with pytest.raises(Exception):  # noqa: B017 — frozen → FrozenInstanceError
            out.subject = "other"  # type: ignore[misc]

    def test_prose_preamble_recovers_when_json_is_valid(self) -> None:
        text = "Here is the message:\n" + json.dumps({
            "subject": "feat(api): rotate token",
            "body": "Daily rotation.",
            "type": "feat",
            "scope": "api",
            "breaking": False,
        })
        out = parse_commit_message(text)
        assert out.subject == "feat(api): rotate token"
        assert len(out.parse_warnings) == 1
        assert (
            "stripped non-JSON text around commit_message JSON"
            in out.parse_warnings[0]
        )

    def test_markdown_fence_recovers_when_json_is_valid(self) -> None:
        text = "```json\n" + json.dumps({
            "subject": "feat(api): rotate token",
            "body": "Daily rotation.",
            "type": "feat",
            "scope": "api",
            "breaking": False,
        }) + "\n```"
        out = parse_commit_message(text)
        assert out.subject == "feat(api): rotate token"
        assert len(out.parse_warnings) == 1
        assert (
            "stripped non-JSON text around commit_message JSON"
            in out.parse_warnings[0]
        )

    def test_prose_preamble_rejected(self) -> None:
        text = "Here is the message:\n{\"subject\": \"feat: x\"}"
        with pytest.raises(CommitMessageParseError, match="JSON object"):
            parse_commit_message(text)

    def test_markdown_fence_rejected(self) -> None:
        text = "```json\n{\"subject\": \"feat: x\"}\n```"
        with pytest.raises(CommitMessageParseError, match="JSON object"):
            parse_commit_message(text)

    def test_invalid_json_rejected(self) -> None:
        with pytest.raises(CommitMessageParseError, match="JSON"):
            parse_commit_message("{not json")

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(CommitMessageParseError, match="JSON object"):
            parse_commit_message("")

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(CommitMessageParseError, match="JSON object"):
            parse_commit_message("   \n\t  ")

    def test_schema_violation_propagates_as_schema_error(self) -> None:
        # Valid JSON, but schema rejects the empty subject — surface the
        # schema-specific error class, not the parse-error class.
        text = json.dumps({
            "subject": "",
            "body": "",
            "type": "feat",
            "breaking": False,
        })
        with pytest.raises(CommitMessageSchemaError):
            parse_commit_message(text)


class TestRenderCommitText:
    def test_scope_and_body(self) -> None:
        out = render_commit_text(
            subject="summary",
            body="body line",
            type="feat",
            scope="api",
            breaking=False,
        )
        assert out == "feat(api): summary\n\nbody line\n"

    def test_no_scope_no_body(self) -> None:
        out = render_commit_text(
            subject="summary",
            body="",
            type="feat",
            scope=None,
            breaking=False,
        )
        assert out == "feat: summary\n"

    def test_whitespace_body_treated_as_empty(self) -> None:
        out = render_commit_text(
            subject="summary",
            body="   ",
            type="fix",
            scope=None,
            breaking=False,
        )
        assert out == "fix: summary\n"

    def test_breaking_with_body_adds_footer(self) -> None:
        out = render_commit_text(
            subject="summary",
            body="X",
            type="feat",
            scope=None,
            breaking=True,
        )
        assert "BREAKING CHANGE: X" in out
        # Footer block sits after the body.
        assert out == "feat!: summary\n\nX\n\nBREAKING CHANGE: X\n"

    def test_breaking_with_empty_body_has_no_footer(self) -> None:
        out = render_commit_text(
            subject="summary",
            body="",
            type="feat",
            scope=None,
            breaking=True,
        )
        assert "BREAKING CHANGE" not in out
        assert out == "feat!: summary\n"

    def test_idempotent_on_existing_simple_prefix(self) -> None:
        out = render_commit_text(
            subject="feat: foo",
            body="",
            type="feat",
            scope=None,
            breaking=False,
        )
        assert out == "feat: foo\n"

    def test_idempotent_on_existing_scoped_prefix(self) -> None:
        out = render_commit_text(
            subject="feat(api): foo",
            body="",
            type="feat",
            scope="api",
            breaking=False,
        )
        assert out == "feat(api): foo\n"

    def test_idempotent_on_existing_breaking_prefix(self) -> None:
        out = render_commit_text(
            subject="feat!: foo",
            body="",
            type="feat",
            scope=None,
            breaking=True,
        )
        assert out == "feat!: foo\n"

    def test_render_does_not_treat_malformed_prefix_as_header(self) -> None:
        """Defense-in-depth pin: a subject that *looks like* a CC header
        attempt but doesn't fully parse (e.g. unclosed scope paren) must
        NOT be treated as already-prefixed by the renderer. If schema
        validation ever regresses and lets a malformed subject through,
        the renderer should prepend a proper header rather than silently
        keep the malformed one.

        The original loose ``startswith("feat(")`` check accepted
        ``"feat(api: drop X"`` as already-prefixed → resulting commit
        had no ``!``, no proper scope syntax, no rewrite. The
        ``_CC_HEADER_RE`` switch fixes it: malformed prefix → renderer
        sees no header → prepends one from fields.
        """
        out = render_commit_text(
            subject="feat(api: drop old API",  # unclosed scope paren
            body="",
            type="feat",
            scope="api",
            breaking=True,
        )
        # The malformed prefix is NOT treated as a header — renderer
        # prepends the proper ``feat(api)!: `` and ships both.
        assert out.startswith("feat(api)!: ")
        assert "feat(api: drop old API" in out
