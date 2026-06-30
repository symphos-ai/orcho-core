"""Tests for ``pipeline.control.resume_context``."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.control.resume_context import (
    FollowupResumeFields,
    LatestRunNotFound,
    ResumeContextError,
    ResumedMeta,
    ResumeMode,
    build_followup_resume_fields,
    classify_resume_mode,
    extract_cross_followup_session_seeds,
    extract_followup_session_seeds,
    get_resume_intent_options,
    is_awaiting_commit_decision,
    is_terminal_commit_decision_fix,
    is_terminal_commit_decision_halt,
    is_terminal_final_acceptance_rejected,
    is_terminal_phase_handoff_halt,
    is_terminal_resume_parent,
    is_terminal_success,
    load_resume_meta,
    meta_run_kind,
    resolve_latest_run,
    resolve_project,
    resolve_projects_argv,
    resolve_resume_profile,
    resolve_task,
)


class TestLoadResumeMeta:
    def test_returns_none_when_meta_absent(self, tmp_path: Path) -> None:
        # Pause may happen before meta.json is written (very early
        # crash, legacy run). Loader returns None rather than failing
        # so the orchestrator can fall back to CLI args.
        assert load_resume_meta(tmp_path) is None

    def test_reads_valid_json(self, tmp_path: Path) -> None:
        meta = {"task": "T", "project": "/p"}
        (tmp_path / "meta.json").write_text(
            json.dumps(meta), encoding="utf-8",
        )
        out = load_resume_meta(tmp_path)
        assert isinstance(out, ResumedMeta)
        assert out.meta == meta

    def test_malformed_json_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "meta.json").write_text("not json", encoding="utf-8")
        with pytest.raises(ResumeContextError, match="cannot read"):
            load_resume_meta(tmp_path)

    def test_non_object_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "meta.json").write_text("[]", encoding="utf-8")
        with pytest.raises(ResumeContextError, match="not a JSON object"):
            load_resume_meta(tmp_path)


class TestResolveTask:
    def test_task_file_wins(self, tmp_path: Path) -> None:
        f = tmp_path / "t.md"
        f.write_text("from file\n", encoding="utf-8")
        out = resolve_task(
            explicit_task="from cli",
            explicit_task_file=str(f),
            resumed=None,
        )
        assert out == "from file"

    def test_bare_md_task_file_resolves_from_project_task_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "project"
        task_dir = project / ".orcho" / ".task-files"
        task_dir.mkdir(parents=True)
        (task_dir / "t.md").write_text("from project task files\n", encoding="utf-8")
        nested_cwd = project / "src" / "pkg"
        nested_cwd.mkdir(parents=True)
        monkeypatch.chdir(nested_cwd)

        out = resolve_task(
            explicit_task=None,
            explicit_task_file="t.md",
            resumed=None,
        )

        assert out == "from project task files"

    def test_bare_md_task_file_resolves_from_explicit_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "project"
        task_dir = project / ".orcho" / ".task-files"
        task_dir.mkdir(parents=True)
        (task_dir / "t.md").write_text("from explicit project\n", encoding="utf-8")
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.chdir(other)

        out = resolve_task(
            explicit_task=None,
            explicit_task_file="t.md",
            explicit_project=str(project),
            resumed=None,
        )

        assert out == "from explicit project"

    def test_missing_bare_md_task_file_reports_task_files_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "project"
        nested_cwd = project / "src" / "pkg"
        nested_cwd.mkdir(parents=True)
        monkeypatch.chdir(nested_cwd)

        with pytest.raises(ResumeContextError) as exc:
            resolve_task(
                explicit_task=None,
                explicit_task_file="missing.md",
                explicit_project=str(project),
                resumed=None,
            )

        message = str(exc.value)
        assert "--task-file short name not found: missing.md" in message
        assert "treated 'missing.md' as a short task-file name" in message
        assert ".orcho/.task-files" in message
        assert str(project / ".orcho" / ".task-files") in message
        assert "--task-file ./missing.md" in message

    def test_missing_direct_task_file_reports_path_without_traceback(
        self, tmp_path: Path,
    ) -> None:
        missing = tmp_path / "tasks" / "missing.md"

        with pytest.raises(ResumeContextError) as exc:
            resolve_task(
                explicit_task=None,
                explicit_task_file=str(missing),
                resumed=None,
            )

        message = str(exc.value)
        assert f"--task-file not found: {missing}" in message
        assert ".orcho/.task-files" in message

    def test_explicit_task_wins_over_meta(self, tmp_path: Path) -> None:
        resumed = ResumedMeta(
            path=tmp_path / "meta.json", meta={"task": "from meta"},
        )
        out = resolve_task(
            explicit_task="from cli",
            explicit_task_file=None,
            resumed=resumed,
        )
        assert out == "from cli"

    def test_meta_fallback(self, tmp_path: Path) -> None:
        resumed = ResumedMeta(
            path=tmp_path / "meta.json", meta={"task": "from meta"},
        )
        out = resolve_task(
            explicit_task=None,
            explicit_task_file=None,
            resumed=resumed,
        )
        assert out == "from meta"

    def test_missing_everywhere_errors(self) -> None:
        with pytest.raises(ResumeContextError, match="task:"):
            resolve_task(
                explicit_task=None,
                explicit_task_file=None,
                resumed=None,
            )

    def test_missing_with_blank_meta_errors(self, tmp_path: Path) -> None:
        resumed = ResumedMeta(
            path=tmp_path / "meta.json", meta={"task": "   "},
        )
        with pytest.raises(ResumeContextError, match="task:"):
            resolve_task(
                explicit_task=None,
                explicit_task_file=None,
                resumed=resumed,
            )


class TestResolveProject:
    def test_explicit_wins(self, tmp_path: Path) -> None:
        resumed = ResumedMeta(
            path=tmp_path / "meta.json", meta={"project": "/from-meta"},
        )
        assert resolve_project(
            explicit_project="/from-cli", resumed=resumed,
        ) == "/from-cli"

    def test_meta_fallback(self, tmp_path: Path) -> None:
        resumed = ResumedMeta(
            path=tmp_path / "meta.json", meta={"project": "/from-meta"},
        )
        assert resolve_project(
            explicit_project=None, resumed=resumed,
        ) == "/from-meta"

    def test_missing_errors(self) -> None:
        with pytest.raises(ResumeContextError, match="project:"):
            resolve_project(explicit_project=None, resumed=None)


class TestResolveResumeProfile:
    """``resolve_resume_profile`` keeps three orthogonal cases distinct:
    explicit-wins, inherit-from-meta, and fresh-run fallback. The
    fallback default is intentionally ``feature`` (not ``task``) so
    legacy meta.json without a ``profile`` field still includes
    ``artifact:validate_plan`` in review/final prompt envelopes —
    see the helper's docstring for the trade-off rationale."""

    def test_explicit_wins_over_meta(self, tmp_path: Path) -> None:
        resumed = ResumedMeta(
            path=tmp_path / "meta.json", meta={"profile": "feature"},
        )
        assert resolve_resume_profile(
            explicit_profile="small_task", resumed=resumed,
        ) == "small_task"

    def test_explicit_feature_still_explicit(self, tmp_path: Path) -> None:
        """Explicit ``--profile feature`` is a deliberate switch when
        meta says something else; helper must not collapse the explicit
        value into the inherit path."""
        resumed = ResumedMeta(
            path=tmp_path / "meta.json", meta={"profile": "small_task"},
        )
        assert resolve_resume_profile(
            explicit_profile="feature", resumed=resumed,
        ) == "feature"

    def test_inherit_from_meta_when_no_explicit(self, tmp_path: Path) -> None:
        resumed = ResumedMeta(
            path=tmp_path / "meta.json", meta={"profile": "feature"},
        )
        assert resolve_resume_profile(
            explicit_profile=None, resumed=resumed,
        ) == "feature"

    def test_inherit_non_default_profile_from_meta(self, tmp_path: Path) -> None:
        resumed = ResumedMeta(
            path=tmp_path / "meta.json", meta={"profile": "small_task"},
        )
        assert resolve_resume_profile(
            explicit_profile=None, resumed=resumed,
        ) == "small_task"

    def test_fresh_default_when_no_resume(self) -> None:
        assert resolve_resume_profile(
            explicit_profile=None, resumed=None,
        ) == "feature"

    def test_legacy_meta_without_profile_field_falls_back(
        self, tmp_path: Path,
    ) -> None:
        """Old runs (pre-profile-capture meta) gracefully fall back
        to the fresh-run default rather than crashing."""
        resumed = ResumedMeta(
            path=tmp_path / "meta.json",
            meta={"task": "t", "project": "/p"},
        )
        assert resolve_resume_profile(
            explicit_profile=None, resumed=resumed,
        ) == "feature"

    def test_meta_profile_empty_string_falls_back(self, tmp_path: Path) -> None:
        resumed = ResumedMeta(
            path=tmp_path / "meta.json", meta={"profile": "   "},
        )
        assert resolve_resume_profile(
            explicit_profile=None, resumed=resumed,
        ) == "feature"

    def test_explicit_empty_string_treated_as_missing(self, tmp_path: Path) -> None:
        """Defensive: an explicit empty / whitespace-only ``--profile``
        is treated as "not supplied" so the inherit/fallback path
        still kicks in. Avoids accidental dispatch to an empty
        profile name from a stripped CLI arg."""
        resumed = ResumedMeta(
            path=tmp_path / "meta.json", meta={"profile": "small_task"},
        )
        assert resolve_resume_profile(
            explicit_profile="   ", resumed=resumed,
        ) == "small_task"

    def test_custom_fresh_default(self) -> None:
        """``fresh_default`` is a parameter, not hardcoded — callers
        with profile-aware fallback policies can override."""
        assert resolve_resume_profile(
            explicit_profile=None,
            resumed=None,
            fresh_default="complex_feature",
        ) == "complex_feature"

    def test_fresh_default_sources_canonical_constant(self) -> None:
        """The implicit fresh fallback is sourced from the canonical
        ``DEFAULT_PROFILE_NAME`` — no duplicated ``'feature'`` literal in
        ``resume_context``. Asserting against the imported constant means
        this stays correct even if the canonical default is renamed."""
        from pipeline.project.constants import DEFAULT_PROFILE_NAME

        assert resolve_resume_profile(
            explicit_profile=None, resumed=None,
        ) == DEFAULT_PROFILE_NAME

    def test_ambient_env_does_not_override_meta_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: durable ``meta['profile']`` wins over an ambient
        ``ORCHO_PIPELINE`` at the resolution layer. This helper never read
        the env (the silent hijack lived downstream in profile setup), but
        the guard pins the invariant so no future edit reintroduces it."""
        monkeypatch.setenv("ORCHO_PIPELINE", "task")
        resumed = ResumedMeta(
            path=tmp_path / "meta.json", meta={"profile": "feature"},
        )
        assert resolve_resume_profile(
            explicit_profile=None, resumed=resumed,
        ) == "feature"

    def test_ambient_env_does_not_override_fresh_default(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fresh fallback (no resume, no explicit) still resolves to the
        canonical default even with ``ORCHO_PIPELINE`` set — env A/B is a
        downstream fresh-start concern, not this resolver's job."""
        from pipeline.project.constants import DEFAULT_PROFILE_NAME

        monkeypatch.setenv("ORCHO_PIPELINE", "task")
        assert resolve_resume_profile(
            explicit_profile=None, resumed=None,
        ) == DEFAULT_PROFILE_NAME


