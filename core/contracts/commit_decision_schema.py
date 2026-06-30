"""
core/contracts/commit_decision_schema.py — JSON schemas for the
commit-decision gate.

Three related but distinct schemas live here:

* :func:`validate_commit_message_dict` — the LLM-generated commit message
  the ``llm_generate`` strategy emits via the system-tail
  ``commit_message_json_contract``. The model output is one JSON object
  with ``subject`` (Conventional Commits header), ``body``, ``type``,
  optional ``scope``, and a boolean ``breaking`` flag.

* :func:`validate_pending_dict` — the payload the orchestrator writes
  into ``meta.commit_decision`` (and persists in ``meta.json``) when it
  pauses after a release verdict on a non-empty working tree. The
  operator reads this payload to decide ``fix`` / ``approve`` / ``apply`` /
  ``skip`` / ``halt``.

* :func:`validate_decision_dict` — the persisted operator decision
  artifact under ``<run_dir>/commit_decisions/<safe_id>.json``. This is
  the audit record of the exact instruction the resume path executes.

Schemas are dependency-free (no pydantic) — the core stays importable
on a bare stdlib install, identical to ``release_schema`` /
``review_schema`` / ``plan_schema``.
"""
from __future__ import annotations

import re
from typing import Any

# Conventional Commits 1.0 header anchor.
#   <type>(<scope>)?(!)?: <summary>
# Used by :func:`validate_commit_message_dict` to detect when the
# subject carries an explicit CC-style header so we can cross-check
# its type / scope / bang against the structured fields. Subjects
# without a header are treated as raw summaries — :func:`render_
# commit_text` builds the header from the fields.
_CC_HEADER_RE = re.compile(
    r"^(?P<type>[a-z][a-z0-9_-]*)"     # ``feat``, ``fix``, ``refactor``…
    r"(?:\((?P<scope>[^)]+)\))?"        # optional ``(scope)``
    r"(?P<bang>!)?"                     # optional ``!``
    r":\s+"                             # ``: `` separator
    r"(?P<summary>.+)$"                 # remainder is the summary
)

# Loose "looks like a CC header attempt" anchor — a subject that
# starts with a lowercase token followed immediately by ``(``,
# ``!``, or ``:`` is reaching for the CC shape. If it doesn't fully
# parse under :data:`_CC_HEADER_RE` it is malformed (e.g.
# ``feat(api: drop X`` — unclosed scope paren) and must be rejected.
# Without this guard, schema validation would silently accept
# malformed prefixes and the renderer's prefix-detection used to
# treat them as already-prefixed — silently dropping the ``!`` and
# any other field-driven correction.
_CC_HEADER_LOOKS_LIKE_RE = re.compile(r"^[a-z][a-z0-9_-]*[(!:]")

# ---------------------------------------------------------------------------
# LLM commit_message JSON contract — what the ``llm_generate`` strategy emits.
# Mirrors Conventional Commits 1.0 (https://www.conventionalcommits.org/).
# ---------------------------------------------------------------------------

COMMIT_MESSAGE_SUBJECT_MAX_CHARS = 100
COMMIT_MESSAGE_BODY_MAX_CHARS = 4000
COMMIT_MESSAGE_TYPES: tuple[str, ...] = (
    "feat", "fix", "chore", "docs", "refactor", "perf",
    "test", "build", "ci", "style", "revert",
)
COMMIT_MESSAGE_REQUIRED_KEYS: tuple[str, ...] = (
    "subject", "body", "type", "breaking",
)
COMMIT_MESSAGE_OPTIONAL_KEYS: tuple[str, ...] = ("scope",)


class CommitMessageSchemaError(ValueError):
    """Raised when an LLM-generated commit message does not match the schema."""


