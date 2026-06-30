"""Tests for the persisted commit-decision payloads in
:mod:`core.contracts.commit_decision_schema`:

* :func:`validate_pending_dict` — the gate's active payload written to
  ``meta.commit_decision`` while the orchestrator pauses on
  ``awaiting_commit_decision``.
* :func:`validate_decision_dict` — the audit-record artifact the
  operator's decision produces under ``commit_decisions/<id>.json``.

The LLM-output ``validate_commit_message_dict`` contract is covered in
:mod:`tests.unit.pipeline.test_commit_message_parser`.
"""
from __future__ import annotations

from typing import Any

import pytest

from core.contracts.commit_decision_schema import (
    CommitDecisionSchemaError,
    CommitPendingSchemaError,
    validate_decision_dict,
    validate_pending_dict,
)


def _minimal_pending(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "p1",
        "kind": "single",
        "project_path": "/p",
        "git_root": "/p",
        "release_summary": "shipped feature",
        "release_verdict": "APPROVED",
        "diff_stat": {
            "files_changed": 3,
            "insertions": 10,
            "deletions": 2,
        },
        "changed_by_run": [{"path": "a.py", "status": "M"}],
        "untracked": [],
        "pre_existing_dirty": [],
        "available_actions": ["fix", "approve", "apply", "skip", "halt"],
        "available_strategies": ["release_summary", "llm_generate"],
        "default_strategy": "release_summary",
        "paused_at": "2026-05-21T10:00:00Z",
        "alias": None,
    }
    base.update(overrides)
    return base


def _minimal_decision(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": "r1",
        "decision_id": "d1",
        "action": "approve",
        "include_untracked": False,
        "include_pre_existing_dirty": False,
        "files_staged": ["a.py"],
        "commit_status": "committed",
        "decided_at": "2026-05-21T10:01:00Z",
        "strategy": "release_summary",
        "final_message": "feat: do a thing",
        "commit_sha": "abc1234",
    }
    base.update(overrides)
    return base