class TestResolveProjectsArgv:
    def test_explicit_wins(self, tmp_path: Path) -> None:
        resumed = ResumedMeta(
            path=tmp_path / "meta.json",
            meta={"projects": {"api": "/a", "web": "/w"}},
        )
        out = resolve_projects_argv(
            explicit_projects=["api:/cli-a"], resumed=resumed,
        )
        assert out == ["api:/cli-a"]

    def test_meta_fallback_reconstructs_argv(self, tmp_path: Path) -> None:
        resumed = ResumedMeta(
            path=tmp_path / "meta.json",
            meta={"projects": {"api": "/a", "web": "/w"}},
        )
        out = resolve_projects_argv(
            explicit_projects=None, resumed=resumed,
        )
        assert sorted(out) == sorted(["api:/a", "web:/w"])

    def test_missing_errors(self) -> None:
        with pytest.raises(ResumeContextError, match="projects:"):
            resolve_projects_argv(explicit_projects=None, resumed=None)

    def test_malformed_meta_rejected(self, tmp_path: Path) -> None:
        resumed = ResumedMeta(
            path=tmp_path / "meta.json",
            meta={"projects": {"api": 42}},  # type: ignore[dict-item]
        )
        with pytest.raises(ResumeContextError, match="malformed"):
            resolve_projects_argv(
                explicit_projects=None, resumed=resumed,
            )