def validate_commit_message_dict(data: Any) -> dict[str, Any]:
    """Validate ``data`` against the LLM commit-message schema. Returns the dict on success."""
    if not isinstance(data, dict):
        raise CommitMessageSchemaError(
            f"commit_message must be a JSON object, got {type(data).__name__}"
        )
    missing = [k for k in COMMIT_MESSAGE_REQUIRED_KEYS if k not in data]
    if missing:
        raise CommitMessageSchemaError(
            f"commit_message missing required keys: {missing}"
        )
    unknown = sorted(
        set(data.keys())
        - set(COMMIT_MESSAGE_REQUIRED_KEYS)
        - set(COMMIT_MESSAGE_OPTIONAL_KEYS)
    )
    if unknown:
        raise CommitMessageSchemaError(
            f"commit_message has unknown keys: {unknown}"
        )

    subject = data["subject"]
    if not isinstance(subject, str) or not subject.strip():
        raise CommitMessageSchemaError(
            "commit_message.subject must be a non-empty string"
        )
    if "\n" in subject:
        raise CommitMessageSchemaError(
            "commit_message.subject must be a single line (no newlines)"
        )
    if len(subject) > COMMIT_MESSAGE_SUBJECT_MAX_CHARS:
        raise CommitMessageSchemaError(
            f"commit_message.subject exceeds "
            f"{COMMIT_MESSAGE_SUBJECT_MAX_CHARS} chars "
            f"(got {len(subject)})"
        )

    body = data["body"]
    if not isinstance(body, str):
        raise CommitMessageSchemaError(
            "commit_message.body must be a string (use \"\" for no body)"
        )
    if len(body) > COMMIT_MESSAGE_BODY_MAX_CHARS:
        raise CommitMessageSchemaError(
            f"commit_message.body exceeds "
            f"{COMMIT_MESSAGE_BODY_MAX_CHARS} chars (got {len(body)})"
        )

    commit_type = data["type"]
    if commit_type not in COMMIT_MESSAGE_TYPES:
        raise CommitMessageSchemaError(
            f"commit_message.type must be one of {COMMIT_MESSAGE_TYPES}, "
            f"got {commit_type!r}"
        )

    breaking = data["breaking"]
    if not isinstance(breaking, bool):
        raise CommitMessageSchemaError(
            f"commit_message.breaking must be a boolean, "
            f"got {type(breaking).__name__}"
        )

    scope = data.get("scope")
    if scope is not None:
        if not isinstance(scope, str) or not scope.strip():
            raise CommitMessageSchemaError(
                "commit_message.scope must be a non-empty string or null"
            )
        if any(ch.isspace() for ch in scope):
            raise CommitMessageSchemaError(
                "commit_message.scope must be a single token "
                "(no whitespace, including tabs and other unicode spaces)"
            )

    # Cross-field coherence: if the subject carries an explicit
    # Conventional Commits header (``<type>(scope)?!?: ...``), its
    # parts must agree with the structured ``type`` / ``scope`` /
    # ``breaking`` fields. This catches the failure mode where an
    # LLM emits a CC-style subject without ``!`` while flagging
    # ``breaking=true`` in the JSON object — the renderer would
    # keep the bang-less subject and the resulting commit silently
    # lies about its breaking-change status. Mirroring rule for
    # type/scope: a subject prefix that disagrees with the field is
    # a contract violation, not a friendly auto-fix opportunity.
    #
    # Subjects without a CC-style prefix are intentionally accepted
    # as raw summaries — ``render_commit_text`` builds the header
    # from the fields. Two valid shapes for the same intent:
    #   * subject="feat(api)!: drop X", type="feat", scope="api",
    #     breaking=true  ✓
    #   * subject="drop X", type="feat", scope="api",
    #     breaking=true  ✓ (renderer prepends ``feat(api)!: ``).
    #
    # Defense-in-depth gate before the coherence check: if the
    # subject *looks like* a CC header attempt (starts with
    # ``<token>(``, ``<token>!``, or ``<token>:``) but fails the
    # strict header regex, it is malformed (e.g. unclosed scope
    # paren ``feat(api: drop X``). Reject explicitly — letting
    # malformed headers through would (a) bypass the coherence
    # check below and (b) hit the parser's prefix-detection which
    # would silently treat them as already-prefixed and drop the
    # ``!`` / scope correction the fields imply.
    header = _CC_HEADER_RE.match(subject)
    if header is None and _CC_HEADER_LOOKS_LIKE_RE.match(subject):
        raise CommitMessageSchemaError(
            f"commit_message.subject looks like a malformed Conventional "
            f"Commits header ({subject!r}). Expected "
            f"``<type>(<scope>)?!?: <summary>`` with a closing scope "
            f"paren when present, or omit the prefix entirely and let "
            f"the renderer compose the header from the type/scope/"
            f"breaking fields."
        )
    if header is not None:
        header_type = header.group("type")
        header_scope = header.group("scope")
        header_breaking = header.group("bang") is not None
        if header_type != commit_type:
            raise CommitMessageSchemaError(
                f"commit_message.subject header type {header_type!r} "
                f"disagrees with commit_message.type {commit_type!r}"
            )
        if header_scope != scope:
            raise CommitMessageSchemaError(
                f"commit_message.subject header scope {header_scope!r} "
                f"disagrees with commit_message.scope {scope!r}"
            )
        if header_breaking != breaking:
            raise CommitMessageSchemaError(
                f"commit_message.subject header breaking-marker "
                f"({'!' if header_breaking else 'absent'}) disagrees "
                f"with commit_message.breaking={breaking}"
            )

    return data