class TestValidatePendingDict:
    def test_happy_path_single(self) -> None:
        validate_pending_dict(_minimal_pending())

    def test_happy_path_cross_per_alias(self) -> None:
        validate_pending_dict(_minimal_pending(
            kind="cross_per_alias", alias="svc-a",
        ))

    def test_happy_path_with_suggested_message(self) -> None:
        validate_pending_dict(_minimal_pending(
            suggested_message={"subject": "feat: x", "body": ""},
        ))

    def test_happy_path_with_diff_path(self) -> None:
        validate_pending_dict(_minimal_pending(
            diff_stat={
                "files_changed": 1,
                "insertions": 1,
                "deletions": 1,
                "diff_path": "/runs/r1/diff.patch",
            },
        ))

    @pytest.mark.parametrize("bad", [[], "x", None, 42])
    def test_non_object_rejected(self, bad: Any) -> None:
        with pytest.raises(CommitPendingSchemaError, match="JSON object"):
            validate_pending_dict(bad)

    @pytest.mark.parametrize(
        "missing_key",
        [
            "id", "kind", "project_path", "git_root",
            "release_summary", "release_verdict",
            "diff_stat", "changed_by_run", "untracked",
            "pre_existing_dirty",
            "available_actions", "available_strategies",
            "default_strategy", "paused_at",
        ],
    )
    def test_missing_required_key(self, missing_key: str) -> None:
        data = _minimal_pending()
        del data[missing_key]
        with pytest.raises(CommitPendingSchemaError, match="missing required keys"):
            validate_pending_dict(data)

    @pytest.mark.parametrize(
        "blank_key", ["id", "project_path", "git_root", "paused_at"],
    )
    def test_blank_string_field_rejected(self, blank_key: str) -> None:
        with pytest.raises(CommitPendingSchemaError, match=blank_key):
            validate_pending_dict(_minimal_pending(**{blank_key: "   "}))

    def test_invalid_kind_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="kind"):
            validate_pending_dict(_minimal_pending(kind="bogus"))

    def test_single_with_alias_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="alias.*null"):
            validate_pending_dict(_minimal_pending(
                kind="single", alias="svc-a",
            ))

    def test_cross_per_alias_without_alias_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="alias"):
            validate_pending_dict(_minimal_pending(
                kind="cross_per_alias", alias=None,
            ))

    def test_cross_per_alias_blank_alias_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="alias"):
            validate_pending_dict(_minimal_pending(
                kind="cross_per_alias", alias="   ",
            ))

    def test_release_verdict_not_in_enum_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="release_verdict"):
            validate_pending_dict(_minimal_pending(release_verdict="LGTM"))

    def test_release_summary_non_string_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="release_summary"):
            validate_pending_dict(_minimal_pending(release_summary=42))

    def test_diff_stat_non_object_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="diff_stat"):
            validate_pending_dict(_minimal_pending(diff_stat=[]))

    def test_diff_stat_negative_int_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="non-negative"):
            validate_pending_dict(_minimal_pending(diff_stat={
                "files_changed": -1, "insertions": 0, "deletions": 0,
            }))

    def test_diff_stat_bool_not_accepted_as_int(self) -> None:
        # Python ``bool ⊂ int`` — guard explicitly.
        with pytest.raises(CommitPendingSchemaError, match="non-negative"):
            validate_pending_dict(_minimal_pending(diff_stat={
                "files_changed": True, "insertions": 0, "deletions": 0,
            }))

    def test_diff_stat_missing_required_key_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="diff_stat"):
            validate_pending_dict(_minimal_pending(diff_stat={
                "files_changed": 1, "insertions": 1,
            }))

    def test_diff_stat_blank_diff_path_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="diff_path"):
            validate_pending_dict(_minimal_pending(diff_stat={
                "files_changed": 1, "insertions": 1, "deletions": 1,
                "diff_path": "  ",
            }))

    @pytest.mark.parametrize(
        "list_key", ["changed_by_run", "untracked", "pre_existing_dirty"],
    )
    def test_file_list_must_be_list(self, list_key: str) -> None:
        with pytest.raises(CommitPendingSchemaError, match=list_key):
            validate_pending_dict(_minimal_pending(**{list_key: "not a list"}))

    def test_file_entry_without_path_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="missing required"):
            validate_pending_dict(_minimal_pending(
                changed_by_run=[{"status": "M"}],
            ))

    def test_file_entry_empty_path_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="path"):
            validate_pending_dict(_minimal_pending(
                changed_by_run=[{"path": "   "}],
            ))

    def test_file_entry_blank_status_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="status"):
            validate_pending_dict(_minimal_pending(
                changed_by_run=[{"path": "a.py", "status": "   "}],
            ))

    def test_empty_available_actions_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="available_actions"):
            validate_pending_dict(_minimal_pending(available_actions=[]))

    def test_unknown_action_in_available_actions_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="available_actions"):
            validate_pending_dict(_minimal_pending(
                available_actions=["approve", "weird"],
            ))

    def test_empty_available_strategies_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="available_strategies"):
            validate_pending_dict(_minimal_pending(
                available_strategies=[],
                default_strategy="release_summary",
            ))

    def test_unknown_strategy_in_available_strategies_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="available_strategies"):
            validate_pending_dict(_minimal_pending(
                available_strategies=["weird"],
                default_strategy="weird",
            ))

    def test_default_strategy_not_in_available_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="default_strategy"):
            validate_pending_dict(_minimal_pending(
                available_strategies=["release_summary"],
                default_strategy="operator_typed",
            ))

    def test_suggested_message_non_object_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="suggested_message"):
            validate_pending_dict(_minimal_pending(suggested_message="x"))

    def test_suggested_message_empty_subject_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="subject"):
            validate_pending_dict(_minimal_pending(
                suggested_message={"subject": "", "body": ""},
            ))

    def test_suggested_message_non_string_body_rejected(self) -> None:
        with pytest.raises(CommitPendingSchemaError, match="body"):
            validate_pending_dict(_minimal_pending(
                suggested_message={"subject": "feat: x", "body": 42},
            ))