# ── Resume mode classification ─────────────────────────────────────────────


class TestClassifyResumeMode:
    def test_no_resume_is_fresh(self) -> None:
        assert classify_resume_mode(
            resume=None, explicit_task=None, explicit_task_file=None,
        ) == ResumeMode.FRESH

    def test_no_resume_with_task_is_still_fresh(self) -> None:
        # ``--task X`` without ``--resume`` is just a normal fresh run;
        # follow-up semantics only kick in when ``--resume`` is set.
        assert classify_resume_mode(
            resume=None, explicit_task="X", explicit_task_file=None,
        ) == ResumeMode.FRESH

    def test_resume_no_task_is_checkpoint(self) -> None:
        assert classify_resume_mode(
            resume="20260514_120000",
            explicit_task=None,
            explicit_task_file=None,
        ) == ResumeMode.CHECKPOINT

    def test_resume_with_task_is_followup(self) -> None:
        assert classify_resume_mode(
            resume="20260514_120000",
            explicit_task="follow up",
            explicit_task_file=None,
        ) == ResumeMode.FOLLOWUP

    def test_resume_with_task_file_is_followup(self) -> None:
        assert classify_resume_mode(
            resume="20260514_120000",
            explicit_task=None,
            explicit_task_file="task.md",
        ) == ResumeMode.FOLLOWUP


class TestIsTerminalSuccess:
    @pytest.mark.parametrize("status", ["done", "success", "completed"])
    def test_terminal_success_statuses(self, status: str) -> None:
        assert is_terminal_success({"status": status})

    @pytest.mark.parametrize(
        "status",
        [
            "running",
            "failed",
            "halted",
            "interrupted",
            "awaiting_human_review",
            "awaiting_phase_handoff",
            "awaiting_commit_decision",
            "awaiting_gate_decision",
            "",
        ],
    )
    def test_non_terminal_statuses(self, status: str) -> None:
        assert not is_terminal_success({"status": status})

    def test_missing_status_is_not_terminal(self) -> None:
        assert not is_terminal_success({})


class TestIsAwaitingCommitDecision:
    def test_awaiting_status_is_true(self) -> None:
        assert is_awaiting_commit_decision(
            {"status": "awaiting_commit_decision"},
        )

    @pytest.mark.parametrize(
        "status", ["running", "halted", "done", "", "awaiting_human_review"],
    )
    def test_other_statuses_are_false(self, status: str) -> None:
        assert not is_awaiting_commit_decision({"status": status})

    def test_missing_status_is_false(self) -> None:
        assert not is_awaiting_commit_decision({})


class TestIsTerminalCommitDecisionHalt:
    def test_halted_with_commit_decision_reason_is_true(self) -> None:
        assert is_terminal_commit_decision_halt({
            "status": "halted",
            "halt_reason": "commit_decision_halt",
        })

    def test_halted_with_phase_handoff_reason_is_false(self) -> None:
        assert not is_terminal_commit_decision_halt({
            "status": "halted",
            "halt_reason": "phase_handoff_halt",
        })

    def test_halted_without_reason_is_false(self) -> None:
        assert not is_terminal_commit_decision_halt({"status": "halted"})

    def test_done_with_commit_decision_reason_is_false(self) -> None:
        assert not is_terminal_commit_decision_halt({
            "status": "done",
            "halt_reason": "commit_decision_halt",
        })