COMMIT_MESSAGE_SCHEMA_DOC = """
Emit exactly one JSON object with this shape:

{
  "subject": "<one-line Conventional Commits header, <=100 chars>",
  "body":    "<optional motivation / context paragraph; use \"\" if none>",
  "type":    "feat" | "fix" | "chore" | "docs" | "refactor" | "perf" |
             "test" | "build" | "ci" | "style" | "revert",
  "scope":   "<optional single-token scope, e.g. \"auth\"; or null>",
  "breaking": true | false
}

Rules:
- subject is required, single line, <=100 chars, written as "<type>(<scope>): <imperative summary>" or "<type>: <imperative summary>" when scope is null.
- body may be empty ("") but the key must be present.
- type comes from the closed list above (Conventional Commits 1.0 base).
- scope, when present, is a short single-token noun ("auth", "control"); use null or omit when no scope applies.
- breaking is true when the change introduces an incompatible API or behaviour change; the body should explain the break.
- Output JSON only — no prose, no markdown fence, no trailing commentary.
""".strip()


# ---------------------------------------------------------------------------
# Gate pending payload — written to ``meta.commit_decision`` when the
# orchestrator pauses on ``awaiting_commit_decision``.
# ---------------------------------------------------------------------------

COMMIT_PENDING_KINDS: tuple[str, ...] = ("single", "cross_per_alias")
COMMIT_AVAILABLE_ACTIONS: tuple[str, ...] = (
    "fix", "approve", "apply", "skip", "halt",
)
COMMIT_MESSAGE_STRATEGIES: tuple[str, ...] = (
    "release_summary", "llm_generate", "operator_typed",
)
_PENDING_REQUIRED_KEYS: tuple[str, ...] = (
    "id", "kind", "project_path", "git_root",
    "release_summary", "release_verdict",
    "diff_stat", "changed_by_run", "untracked", "pre_existing_dirty",
    "available_actions", "available_strategies", "default_strategy",
    "paused_at",
)
_PENDING_OPTIONAL_KEYS: tuple[str, ...] = ("alias", "suggested_message")
_DIFF_STAT_REQUIRED_KEYS: tuple[str, ...] = (
    "files_changed", "insertions", "deletions",
)
_DIFF_STAT_OPTIONAL_KEYS: tuple[str, ...] = ("diff_path",)
_FILE_ENTRY_REQUIRED_KEYS: tuple[str, ...] = ("path",)
_FILE_ENTRY_OPTIONAL_KEYS: tuple[str, ...] = ("status",)


