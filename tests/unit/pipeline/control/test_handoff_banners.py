"""retry_feedback pre/post banners + feedback sanitization (T3)."""

from __future__ import annotations

import io

from core.io.ansi import strip_ansi
from pipeline.control.handoff_banners import (
    RetryOutcome,
    print_retry_feedback_banner,
    print_retry_outcome_banner,
    render_advice_summary,
    render_retry_feedback_banner,
    render_retry_outcome_banner,
    sanitize_feedback_preview,
)


class TestPreBanner:
    def test_repair_retry_fields(self) -> None:
        banner = render_retry_feedback_banner(
            run_id="20260101_000000",
            handoff_id="review_changes:repair_round:2",
            rejected_phase="review_changes",
            retry_kind="repair",
            retry_round=3,
            loop_max_rounds=2,
            feedback="Fix the null deref in the retry path.",
            resume_provider_session=True,
            worktree_subject="/wt/wt_20260101/checkout",
        )
        assert "20260101_000000" in banner
        assert "review_changes:repair_round:2" in banner
        assert "review_changes" in banner
        assert "retry_feedback" in banner
        assert "repair_changes → review_changes retry" in banner
        # Uses the T1 round label (human retry, never an N/M with N>M).
        assert "human retry 1 after REJECTED verdict" in banner
        assert "2/1" not in banner
        assert "resume" in banner
        assert "Fix the null deref" in banner
        # The retained worktree subject is its own line, distinct from the
        # provider-session line.
        assert "provider session:" in banner
        assert "worktree        : retained retry subject /wt/wt_20260101/checkout" in (
            banner
        )

    def test_plan_retry_fields(self) -> None:
        banner = render_retry_feedback_banner(
            run_id="r1",
            handoff_id="validate_plan:plan_round:1",
            rejected_phase="validate_plan",
            retry_kind="plan",
            retry_round=2,
            loop_max_rounds=1,
            feedback="Tighten the acceptance criteria.",
            resume_provider_session=False,
        )
        assert "plan → validate_plan retry" in banner
        assert "validate_plan" in banner
        assert "fresh session" in banner
        assert "Tighten the acceptance criteria." in banner

    def _banner(self, *, resume: bool, worktree_subject, worktree_isolated=True):
        return render_retry_feedback_banner(
            run_id="r",
            handoff_id="review_changes:repair_round:2",
            rejected_phase="review_changes",
            retry_kind="repair",
            retry_round=2,
            loop_max_rounds=1,
            feedback="fix it",
            resume_provider_session=resume,
            worktree_subject=worktree_subject,
            worktree_isolated=worktree_isolated,
        )

    def test_worktree_line_present_in_both_provider_session_combos(self) -> None:
        path = "/wt/wt_abc/checkout"
        resume_banner = self._banner(resume=True, worktree_subject=path)
        fresh_banner = self._banner(resume=False, worktree_subject=path)

        wt_line = f"  worktree        : retained retry subject {path}"
        # Both lines present regardless of provider-session freshness.
        for banner in (resume_banner, fresh_banner):
            assert "  provider session: " in banner
            assert wt_line in banner

        # The provider-session line differs (resume vs fresh) but the worktree
        # subject line is byte-identical — a fresh fallback never moves it.
        assert "resume" in resume_banner
        assert "fresh session" in fresh_banner
        resume_wt = [ln for ln in resume_banner.splitlines() if "worktree" in ln]
        fresh_wt = [ln for ln in fresh_banner.splitlines() if "worktree" in ln]
        assert resume_wt == fresh_wt == [wt_line]

    def test_worktree_line_isolation_off_uses_in_place_label(self) -> None:
        banner = self._banner(
            resume=True, worktree_subject="/repo/src", worktree_isolated=False,
        )
        assert "  worktree        : in-place checkout /repo/src" in banner
        assert "retained retry subject" not in banner

    def test_worktree_line_not_recorded_when_subject_absent(self) -> None:
        # Default (no subject passed) keeps a stable, honest line.
        banner = render_retry_feedback_banner(
            run_id="r",
            handoff_id="h",
            rejected_phase="review_changes",
            retry_kind="repair",
            retry_round=2,
            loop_max_rounds=1,
            feedback="x",
            resume_provider_session=True,
        )
        assert "  worktree        : (not recorded)" in banner

    def test_print_writes_to_stream(self) -> None:
        out = io.StringIO()
        print_retry_feedback_banner(
            out=out,
            run_id="r",
            handoff_id="h",
            rejected_phase="validate_plan",
            retry_kind="plan",
            retry_round=2,
            loop_max_rounds=1,
            feedback="x",
            resume_provider_session=True,
        )
        assert "retry_feedback" in out.getvalue()


class TestPostBanner:
    def test_three_outcomes_are_distinct(self) -> None:
        bodies = {
            o: render_retry_outcome_banner(
                run_id="r", handoff_id="h",
                rejected_phase="review_changes", outcome=o,
            )
            for o in RetryOutcome
        }
        # All three render distinct text.
        assert len(set(bodies.values())) == 3
        assert "handoff closed" in bodies[RetryOutcome.APPROVED]
        assert "paused for a new operator decision" in (
            bodies[RetryOutcome.REJECTED_AGAIN]
        )
        assert "fresh provider session" in (
            bodies[RetryOutcome.PROVIDER_FALLBACK]
        )

    def test_approved_mentions_remaining_phases(self) -> None:
        banner = render_retry_outcome_banner(
            run_id="r", handoff_id="h",
            rejected_phase="validate_plan", outcome=RetryOutcome.APPROVED,
        )
        assert "remaining phases" in banner

    def test_print_outcome_writes_to_stream(self) -> None:
        out = io.StringIO()
        print_retry_outcome_banner(
            out=out, run_id="r", handoff_id="h",
            rejected_phase="review_changes",
            outcome=RetryOutcome.REJECTED_AGAIN,
        )
        assert "rejected_again" in out.getvalue()


class TestSanitizeFeedbackPreview:
    def test_collapses_multiline(self) -> None:
        assert sanitize_feedback_preview("line one\nline two") == (
            "line one line two"
        )

    def test_strips_control_chars(self) -> None:
        assert sanitize_feedback_preview("a\x00b\tc") == "a b c"

    def test_truncates_with_ellipsis(self) -> None:
        out = sanitize_feedback_preview("x" * 500, max_len=50)
        assert len(out) == 50
        assert out.endswith("…")

    def test_empty_is_none_placeholder(self) -> None:
        assert sanitize_feedback_preview("") == "(none)"
        assert sanitize_feedback_preview("   \n\t ") == "(none)"


class TestAdviceSummary:
    def test_colorized_summary_strips_to_plain_contract(self) -> None:
        kwargs = dict(
            recommended_action="retry_feedback",
            confidence="high",
            rationale="Reviewer found one bounded gap.",
            retry_feedback_preview="Patch the plan and retry.",
            risks=("scope creep",),
            expected_files=("pipeline/project/foo.py",),
        )

        plain = render_advice_summary(**kwargs, color=False)
        colored = render_advice_summary(**kwargs, color=True)

        assert "\x1b[" in colored
        assert strip_ansi(colored) == plain