class TestIsTerminalCommitDecisionFix:
    def test_halted_with_commit_decision_fix_reason_is_true(self) -> None:
        assert is_terminal_commit_decision_fix({
            "status": "halted",
            "halt_reason": "commit_decision_fix",
        })

    def test_other_halted_reason_is_false(self) -> None:
        assert not is_terminal_commit_decision_fix({
            "status": "halted",
            "halt_reason": "commit_decision_halt",
        })


class TestIsTerminalPhaseHandoffHalt:
    def test_phase_handoff_halt_is_terminal(self) -> None:
        assert is_terminal_phase_handoff_halt({
            "status": "halted",
            "halt_reason": "phase_handoff_halt",
        })

    def test_other_halted_reason_is_not_terminal_handoff_halt(self) -> None:
        assert not is_terminal_phase_handoff_halt({
            "status": "halted",
            "halt_reason": "parse_error",
        })


class TestIsTerminalFinalAcceptanceRejected:
    def test_rejected_reason_is_terminal(self) -> None:
        assert is_terminal_final_acceptance_rejected({
            "status": "halted",
            "halt_reason": "final_acceptance_rejected",
        })

    def test_no_diff_reason_is_terminal(self) -> None:
        assert is_terminal_final_acceptance_rejected({
            "status": "halted",
            "halt_reason": "final_acceptance_no_diff",
        })

    def test_other_halted_reason_is_false(self) -> None:
        assert not is_terminal_final_acceptance_rejected({
            "status": "halted",
            "halt_reason": "commit_decision_halt",
        })

    def test_halted_without_reason_is_false(self) -> None:
        assert not is_terminal_final_acceptance_rejected({"status": "halted"})

    def test_done_with_rejected_reason_is_false(self) -> None:
        assert not is_terminal_final_acceptance_rejected({
            "status": "done",
            "halt_reason": "final_acceptance_rejected",
        })


class TestIsTerminalResumeParentRejected:
    """The rejected final-acceptance dead-ends must read as terminal so
    checkpoint-resume does not auto-select them. Shape mirrors the
    20260626_165338_90fb22 run (halted + final_acceptance_rejected, a
    project, no projects/phase_handoff/commit gate/parent)."""

    def test_final_acceptance_rejected_is_terminal_parent(self) -> None:
        assert is_terminal_resume_parent({
            "task": "T",
            "project": "/p",
            "status": "halted",
            "halt_reason": "final_acceptance_rejected",
        })

    def test_final_acceptance_no_diff_is_terminal_parent(self) -> None:
        assert is_terminal_resume_parent({
            "task": "T",
            "project": "/p",
            "status": "halted",
            "halt_reason": "final_acceptance_no_diff",
        })

    def test_non_terminal_halted_is_not_terminal_parent(self) -> None:
        assert not is_terminal_resume_parent({
            "task": "T",
            "project": "/p",
            "status": "halted",
            "halt_reason": "parse_error",
        })

    def test_interrupted_is_not_terminal_parent(self) -> None:
        assert not is_terminal_resume_parent({
            "task": "T", "project": "/p", "status": "interrupted",
        })