class CommitPendingSchemaError(ValueError):
    """Raised when a commit-decision pending payload does not match the schema."""


def validate_pending_dict(data: Any) -> dict[str, Any]:
    """Validate ``data`` against the gate pending payload schema. Returns the dict on success."""
    if not isinstance(data, dict):
        raise CommitPendingSchemaError(
            f"commit_decision pending must be a JSON object, "
            f"got {type(data).__name__}"
        )

    missing = [k for k in _PENDING_REQUIRED_KEYS if k not in data]
    if missing:
        raise CommitPendingSchemaError(
            f"commit_decision pending missing required keys: {missing}"
        )

    for key in ("id", "project_path", "git_root", "paused_at"):
        value = data[key]
        if not isinstance(value, str) or not value.strip():
            raise CommitPendingSchemaError(
                f"commit_decision pending.{key} must be a non-empty string"
            )

    kind = data["kind"]
    if kind not in COMMIT_PENDING_KINDS:
        raise CommitPendingSchemaError(
            f"commit_decision pending.kind must be one of "
            f"{COMMIT_PENDING_KINDS}, got {kind!r}"
        )

    alias = data.get("alias")
    if kind == "cross_per_alias":
        if not isinstance(alias, str) or not alias.strip():
            raise CommitPendingSchemaError(
                "commit_decision pending.alias must be a non-empty string "
                "when kind='cross_per_alias'"
            )
    elif alias is not None:
        raise CommitPendingSchemaError(
            "commit_decision pending.alias must be null when kind='single'"
        )

    release_summary = data["release_summary"]
    if not isinstance(release_summary, str):
        raise CommitPendingSchemaError(
            "commit_decision pending.release_summary must be a string"
        )
    release_verdict = data["release_verdict"]
    if release_verdict not in ("APPROVED", "REJECTED"):
        raise CommitPendingSchemaError(
            f"commit_decision pending.release_verdict must be 'APPROVED' "
            f"or 'REJECTED', got {release_verdict!r}"
        )

    _validate_diff_stat(data["diff_stat"])

    for key in ("changed_by_run", "untracked", "pre_existing_dirty"):
        entries = data[key]
        if not isinstance(entries, list):
            raise CommitPendingSchemaError(
                f"commit_decision pending.{key} must be a list"
            )
        for i, entry in enumerate(entries):
            _validate_file_entry(entry, f"{key}[{i}]")

    actions = data["available_actions"]
    if not isinstance(actions, list) or not actions:
        raise CommitPendingSchemaError(
            "commit_decision pending.available_actions must be a non-empty list"
        )
    for entry in actions:
        if entry not in COMMIT_AVAILABLE_ACTIONS:
            raise CommitPendingSchemaError(
                f"commit_decision pending.available_actions has unknown "
                f"entry {entry!r}; allowed: {COMMIT_AVAILABLE_ACTIONS}"
            )

    strategies = data["available_strategies"]
    if not isinstance(strategies, list) or not strategies:
        raise CommitPendingSchemaError(
            "commit_decision pending.available_strategies must be a "
            "non-empty list"
        )
    for entry in strategies:
        if entry not in COMMIT_MESSAGE_STRATEGIES:
            raise CommitPendingSchemaError(
                f"commit_decision pending.available_strategies has unknown "
                f"entry {entry!r}; allowed: {COMMIT_MESSAGE_STRATEGIES}"
            )

    default_strategy = data["default_strategy"]
    if default_strategy not in strategies:
        raise CommitPendingSchemaError(
            f"commit_decision pending.default_strategy {default_strategy!r} "
            f"is not in available_strategies {strategies!r}"
        )

    suggested = data.get("suggested_message")
    if suggested is not None:
        if not isinstance(suggested, dict):
            raise CommitPendingSchemaError(
                "commit_decision pending.suggested_message must be an "
                "object or null"
            )
        subj = suggested.get("subject")
        body = suggested.get("body", "")
        if not isinstance(subj, str) or not subj.strip():
            raise CommitPendingSchemaError(
                "commit_decision pending.suggested_message.subject must be a "
                "non-empty string"
            )
        if not isinstance(body, str):
            raise CommitPendingSchemaError(
                "commit_decision pending.suggested_message.body must be a "
                "string"
            )

    return data