class TestValidateDecisionDict:
    def test_happy_path_approve_committed(self) -> None:
        validate_decision_dict(_minimal_decision())

    def test_happy_path_fix_requested(self) -> None:
        validate_decision_dict(_minimal_decision(
            action="fix",
            commit_status="fix_requested",
            strategy=None,
            final_message=None,
            commit_sha=None,
            files_staged=[],
        ))

    def test_happy_path_with_untracked_delivered(self) -> None:
        validate_decision_dict(_minimal_decision(
            untracked_delivered=["new.txt"],
        ))

    def test_happy_path_approve_commit_failed(self) -> None:
        validate_decision_dict(_minimal_decision(
            commit_status="commit_failed",
            commit_sha=None,
            commit_error="git push failed",
            final_message=None,
        ))

    def test_happy_path_apply_uncommitted(self) -> None:
        validate_decision_dict(_minimal_decision(
            action="apply",
            commit_status="applied_uncommitted",
            strategy=None,
            final_message=None,
            commit_sha=None,
            files_staged=[],
        ))

    def test_happy_path_apply_failed(self) -> None:
        validate_decision_dict(_minimal_decision(
            action="apply",
            commit_status="apply_failed",
            strategy=None,
            final_message=None,
            commit_sha=None,
            commit_error="patch does not apply",
            files_staged=[],
        ))

    def test_happy_path_skip(self) -> None:
        validate_decision_dict(_minimal_decision(
            action="skip",
            commit_status="skipped",
            strategy=None,
            final_message=None,
            commit_sha=None,
            files_staged=[],
        ))

    def test_happy_path_halt(self) -> None:
        validate_decision_dict(_minimal_decision(
            action="halt",
            commit_status="halted",
            strategy=None,
            final_message=None,
            commit_sha=None,
            files_staged=[],
        ))

    def test_happy_path_with_alias_operator_note(self) -> None:
        validate_decision_dict(_minimal_decision(
            alias="svc-a", operator="ci-bot", note="retry tomorrow",
        ))

    @pytest.mark.parametrize("bad", [[], "x", None, 42])
    def test_non_object_rejected(self, bad: Any) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="JSON object"):
            validate_decision_dict(bad)

    @pytest.mark.parametrize(
        "missing_key",
        [
            "run_id", "decision_id", "action",
            "include_untracked", "include_pre_existing_dirty",
            "files_staged", "commit_status", "decided_at",
        ],
    )
    def test_missing_required_key(self, missing_key: str) -> None:
        data = _minimal_decision()
        del data[missing_key]
        with pytest.raises(CommitDecisionSchemaError, match="missing required keys"):
            validate_decision_dict(data)

    def test_unknown_extra_key_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="unknown keys"):
            validate_decision_dict(_minimal_decision(extra="x"))

    @pytest.mark.parametrize(
        "blank_key", ["run_id", "decision_id", "decided_at"],
    )
    def test_blank_string_field_rejected(self, blank_key: str) -> None:
        with pytest.raises(CommitDecisionSchemaError, match=blank_key):
            validate_decision_dict(_minimal_decision(**{blank_key: "   "}))

    def test_invalid_action_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="action"):
            validate_decision_dict(_minimal_decision(action="bogus"))

    def test_invalid_commit_status_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_status"):
            validate_decision_dict(_minimal_decision(commit_status="bogus"))

    # Cross-field coherence — fix.

    def test_fix_with_skipped_status_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_status"):
            validate_decision_dict(_minimal_decision(
                action="fix",
                commit_status="skipped",
                strategy=None,
                final_message=None,
                commit_sha=None,
                files_staged=[],
            ))

    def test_fix_with_sha_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_sha"):
            validate_decision_dict(_minimal_decision(
                action="fix",
                commit_status="fix_requested",
                strategy=None,
                final_message=None,
                commit_sha="abc",
                files_staged=[],
            ))

    def test_fix_with_error_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_sha and commit_error"):
            validate_decision_dict(_minimal_decision(
                action="fix",
                commit_status="fix_requested",
                strategy=None,
                final_message=None,
                commit_sha=None,
                commit_error="oops",
                files_staged=[],
            ))

    def test_fix_with_strategy_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="strategy"):
            validate_decision_dict(_minimal_decision(
                action="fix",
                commit_status="fix_requested",
                strategy="release_summary",
                final_message=None,
                commit_sha=None,
                files_staged=[],
            ))

    # Cross-field coherence — approve.

    def test_approve_with_skipped_status_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_status"):
            validate_decision_dict(_minimal_decision(
                commit_status="skipped", commit_sha=None, final_message=None,
            ))

    def test_approve_without_strategy_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="strategy"):
            validate_decision_dict(_minimal_decision(strategy=None))

    def test_approve_with_bogus_strategy_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="strategy"):
            validate_decision_dict(_minimal_decision(strategy="bogus"))

    def test_approve_committed_without_sha_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_sha"):
            validate_decision_dict(_minimal_decision(commit_sha=None))

    def test_approve_committed_with_error_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_error"):
            validate_decision_dict(_minimal_decision(commit_error="oops"))

    def test_approve_committed_without_final_message_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="final_message"):
            validate_decision_dict(_minimal_decision(final_message=None))

    def test_approve_committed_with_empty_final_message_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="final_message"):
            validate_decision_dict(_minimal_decision(final_message="   "))

    def test_approve_commit_failed_with_sha_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_sha"):
            validate_decision_dict(_minimal_decision(
                commit_status="commit_failed",
                commit_sha="abc",
                commit_error="oops",
                final_message=None,
            ))

    def test_approve_commit_failed_with_empty_error_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_error"):
            validate_decision_dict(_minimal_decision(
                commit_status="commit_failed",
                commit_sha=None,
                commit_error="   ",
                final_message=None,
            ))

    # Cross-field coherence — apply.

    def test_apply_with_committed_status_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_status"):
            validate_decision_dict(_minimal_decision(
                action="apply",
                commit_status="committed",
                strategy=None,
                final_message=None,
            ))

    def test_apply_with_sha_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_sha"):
            validate_decision_dict(_minimal_decision(
                action="apply",
                commit_status="applied_uncommitted",
                strategy=None,
                final_message=None,
                commit_sha="abc",
            ))

    def test_apply_success_with_error_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_error"):
            validate_decision_dict(_minimal_decision(
                action="apply",
                commit_status="applied_uncommitted",
                strategy=None,
                final_message=None,
                commit_sha=None,
                commit_error="oops",
            ))

    def test_apply_failed_without_error_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_error"):
            validate_decision_dict(_minimal_decision(
                action="apply",
                commit_status="apply_failed",
                strategy=None,
                final_message=None,
                commit_sha=None,
            ))

    def test_apply_with_strategy_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="strategy"):
            validate_decision_dict(_minimal_decision(
                action="apply",
                commit_status="applied_uncommitted",
                strategy="release_summary",
                final_message=None,
                commit_sha=None,
            ))

    # Cross-field coherence — skip.

    def test_skip_with_committed_status_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_status"):
            validate_decision_dict(_minimal_decision(
                action="skip",
                commit_status="committed",
                strategy=None,
                final_message=None,
                commit_sha="abc",
            ))

    def test_skip_with_sha_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_sha"):
            validate_decision_dict(_minimal_decision(
                action="skip",
                commit_status="skipped",
                strategy=None,
                final_message=None,
                commit_sha="abc",
            ))

    def test_skip_with_error_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_sha and commit_error"):
            validate_decision_dict(_minimal_decision(
                action="skip",
                commit_status="skipped",
                strategy=None,
                final_message=None,
                commit_sha=None,
                commit_error="oops",
            ))

    def test_skip_with_strategy_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="strategy"):
            validate_decision_dict(_minimal_decision(
                action="skip",
                commit_status="skipped",
                strategy="release_summary",
                final_message=None,
                commit_sha=None,
            ))

    # Cross-field coherence — halt.

    def test_halt_with_skipped_status_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_status"):
            validate_decision_dict(_minimal_decision(
                action="halt",
                commit_status="skipped",
                strategy=None,
                final_message=None,
                commit_sha=None,
            ))

    def test_halt_with_sha_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_sha"):
            validate_decision_dict(_minimal_decision(
                action="halt",
                commit_status="halted",
                strategy=None,
                final_message=None,
                commit_sha="abc",
            ))

    def test_halt_with_error_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="commit_sha and commit_error"):
            validate_decision_dict(_minimal_decision(
                action="halt",
                commit_status="halted",
                strategy=None,
                final_message=None,
                commit_sha=None,
                commit_error="oops",
            ))

    def test_halt_with_strategy_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="strategy"):
            validate_decision_dict(_minimal_decision(
                action="halt",
                commit_status="halted",
                strategy="release_summary",
                final_message=None,
                commit_sha=None,
            ))

    # Type / list-shape rejections.

    @pytest.mark.parametrize(
        "key", ["include_untracked", "include_pre_existing_dirty"],
    )
    def test_include_flags_must_be_bool(self, key: str) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="boolean"):
            validate_decision_dict(_minimal_decision(**{key: "true"}))

    def test_files_staged_must_be_list(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="files_staged"):
            validate_decision_dict(_minimal_decision(files_staged="a.py"))

    def test_files_staged_empty_string_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="files_staged"):
            validate_decision_dict(_minimal_decision(files_staged=[""]))

    def test_files_staged_non_string_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="files_staged"):
            validate_decision_dict(_minimal_decision(files_staged=[123]))

    def test_untracked_delivered_must_be_list(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="untracked_delivered"):
            validate_decision_dict(_minimal_decision(untracked_delivered="new.txt"))

    def test_untracked_delivered_empty_string_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="untracked_delivered"):
            validate_decision_dict(_minimal_decision(untracked_delivered=[""]))

    # Optional-field shape.

    def test_alias_blank_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="alias"):
            validate_decision_dict(_minimal_decision(alias=""))

    def test_alias_none_accepted(self) -> None:
        validate_decision_dict(_minimal_decision(alias=None))

    def test_operator_non_string_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="operator"):
            validate_decision_dict(_minimal_decision(operator=42))

    def test_note_non_string_rejected(self) -> None:
        with pytest.raises(CommitDecisionSchemaError, match="note"):
            validate_decision_dict(_minimal_decision(note=42))

    # ── target_dirty (B1.2) ────────────────────────────────────────────────

    def test_target_dirty_valid_for_approve(self) -> None:
        """Approve + target_dirty mirrors what the engine actually persists:
        strategy/final_message stay set (the engine records the strategy
        that WOULD have applied), sha/error are null, paths populated."""
        validate_decision_dict(_minimal_decision(
            action="approve",
            commit_status="target_dirty",
            commit_sha=None,
            commit_error=None,
            files_staged=[],
            target_dirty_paths=[" M src/foo.py", "?? scratch.txt"],
            target_dirty_retries=0,
        ))

    def test_target_dirty_valid_for_apply(self) -> None:
        validate_decision_dict(_minimal_decision(
            action="apply",
            commit_status="target_dirty",
            commit_sha=None,
            commit_error=None,
            strategy=None,
            final_message=None,
            files_staged=[],
            target_dirty_paths=[" M src/foo.py"],
            target_dirty_retries=2,
        ))

    def test_target_dirty_requires_non_empty_paths(self) -> None:
        with pytest.raises(
            CommitDecisionSchemaError, match="target_dirty_paths must be a non-empty list",
        ):
            validate_decision_dict(_minimal_decision(
                action="approve",
                commit_status="target_dirty",
                commit_sha=None,
                commit_error=None,
                files_staged=[],
                target_dirty_paths=[],
            ))

    def test_target_dirty_rejects_non_null_sha(self) -> None:
        with pytest.raises(
            CommitDecisionSchemaError, match="commit_sha must be null",
        ):
            validate_decision_dict(_minimal_decision(
                action="approve",
                commit_status="target_dirty",
                commit_sha="abc1234",
                commit_error=None,
                files_staged=[],
                target_dirty_paths=[" M a.py"],
            ))

    def test_target_dirty_rejects_non_null_error(self) -> None:
        with pytest.raises(
            CommitDecisionSchemaError, match="commit_error must be null",
        ):
            validate_decision_dict(_minimal_decision(
                action="apply",
                commit_status="target_dirty",
                commit_sha=None,
                commit_error="boom",
                strategy=None,
                final_message=None,
                files_staged=[],
                target_dirty_paths=[" M a.py"],
            ))

    def test_committed_with_target_dirty_retries_only_is_valid(self) -> None:
        """Retry-success: operator chose retry, checkout became clean, commit
        succeeded. The artifact records the retry count but no stale paths."""
        validate_decision_dict(_minimal_decision(
            action="approve",
            commit_status="committed",
            commit_sha="abc1234",
            target_dirty_retries=1,
        ))

    def test_committed_with_stale_target_dirty_paths_rejected(self) -> None:
        with pytest.raises(
            CommitDecisionSchemaError, match="target_dirty_paths must be absent",
        ):
            validate_decision_dict(_minimal_decision(
                action="approve",
                commit_status="committed",
                commit_sha="abc1234",
                target_dirty_paths=[" M a.py"],
            ))

    def test_applied_uncommitted_with_stale_target_dirty_paths_rejected(self) -> None:
        with pytest.raises(
            CommitDecisionSchemaError, match="target_dirty_paths must be absent",
        ):
            validate_decision_dict(_minimal_decision(
                action="apply",
                commit_status="applied_uncommitted",
                commit_sha=None,
                strategy=None,
                final_message=None,
                target_dirty_paths=[" M a.py"],
            ))

    def test_target_dirty_rejected_when_action_is_skip(self) -> None:
        with pytest.raises(
            CommitDecisionSchemaError, match="commit_status must be 'skipped'",
        ):
            validate_decision_dict(_minimal_decision(
                action="skip",
                commit_status="target_dirty",
                commit_sha=None,
                strategy=None,
                final_message=None,
                files_staged=[],
                target_dirty_paths=[" M a.py"],
            ))

    def test_target_dirty_rejected_when_action_is_halt(self) -> None:
        with pytest.raises(
            CommitDecisionSchemaError, match="commit_status must be 'halted'",
        ):
            validate_decision_dict(_minimal_decision(
                action="halt",
                commit_status="target_dirty",
                commit_sha=None,
                strategy=None,
                final_message=None,
                files_staged=[],
                target_dirty_paths=[" M a.py"],
            ))

    def test_target_dirty_retries_negative_rejected(self) -> None:
        with pytest.raises(
            CommitDecisionSchemaError, match="target_dirty_retries",
        ):
            validate_decision_dict(_minimal_decision(
                target_dirty_retries=-1,
            ))

    def test_target_dirty_retries_non_int_rejected(self) -> None:
        with pytest.raises(
            CommitDecisionSchemaError, match="target_dirty_retries",
        ):
            validate_decision_dict(_minimal_decision(
                target_dirty_retries="lots",
            ))

    def test_target_dirty_retries_bool_rejected(self) -> None:
        """bool is a subclass of int — must be excluded explicitly."""
        with pytest.raises(
            CommitDecisionSchemaError, match="target_dirty_retries",
        ):
            validate_decision_dict(_minimal_decision(
                target_dirty_retries=True,
            ))

    def test_target_dirty_paths_must_be_list(self) -> None:
        with pytest.raises(
            CommitDecisionSchemaError, match="target_dirty_paths must be a list",
        ):
            validate_decision_dict(_minimal_decision(
                target_dirty_paths=" M a.py",  # string, not list
            ))

    def test_target_dirty_paths_blank_string_rejected(self) -> None:
        with pytest.raises(
            CommitDecisionSchemaError, match="target_dirty_paths",
        ):
            validate_decision_dict(_minimal_decision(
                target_dirty_paths=["   "],
            ))