class TestGetResumeIntentOptions:
    def test_missing_parent_meta_blocks_both(self) -> None:
        opts = get_resume_intent_options(parent_meta=None, has_new_task=False)
        assert not opts.can_checkpoint
        assert not opts.can_followup
        assert opts.default_mode is None
        assert opts.reason == "missing-parent-meta"

    def test_explicit_task_forces_followup_only(self) -> None:
        opts = get_resume_intent_options(
            parent_meta={"task": "T", "project": "/p", "status": "running"},
            has_new_task=True,
        )
        assert not opts.can_checkpoint
        assert opts.can_followup
        assert opts.default_mode == ResumeMode.FOLLOWUP
        assert opts.reason == "explicit-task"

    def test_terminal_success_offers_followup_only(self) -> None:
        opts = get_resume_intent_options(
            parent_meta={"task": "T", "project": "/p", "status": "done"},
            has_new_task=False,
        )
        assert not opts.can_checkpoint
        assert opts.can_followup
        assert opts.default_mode == ResumeMode.FOLLOWUP
        assert opts.reason == "terminal-success"

    def test_phase_handoff_halt_offers_followup_only(self) -> None:
        opts = get_resume_intent_options(
            parent_meta={
                "task": "T",
                "project": "/p",
                "status": "halted",
                "halt_reason": "phase_handoff_halt",
            },
            has_new_task=False,
        )
        assert not opts.can_checkpoint
        assert opts.can_followup
        assert opts.default_mode == ResumeMode.FOLLOWUP
        assert opts.reason == "terminal-halt"

    def test_incomplete_offers_both_default_checkpoint(self) -> None:
        opts = get_resume_intent_options(
            parent_meta={
                "task": "T", "project": "/p", "status": "interrupted",
            },
            has_new_task=False,
        )
        assert opts.can_checkpoint
        assert opts.can_followup
        assert opts.default_mode == ResumeMode.CHECKPOINT
        assert opts.reason == "incomplete-parent"

    def test_awaiting_commit_decision_offers_both_default_checkpoint(self) -> None:
        opts = get_resume_intent_options(
            parent_meta={
                "task": "T",
                "project": "/p",
                "status": "awaiting_commit_decision",
            },
            has_new_task=False,
        )
        assert opts.can_checkpoint
        assert opts.can_followup
        assert opts.default_mode == ResumeMode.CHECKPOINT
        assert opts.reason == "awaiting-commit-decision"

    def test_commit_decision_halt_offers_followup_only(self) -> None:
        opts = get_resume_intent_options(
            parent_meta={
                "task": "T",
                "project": "/p",
                "status": "halted",
                "halt_reason": "commit_decision_halt",
            },
            has_new_task=False,
        )
        assert not opts.can_checkpoint
        assert opts.can_followup
        assert opts.default_mode == ResumeMode.FOLLOWUP
        assert opts.reason == "terminal-halt"
        # ``halt`` is "gave up", not a correction dead-end — no follow-up mandate.
        assert opts.requires_followup_task is False

    def test_commit_decision_fix_requires_followup_task(self) -> None:
        # T1: a fix-marked correction terminal makes a bare task-less resume
        # meaningless; the actionable next step is a from_run_plan follow-up.
        opts = get_resume_intent_options(
            parent_meta={
                "task": "T",
                "project": "/p",
                "status": "halted",
                "halt_reason": "commit_decision_fix",
            },
            has_new_task=False,
        )
        assert not opts.can_checkpoint
        assert opts.can_followup
        assert opts.default_mode == ResumeMode.FOLLOWUP
        assert opts.reason == "correction-followup-required"
        assert opts.requires_followup_task is True

    def test_final_acceptance_rejected_requires_followup_task(self) -> None:
        opts = get_resume_intent_options(
            parent_meta={
                "task": "T",
                "project": "/p",
                "status": "halted",
                "halt_reason": "final_acceptance_rejected",
            },
            has_new_task=False,
        )
        assert not opts.can_checkpoint
        assert opts.can_followup
        assert opts.default_mode == ResumeMode.FOLLOWUP
        assert opts.reason == "correction-followup-required"
        assert opts.requires_followup_task is True

    def test_final_acceptance_no_diff_requires_followup_task(self) -> None:
        opts = get_resume_intent_options(
            parent_meta={
                "task": "T",
                "project": "/p",
                "status": "halted",
                "halt_reason": "final_acceptance_no_diff",
            },
            has_new_task=False,
        )
        assert not opts.can_checkpoint
        assert opts.can_followup
        assert opts.default_mode == ResumeMode.FOLLOWUP
        assert opts.reason == "correction-followup-required"
        assert opts.requires_followup_task is True

    def test_awaiting_human_review_is_incomplete(self) -> None:
        # Pause states must default to checkpoint so a paused-review
        # run keeps its existing context on resume.
        opts = get_resume_intent_options(
            parent_meta={
                "task": "T",
                "project": "/p",
                "status": "awaiting_human_review",
            },
            has_new_task=False,
        )
        assert opts.can_checkpoint
        assert opts.default_mode == ResumeMode.CHECKPOINT


class TestExtractFollowupSessionSeeds:
    def test_happy_path_reads_step0_session_shape(self) -> None:
        parent_meta = {
            "phases": {
                "plan": [{"session_id": "plan-1"}, {"session_id": "plan-2"}],
                "validate_plan": [{"session_id": "validate-1"}],
                "implement": {"meta": {"session_id": "implement-1"}},
                "rounds": [
                    {
                        "review_session_id": "review-1",
                        "repair_session_id": "repair-1",
                    },
                    {
                        "review_session_id": "review-2",
                        "repair_session_id": "repair-2",
                    },
                ],
                "final_acceptance": {"meta": {"session_id": "final-1"}},
            },
        }
        assert extract_followup_session_seeds(parent_meta) == {
            "plan": "plan-2",
            "validate_plan": "validate-1",
            "implement": "implement-1",
            "review_changes": "review-2",
            "repair_changes": "repair-2",
            "final_acceptance": "final-1",
        }

    def test_missing_phases_returns_empty(self) -> None:
        assert extract_followup_session_seeds({}) == {}

    def test_missing_entries_keep_other_roles(self) -> None:
        parent_meta = {"phases": {"implement": {"meta": {"session_id": "i"}}}}
        assert extract_followup_session_seeds(parent_meta) == {"implement": "i"}

    def test_malformed_values_are_skipped_silently(self) -> None:
        parent_meta = {
            "phases": {
                "plan": [{"session_id": 123}],
                "implement": {"meta": {"session_id": ""}},
                "rounds": [{"review_session_id": None, "repair_session_id": []}],
            },
        }
        assert extract_followup_session_seeds(parent_meta) == {}

    def test_presplit_round_session_id_seeds_repair_only(self) -> None:
        parent_meta = {"phases": {"rounds": [{"session_id": "legacy-repair"}]}}
        assert extract_followup_session_seeds(parent_meta) == {
            "repair_changes": "legacy-repair",
        }


# ── resolve_latest_run ─────────────────────────────────────────────────────


def _make_run(runs_dir: Path, run_id: str, meta: dict) -> None:
    d = runs_dir / run_id
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