def _validate_diff_stat(stat: Any) -> None:
    if not isinstance(stat, dict):
        raise CommitPendingSchemaError(
            "commit_decision pending.diff_stat must be an object"
        )
    missing = [k for k in _DIFF_STAT_REQUIRED_KEYS if k not in stat]
    if missing:
        raise CommitPendingSchemaError(
            f"commit_decision pending.diff_stat missing required keys: {missing}"
        )
    for key in _DIFF_STAT_REQUIRED_KEYS:
        value = stat[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise CommitPendingSchemaError(
                f"commit_decision pending.diff_stat.{key} must be a "
                f"non-negative integer"
            )
    if (
        "diff_path" in stat
        and stat["diff_path"] is not None
        and (not isinstance(stat["diff_path"], str) or not stat["diff_path"].strip())
    ):
        raise CommitPendingSchemaError(
            "commit_decision pending.diff_stat.diff_path must be a "
            "non-empty string or null"
        )


def _validate_file_entry(entry: Any, where: str) -> None:
    if not isinstance(entry, dict):
        raise CommitPendingSchemaError(
            f"commit_decision pending.{where} must be an object"
        )
    missing = [k for k in _FILE_ENTRY_REQUIRED_KEYS if k not in entry]
    if missing:
        raise CommitPendingSchemaError(
            f"commit_decision pending.{where} missing required keys: {missing}"
        )
    path = entry["path"]
    if not isinstance(path, str) or not path.strip():
        raise CommitPendingSchemaError(
            f"commit_decision pending.{where}.path must be a non-empty string"
        )
    if "status" in entry and entry["status"] is not None:
        status = entry["status"]
        if not isinstance(status, str) or not status.strip():
            raise CommitPendingSchemaError(
                f"commit_decision pending.{where}.status must be a "
                f"non-empty string or null"
            )


# ---------------------------------------------------------------------------
# Operator decision artifact — the audit record of the exact instruction.
# ---------------------------------------------------------------------------

COMMIT_DECISION_STATUSES: tuple[str, ...] = (
    "fix_requested", "committed", "applied_uncommitted", "skipped", "halted",
    "commit_failed", "apply_failed", "target_dirty",
)

_DECISION_REQUIRED_KEYS: tuple[str, ...] = (
    "run_id", "decision_id", "action", "include_untracked",
    "include_pre_existing_dirty", "files_staged", "commit_status",
    "decided_at",
)
_DECISION_OPTIONAL_KEYS: tuple[str, ...] = (
    "alias", "strategy", "final_message", "commit_sha",
    "commit_error", "note", "operator", "untracked_delivered",
    "target_dirty_paths", "target_dirty_retries",
    # Stage 6 verification delivery gate awareness (ADR 0083). Additive audit
    # keys, written only when non-empty; absent on no-contract artifacts.
    "verification_policy", "verification_missing", "verification_failed",
    "verification_stale", "generated_garbage_paths",
)


class CommitDecisionSchemaError(ValueError):
    """Raised when a commit-decision artifact does not match the schema."""


def _forbid_stale_target_dirty_paths(
    target_dirty_paths: Any, commit_status: str,
) -> None:
    """Refuse stale dirty-paths on success / executor-failure artifacts.

    Once the delivery either succeeded (committed / applied_uncommitted)
    or failed in the executor after a clean check (commit_failed /
    apply_failed), any earlier dirty-target state is no longer the
    current truth. Persisting the porcelain lines would mislead the
    audit reader. Only ``target_dirty`` and the dirty-prompt
    skip/halt artifacts carry paths.
    """
    if target_dirty_paths is not None and target_dirty_paths != []:
        raise CommitDecisionSchemaError(
            f"commit_decision artifact.target_dirty_paths must be absent "
            f"or empty when commit_status={commit_status!r}"
        )


def _validate_target_dirty_coherence(
    sha: Any, error: Any, target_dirty_paths: Any,
) -> None:
    """Shared coherence rule for commit_status='target_dirty' artifacts.

    Used from both action='approve' and action='apply' branches: the
    delivery refused to write because project_dir was dirty, so there
    is no commit sha, no executor error, and the porcelain dirty paths
    must be present to explain the block.
    """
    if sha is not None:
        raise CommitDecisionSchemaError(
            "commit_decision artifact.commit_sha must be null when "
            "commit_status='target_dirty'"
        )
    if error is not None:
        raise CommitDecisionSchemaError(
            "commit_decision artifact.commit_error must be null when "
            "commit_status='target_dirty'"
        )
    if not isinstance(target_dirty_paths, list) or not target_dirty_paths:
        raise CommitDecisionSchemaError(
            "commit_decision artifact.target_dirty_paths must be a "
            "non-empty list when commit_status='target_dirty'"
        )


def validate_decision_dict(data: Any) -> dict[str, Any]:
    """Validate ``data`` against the commit-decision artifact schema. Returns the dict on success."""
    if not isinstance(data, dict):
        raise CommitDecisionSchemaError(
            f"commit_decision artifact must be a JSON object, "
            f"got {type(data).__name__}"
        )
    missing = [k for k in _DECISION_REQUIRED_KEYS if k not in data]
    if missing:
        raise CommitDecisionSchemaError(
            f"commit_decision artifact missing required keys: {missing}"
        )
    unknown = sorted(
        set(data.keys())
        - set(_DECISION_REQUIRED_KEYS)
        - set(_DECISION_OPTIONAL_KEYS)
    )
    if unknown:
        raise CommitDecisionSchemaError(
            f"commit_decision artifact has unknown keys: {unknown}"
        )

    for key in ("run_id", "decision_id", "decided_at"):
        value = data[key]
        if not isinstance(value, str) or not value.strip():
            raise CommitDecisionSchemaError(
                f"commit_decision artifact.{key} must be a non-empty string"
            )

    action = data["action"]
    if action not in COMMIT_AVAILABLE_ACTIONS:
        raise CommitDecisionSchemaError(
            f"commit_decision artifact.action must be one of "
            f"{COMMIT_AVAILABLE_ACTIONS}, got {action!r}"
        )

    strategy = data.get("strategy")
    if action == "approve":
        if strategy not in COMMIT_MESSAGE_STRATEGIES:
            raise CommitDecisionSchemaError(
                f"commit_decision artifact.strategy must be one of "
                f"{COMMIT_MESSAGE_STRATEGIES} when action='approve', "
                f"got {strategy!r}"
            )
    elif strategy is not None:
        raise CommitDecisionSchemaError(
            "commit_decision artifact.strategy must be null unless "
            "action='approve'"
        )

    for key in ("include_untracked", "include_pre_existing_dirty"):
        if not isinstance(data[key], bool):
            raise CommitDecisionSchemaError(
                f"commit_decision artifact.{key} must be a boolean"
            )

    files_staged = data["files_staged"]
    if not isinstance(files_staged, list):
        raise CommitDecisionSchemaError(
            "commit_decision artifact.files_staged must be a list"
        )
    for i, entry in enumerate(files_staged):
        if not isinstance(entry, str) or not entry.strip():
            raise CommitDecisionSchemaError(
                f"commit_decision artifact.files_staged[{i}] must be a "
                f"non-empty string"
            )

    untracked_delivered = data.get("untracked_delivered")
    if untracked_delivered is not None:
        if not isinstance(untracked_delivered, list):
            raise CommitDecisionSchemaError(
                "commit_decision artifact.untracked_delivered must be a list"
            )
        for i, entry in enumerate(untracked_delivered):
            if not isinstance(entry, str) or not entry.strip():
                raise CommitDecisionSchemaError(
                    f"commit_decision artifact.untracked_delivered[{i}] "
                    "must be a non-empty string"
                )

    target_dirty_paths = data.get("target_dirty_paths")
    if target_dirty_paths is not None:
        if not isinstance(target_dirty_paths, list):
            raise CommitDecisionSchemaError(
                "commit_decision artifact.target_dirty_paths must be a list"
            )
        for i, entry in enumerate(target_dirty_paths):
            if not isinstance(entry, str) or not entry.strip():
                raise CommitDecisionSchemaError(
                    f"commit_decision artifact.target_dirty_paths[{i}] "
                    "must be a non-empty string"
                )

    target_dirty_retries = data.get("target_dirty_retries")
    if target_dirty_retries is not None and (
        isinstance(target_dirty_retries, bool)
        or not isinstance(target_dirty_retries, int)
        or target_dirty_retries < 0
    ):
        raise CommitDecisionSchemaError(
            "commit_decision artifact.target_dirty_retries must be "
            "a non-negative integer"
        )

    verification_policy = data.get("verification_policy")
    if verification_policy is not None and (
        not isinstance(verification_policy, str)
        or not verification_policy.strip()
    ):
        raise CommitDecisionSchemaError(
            "commit_decision artifact.verification_policy must be a "
            "non-empty string or null"
        )
    for key in (
        "verification_missing", "verification_failed",
        "verification_stale", "generated_garbage_paths",
    ):
        entries = data.get(key)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise CommitDecisionSchemaError(
                f"commit_decision artifact.{key} must be a list"
            )
        for i, entry in enumerate(entries):
            if not isinstance(entry, str) or not entry.strip():
                raise CommitDecisionSchemaError(
                    f"commit_decision artifact.{key}[{i}] must be a "
                    f"non-empty string"
                )

    commit_status = data["commit_status"]
    if commit_status not in COMMIT_DECISION_STATUSES:
        raise CommitDecisionSchemaError(
            f"commit_decision artifact.commit_status must be one of "
            f"{COMMIT_DECISION_STATUSES}, got {commit_status!r}"
        )

    # Cross-field coherence between action / commit_status / commit_sha /
    # commit_error. The artifact is the audit record; a contradictory
    # status / sha pair would let resume trust the wrong outcome.
    sha = data.get("commit_sha")
    error = data.get("commit_error")
    if action == "fix":
        if commit_status != "fix_requested":
            raise CommitDecisionSchemaError(
                "commit_decision artifact.commit_status must be "
                "'fix_requested' when action='fix'"
            )
        if sha is not None or error is not None:
            raise CommitDecisionSchemaError(
                "commit_decision artifact.commit_sha and commit_error must "
                "be null when action='fix'"
            )
    elif action == "approve":
        if commit_status not in ("committed", "commit_failed", "target_dirty"):
            raise CommitDecisionSchemaError(
                f"commit_decision artifact.commit_status must be "
                f"'committed', 'commit_failed', or 'target_dirty' when "
                f"action='approve', got {commit_status!r}"
            )
        if commit_status == "committed":
            if not isinstance(sha, str) or not sha.strip():
                raise CommitDecisionSchemaError(
                    "commit_decision artifact.commit_sha must be a "
                    "non-empty string when commit_status='committed'"
                )
            if error is not None:
                raise CommitDecisionSchemaError(
                    "commit_decision artifact.commit_error must be null "
                    "when commit_status='committed'"
                )
            _forbid_stale_target_dirty_paths(target_dirty_paths, commit_status)
        elif commit_status == "commit_failed":
            if sha is not None:
                raise CommitDecisionSchemaError(
                    "commit_decision artifact.commit_sha must be null when "
                    "commit_status='commit_failed'"
                )
            if not isinstance(error, str) or not error.strip():
                raise CommitDecisionSchemaError(
                    "commit_decision artifact.commit_error must be a "
                    "non-empty string when commit_status='commit_failed'"
                )
            _forbid_stale_target_dirty_paths(target_dirty_paths, commit_status)
        else:  # target_dirty
            _validate_target_dirty_coherence(sha, error, target_dirty_paths)
    elif action == "apply":
        if commit_status not in (
            "applied_uncommitted", "apply_failed", "target_dirty",
        ):
            raise CommitDecisionSchemaError(
                f"commit_decision artifact.commit_status must be "
                f"'applied_uncommitted', 'apply_failed', or 'target_dirty' "
                f"when action='apply', got {commit_status!r}"
            )
        if sha is not None:
            raise CommitDecisionSchemaError(
                "commit_decision artifact.commit_sha must be null when "
                "action='apply'"
            )
        if commit_status == "applied_uncommitted":
            if error is not None:
                raise CommitDecisionSchemaError(
                    "commit_decision artifact.commit_error must be null "
                    "when commit_status='applied_uncommitted'"
                )
            _forbid_stale_target_dirty_paths(target_dirty_paths, commit_status)
        elif commit_status == "apply_failed":
            if not isinstance(error, str) or not error.strip():
                raise CommitDecisionSchemaError(
                    "commit_decision artifact.commit_error must be a "
                    "non-empty string when commit_status='apply_failed'"
                )
            _forbid_stale_target_dirty_paths(target_dirty_paths, commit_status)
        else:  # target_dirty
            _validate_target_dirty_coherence(sha, error, target_dirty_paths)
    elif action == "skip":
        if commit_status != "skipped":
            raise CommitDecisionSchemaError(
                "commit_decision artifact.commit_status must be 'skipped' "
                "when action='skip'"
            )
        if sha is not None or error is not None:
            raise CommitDecisionSchemaError(
                "commit_decision artifact.commit_sha and commit_error must "
                "be null when action='skip'"
            )
    else:  # halt
        if commit_status != "halted":
            raise CommitDecisionSchemaError(
                "commit_decision artifact.commit_status must be 'halted' "
                "when action='halt'"
            )
        if sha is not None or error is not None:
            raise CommitDecisionSchemaError(
                "commit_decision artifact.commit_sha and commit_error must "
                "be null when action='halt'"
            )

    final_message = data.get("final_message")
    if action == "approve" and commit_status == "committed":
        if not isinstance(final_message, str) or not final_message.strip():
            raise CommitDecisionSchemaError(
                "commit_decision artifact.final_message must be a non-empty "
                "string when commit_status='committed'"
            )
    elif final_message is not None and not isinstance(final_message, str):
        raise CommitDecisionSchemaError(
            "commit_decision artifact.final_message must be a string or null"
        )

    note = data.get("note")
    if note is not None and not isinstance(note, str):
        raise CommitDecisionSchemaError(
            "commit_decision artifact.note must be a string or null"
        )

    operator = data.get("operator")
    if operator is not None and not isinstance(operator, str):
        raise CommitDecisionSchemaError(
            "commit_decision artifact.operator must be a string or null"
        )

    alias = data.get("alias")
    if alias is not None and (not isinstance(alias, str) or not alias.strip()):
        raise CommitDecisionSchemaError(
            "commit_decision artifact.alias must be a non-empty string or null"
        )

    return data


__all__ = [
    "COMMIT_AVAILABLE_ACTIONS",
    "COMMIT_DECISION_STATUSES",
    "COMMIT_MESSAGE_BODY_MAX_CHARS",
    "COMMIT_MESSAGE_SCHEMA_DOC",
    "COMMIT_MESSAGE_STRATEGIES",
    "COMMIT_MESSAGE_SUBJECT_MAX_CHARS",
    "COMMIT_MESSAGE_TYPES",
    "COMMIT_PENDING_KINDS",
    "CommitDecisionSchemaError",
    "CommitMessageSchemaError",
    "CommitPendingSchemaError",
    "validate_commit_message_dict",
    "validate_decision_dict",
    "validate_pending_dict",
]