class TestResolveLatestRun:
    def test_picks_newest_single_project(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        _make_run(runs_dir, "20260101_000000",
                  {"task": "a", "project": "/p", "status": "done"})
        _make_run(runs_dir, "20260514_120000",
                  {"task": "b", "project": "/p", "status": "done"})
        _make_run(runs_dir, "20260301_080000",
                  {"task": "c", "project": "/p", "status": "done"})
        out = resolve_latest_run(runs_dir=runs_dir, kind="run")
        assert out == "20260514_120000"

    def test_run_kind_ignores_cross(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        # Newer cross run, older single-project run.
        _make_run(runs_dir, "20260514_120000",
                  {"task": "a", "projects": {"u": "/u"}, "status": "done"})
        _make_run(runs_dir, "20260101_000000",
                  {"task": "b", "project": "/p", "status": "done"})
        out = resolve_latest_run(runs_dir=runs_dir, kind="run")
        # Cross run filtered out; single-project run picked even though
        # it is older.
        assert out == "20260101_000000"

    def test_cross_kind_ignores_single_project(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        _make_run(runs_dir, "20260514_120000",
                  {"task": "a", "project": "/p", "status": "done"})
        _make_run(runs_dir, "20260101_000000",
                  {"task": "b", "projects": {"u": "/u"}, "status": "done"})
        out = resolve_latest_run(runs_dir=runs_dir, kind="cross")
        assert out == "20260101_000000"

    def test_prefer_incomplete_skips_terminal_success(
        self, tmp_path: Path,
    ) -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        # Newer is done; older is interrupted. With prefer_incomplete=True,
        # the resolver should pick the older incomplete one.
        _make_run(runs_dir, "20260514_120000",
                  {"task": "a", "project": "/p", "status": "done"})
        _make_run(runs_dir, "20260101_000000",
                  {"task": "b", "project": "/p", "status": "interrupted"})
        out = resolve_latest_run(
            runs_dir=runs_dir, kind="run", prefer_incomplete=True,
        )
        assert out == "20260101_000000"

    def test_prefer_incomplete_falls_back_to_terminal_when_no_incomplete(
        self, tmp_path: Path,
    ) -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        _make_run(runs_dir, "20260514_120000",
                  {"task": "a", "project": "/p", "status": "done"})
        out = resolve_latest_run(
            runs_dir=runs_dir, kind="run", prefer_incomplete=True,
        )
        # No incomplete candidate exists; include_terminal_success
        # defaults True, so the done run is still returned.
        assert out == "20260514_120000"

    def test_exclude_terminal_success_raises_when_only_terminal(
        self, tmp_path: Path,
    ) -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        _make_run(runs_dir, "20260514_120000",
                  {"task": "a", "project": "/p", "status": "done"})
        with pytest.raises(LatestRunNotFound):
            resolve_latest_run(
                runs_dir=runs_dir, kind="run",
                include_terminal_success=False,
            )

    def test_empty_runs_dir_raises(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        with pytest.raises(LatestRunNotFound):
            resolve_latest_run(runs_dir=runs_dir, kind="run")

    def test_missing_runs_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(LatestRunNotFound):
            resolve_latest_run(
                runs_dir=tmp_path / "does-not-exist", kind="run",
            )

    def test_malformed_meta_is_skipped(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        # Corrupt meta — load_resume_meta raises and the resolver should
        # skip the run rather than abort the whole lookup.
        bad = runs_dir / "20260514_120000"
        bad.mkdir()
        (bad / "meta.json").write_text("not json", encoding="utf-8")
        _make_run(runs_dir, "20260101_000000",
                  {"task": "a", "project": "/p", "status": "done"})
        out = resolve_latest_run(runs_dir=runs_dir, kind="run")
        assert out == "20260101_000000"

    def test_legacy_meta_without_project_or_projects_skipped(
        self, tmp_path: Path,
    ) -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        _make_run(runs_dir, "20260514_120000",
                  {"task": "a", "status": "done"})  # no project/projects
        _make_run(runs_dir, "20260101_000000",
                  {"task": "b", "project": "/p", "status": "done"})
        out = resolve_latest_run(runs_dir=runs_dir, kind="run")
        assert out == "20260101_000000"

    def test_require_existing_project_skips_stale_single_project_meta(
        self, tmp_path: Path,
    ) -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        live_project = tmp_path / "live-project"
        live_project.mkdir()
        _make_run(
            runs_dir,
            "20260610_003038",
            {
                "task": "stale pytest run",
                "project": str(tmp_path / "deleted-private-tmp-project"),
                "status": "interrupted",
            },
        )
        _make_run(
            runs_dir,
            "20260609_125615",
            {
                "task": "real run",
                "project": str(live_project),
                "status": "interrupted",
            },
        )

        out = resolve_latest_run(
            runs_dir=runs_dir,
            kind="run",
            prefer_incomplete=True,
            require_existing_project=True,
        )

        assert out == "20260609_125615"

    def test_require_existing_project_skips_stale_cross_project_meta(
        self, tmp_path: Path,
    ) -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        live_project = tmp_path / "live-project"
        live_project.mkdir()
        _make_run(
            runs_dir,
            "20260610_003038",
            {
                "task": "stale cross",
                "projects": {
                    "core": str(live_project),
                    "mcp": str(tmp_path / "missing-mcp"),
                },
                "status": "interrupted",
            },
        )
        _make_run(
            runs_dir,
            "20260609_125615",
            {
                "task": "real cross",
                "projects": {"core": str(live_project)},
                "status": "interrupted",
            },
        )

        out = resolve_latest_run(
            runs_dir=runs_dir,
            kind="cross",
            prefer_incomplete=True,
            require_existing_project=True,
        )

        assert out == "20260609_125615"


# ── extract_cross_followup_session_seeds ───────────────────────────────────


def _write_child_meta(
    parent_dir: Path,
    alias: str,
    *,
    plan_sid: str | None = None,
    implement_sid: str | None = None,
    final_sid: str | None = None,
    review_sid: str | None = None,
    repair_sid: str | None = None,
    extra_phases: dict | None = None,
) -> None:
    """Create ``<parent_dir>/<alias>/meta.json`` with the Step-0 shape.

    Each kwarg slots into the right corner of the persisted phase log:
    plan goes into ``phases.plan[-1].session_id``, implement into
    ``phases.implement.meta.session_id``, etc. Mirrors what real Step-0
    adapters write so the extractor sees production-shaped data.
    """
    alias_dir = parent_dir / alias
    alias_dir.mkdir(parents=True, exist_ok=True)
    phases: dict = {}
    if plan_sid:
        phases["plan"] = [{"attempt": 1, "output": "p", "session_id": plan_sid}]
    if implement_sid:
        phases["implement"] = {"output": "i", "meta": {"session_id": implement_sid}}
    if final_sid:
        phases["final_acceptance"] = {"verdict": "APPROVED", "session_id": final_sid}
    if review_sid or repair_sid:
        round_entry: dict = {"round": 1}
        if review_sid:
            round_entry["review_session_id"] = review_sid
        if repair_sid:
            round_entry["repair_session_id"] = repair_sid
        phases["rounds"] = [round_entry]
    if extra_phases:
        phases.update(extra_phases)
    meta = {
        "task": "child",
        "project": f"/projects/{alias}",
        "status": "done",
        "phases": phases,
    }
    (alias_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


class TestExtractCrossFollowupSessionSeeds:
    def test_per_alias_seeds_for_each_child(self, tmp_path: Path) -> None:
        parent = tmp_path / "cross"
        _write_child_meta(parent, "api",
                          plan_sid="api-plan", implement_sid="api-impl")
        _write_child_meta(parent, "web",
                          plan_sid="web-plan", implement_sid="web-impl")
        seeds = extract_cross_followup_session_seeds(parent, ["api", "web"])
        assert seeds == {
            "api": {"plan": "api-plan", "implement": "api-impl"},
            "web": {"plan": "web-plan", "implement": "web-impl"},
        }

    def test_missing_alias_child_dir_skipped(self, tmp_path: Path) -> None:
        # Parent cross run never spawned a "web" sub-pipeline (or the
        # directory was deleted). The follow-up still asks for "web";
        # we silently omit it from the seed map. Caller falls through
        # to a fresh agent context for that alias.
        parent = tmp_path / "cross"
        _write_child_meta(parent, "api", plan_sid="api-plan")
        seeds = extract_cross_followup_session_seeds(parent, ["api", "web"])
        assert seeds == {"api": {"plan": "api-plan"}}

    def test_malformed_alias_meta_skipped(self, tmp_path: Path) -> None:
        parent = tmp_path / "cross"
        _write_child_meta(parent, "api", plan_sid="api-plan")
        (parent / "web").mkdir(parents=True)
        (parent / "web" / "meta.json").write_text("not json", encoding="utf-8")
        seeds = extract_cross_followup_session_seeds(parent, ["api", "web"])
        assert seeds == {"api": {"plan": "api-plan"}}

    def test_alias_with_no_resumable_phases_dropped(self, tmp_path: Path) -> None:
        # Child meta exists but no phases captured session ids. Empty
        # per-alias seed map gets dropped (the caller treats absence as
        # "fresh agent for that alias" identically).
        parent = tmp_path / "cross"
        (parent / "api").mkdir(parents=True)
        (parent / "api" / "meta.json").write_text(
            json.dumps({"task": "x", "project": "/p", "status": "done",
                        "phases": {}}),
            encoding="utf-8",
        )
        _write_child_meta(parent, "web", plan_sid="web-plan")
        seeds = extract_cross_followup_session_seeds(parent, ["api", "web"])
        assert seeds == {"web": {"plan": "web-plan"}}

    def test_aliases_not_in_parent_ignored(self, tmp_path: Path) -> None:
        # Follow-up only asks for "api" even though the parent had
        # "api" + "web". We only return what the caller asked for.
        parent = tmp_path / "cross"
        _write_child_meta(parent, "api",
                          plan_sid="api-plan", implement_sid="api-impl")
        _write_child_meta(parent, "web", plan_sid="web-plan")
        seeds = extract_cross_followup_session_seeds(parent, ["api"])
        assert seeds == {"api": {"plan": "api-plan", "implement": "api-impl"}}
        assert "web" not in seeds

    def test_round_split_per_alias(self, tmp_path: Path) -> None:
        parent = tmp_path / "cross"
        _write_child_meta(parent, "api",
                          review_sid="api-review", repair_sid="api-repair")
        seeds = extract_cross_followup_session_seeds(parent, ["api"])
        assert seeds == {
            "api": {
                "review_changes": "api-review",
                "repair_changes": "api-repair",
            },
        }

    def test_round_legacy_alias_fallback(self, tmp_path: Path) -> None:
        # Pre-split parent rounds wrote a single ``session_id`` on the
        # round entry (repair side semantics). The cross extractor
        # inherits the single-project fallback through reuse.
        parent = tmp_path / "cross"
        (parent / "api").mkdir(parents=True)
        (parent / "api" / "meta.json").write_text(
            json.dumps({
                "task": "x", "project": "/p", "status": "done",
                "phases": {
                    "rounds": [{"round": 1, "session_id": "legacy-sid"}],
                },
            }),
            encoding="utf-8",
        )
        seeds = extract_cross_followup_session_seeds(parent, ["api"])
        assert seeds == {"api": {"repair_changes": "legacy-sid"}}

    def test_missing_parent_dir_returns_empty(self, tmp_path: Path) -> None:
        seeds = extract_cross_followup_session_seeds(
            tmp_path / "does-not-exist", ["api", "web"],
        )
        assert seeds == {}

    def test_empty_alias_list_returns_empty(self, tmp_path: Path) -> None:
        parent = tmp_path / "cross"
        _write_child_meta(parent, "api", plan_sid="api-plan")
        assert extract_cross_followup_session_seeds(parent, []) == {}

    def test_blank_alias_is_ignored(self, tmp_path: Path) -> None:
        # Defensive: a caller that hands us "" or whitespace shouldn't
        # crash or accidentally look up <parent>/ as the alias dir.
        parent = tmp_path / "cross"
        _write_child_meta(parent, "api", plan_sid="api-plan")
        seeds = extract_cross_followup_session_seeds(
            parent, ["api", "", "   "],
        )
        assert seeds == {"api": {"plan": "api-plan"}}


# ── build_followup_resume_fields ───────────────────────────────────────────


class TestBuildFollowupResumeFields:
    def test_followup_maps_all_slots(self, tmp_path: Path) -> None:
        resumed = ResumedMeta(
            path=tmp_path / "20260514_120000" / "meta.json",
            meta={
                "status": "interrupted",
                "task": "parent task",
                "phases": {
                    "plan": [{"session_id": "plan-1"}],
                    "implement": {"meta": {"session_id": "impl-1"}},
                },
            },
        )
        out = build_followup_resume_fields(
            resume_mode=ResumeMode.FOLLOWUP,
            resume_run_id="20260514_120000",
            resumed=resumed,
        )
        assert out == FollowupResumeFields(
            parent_run_id="20260514_120000",
            parent_run_dir=str((tmp_path / "20260514_120000").resolve()),
            parent_status="interrupted",
            base_task="parent task",
            session_seeds={"plan": "plan-1", "implement": "impl-1"},
        )

    def test_followup_empty_seeds_when_no_phases(self, tmp_path: Path) -> None:
        resumed = ResumedMeta(
            path=tmp_path / "r" / "meta.json",
            meta={"status": "running", "task": "t"},
        )
        out = build_followup_resume_fields(
            resume_mode=ResumeMode.FOLLOWUP,
            resume_run_id="r",
            resumed=resumed,
        )
        assert out.session_seeds == {}

    def test_followup_non_string_status_and_task_are_none(
        self, tmp_path: Path,
    ) -> None:
        resumed = ResumedMeta(
            path=tmp_path / "r" / "meta.json",
            meta={"status": 7, "task": ["nope"]},  # type: ignore[dict-item]
        )
        out = build_followup_resume_fields(
            resume_mode=ResumeMode.FOLLOWUP,
            resume_run_id="r",
            resumed=resumed,
        )
        assert out.parent_status is None
        assert out.base_task is None
        # The id / dir / seeds slots still populate for a FOLLOWUP.
        assert out.parent_run_id == "r"
        assert out.parent_run_dir == str((tmp_path / "r").resolve())
        assert out.session_seeds == {}

    def test_checkpoint_yields_all_none(self, tmp_path: Path) -> None:
        resumed = ResumedMeta(
            path=tmp_path / "r" / "meta.json",
            meta={"status": "interrupted", "task": "t"},
        )
        out = build_followup_resume_fields(
            resume_mode=ResumeMode.CHECKPOINT,
            resume_run_id="r",
            resumed=resumed,
        )
        assert out == FollowupResumeFields(
            parent_run_id=None,
            parent_run_dir=None,
            parent_status=None,
            base_task=None,
            session_seeds=None,
        )

    def test_fresh_yields_all_none(self) -> None:
        out = build_followup_resume_fields(
            resume_mode=ResumeMode.FRESH,
            resume_run_id=None,
            resumed=None,
        )
        assert out == FollowupResumeFields(
            parent_run_id=None,
            parent_run_dir=None,
            parent_status=None,
            base_task=None,
            session_seeds=None,
        )

    def test_followup_without_resumed_yields_all_none(self) -> None:
        out = build_followup_resume_fields(
            resume_mode=ResumeMode.FOLLOWUP,
            resume_run_id="r",
            resumed=None,
        )
        assert out == FollowupResumeFields(
            parent_run_id=None,
            parent_run_dir=None,
            parent_status=None,
            base_task=None,
            session_seeds=None,
        )


# ── meta_run_kind ──────────────────────────────────────────────────────────


class TestMetaRunKind:
    def test_non_empty_projects_is_cross(self) -> None:
        assert meta_run_kind({"projects": {"api": "/a"}}) == "cross"

    def test_project_without_projects_is_run(self) -> None:
        assert meta_run_kind({"project": "/p"}) == "run"

    def test_project_with_projects_prefers_cross(self) -> None:
        assert meta_run_kind(
            {"project": "/p", "projects": {"api": "/a"}},
        ) == "cross"

    def test_empty_meta_is_none(self) -> None:
        assert meta_run_kind({}) is None

    def test_empty_projects_dict_with_no_project_is_none(self) -> None:
        assert meta_run_kind({"projects": {}}) is None

    def test_blank_project_string_is_none(self) -> None:
        assert meta_run_kind({"project": "   "}) is None
