"""Hardened action input + non-interactive feedback-file path (T8).

Pins two robustness properties of the interactive phase-handoff prompt:

* pasted multi-line feedback landing where an *action* was expected
  yields a targeted "that looks like pasted feedback" message — never
  ``Unknown action '<...long pasted tail...>'`` — and the action key is
  never glued to a stale feedback buffer; abort after too many invalid
  attempts is preserved;
* ``--feedback-file`` / :func:`set_feedback_file_override` provides a safe
  non-interactive path for a long verdict on the feedback-required
  actions (``retry_feedback`` / ``continue_with_waiver``).
"""

from __future__ import annotations

import io

import pytest

from pipeline.control.handoff_prompt import (
    HANDOFF_PROMPT_ABORTED,
    FeedbackFileError,
    load_feedback_file,
    prompt_phase_handoff_action,
    set_feedback_file_override,
)
from pipeline.runtime.handoff import PhaseHandoffRequested
from pipeline.runtime.roles import PhaseHandoffType

_WAIVER_SET = ("continue", "retry_feedback", "halt", "continue_with_waiver")


def _signal(
    *,
    available_actions: tuple[str, ...] = ("continue", "retry_feedback", "halt"),
) -> PhaseHandoffRequested:
    return PhaseHandoffRequested(
        handoff_id="validate_plan:plan_round:2",
        phase="validate_plan",
        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
        trigger="rejected",
        verdict="REJECTED",
        approved=False,
        round_extras_key="plan_round",
        round=2,
        loop_max_rounds=2,
        available_actions=available_actions,
        artifacts={},
        last_output="Mock critique: missing edge case A coverage.",
    )


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def _scripted_stdin(*lines: str) -> _FakeTTY:
    return _FakeTTY("".join(line + "\n" for line in lines))


def _new_stdout() -> _FakeTTY:
    return _FakeTTY()


@pytest.fixture(autouse=True)
def _reset_feedback_override():
    set_feedback_file_override(None)
    yield
    set_feedback_file_override(None)


class TestActionInputHardening:
    def test_valid_numeric_action(self) -> None:
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin("1", ""),  # action, default note
            stdout=_new_stdout(),
        )
        assert result.action == "continue"

    def test_pasted_feedback_shows_targeted_message(self) -> None:
        out = _new_stdout()
        # First line is pasted prose (contains spaces) where an action
        # was expected; then the operator types a real action.
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin(
                "The plan is missing error handling for the retry path",
                "1",
                "",
            ),
            stdout=out,
        )
        body = out.getvalue()
        assert result.action == "continue"
        assert "looks like pasted feedback" in body
        # The whole pasted tail must NOT be echoed as an unknown action.
        assert "Unknown action 'the plan is missing" not in body.lower()

    def test_long_single_token_paste_detected(self) -> None:
        out = _new_stdout()
        long_token = "x" * 40  # no spaces, but well past any action token
        prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin(long_token, "1", ""),
            stdout=out,
        )
        body = out.getvalue()
        assert "looks like pasted feedback" in body
        assert f"Unknown action '{long_token}'" not in body

    def test_action_key_not_glued_to_stale_paste(self) -> None:
        # Paste prose at the action slot, then choose retry_feedback and
        # type fresh feedback. The recorded feedback must be the freshly
        # typed text, never the earlier pasted prose.
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin(
                "garbage pasted verdict about the plan",  # invalid action
                "2",                                       # retry_feedback
                "real fresh feedback",                     # feedback line 1
                "",                                         # end feedback
                "",                                         # default note
            ),
            stdout=_new_stdout(),
        )
        assert result.action == "retry_feedback"
        assert result.feedback == "real fresh feedback"
        assert "garbage" not in (result.feedback or "")

    def test_retry_feedback_reads_until_empty_line(self) -> None:
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin(
                "2",            # retry_feedback
                "line one",     # feedback
                "line two",     # feedback
                "",             # blank line ends feedback
                "",             # default note
            ),
            stdout=_new_stdout(),
        )
        assert result.action == "retry_feedback"
        assert result.feedback == "line one\nline two"

    def test_retry_feedback_paste_preserves_blank_paragraphs(self) -> None:
        stdin = _scripted_stdin(
            "2",  # retry_feedback
            "The Stage 7A task file is authoritative.",
            "",
            "Do not mark T0 incomplete because roadmap docs drifted.",
            "",
            "Proceed with T1-T4:",
            "- run architecture preflight",
            "- run MCP checkpoint with CORE_UNDER_TEST",
            "",
            "",
        )

        result = prompt_phase_handoff_action(
            _signal(),
            stdin=stdin,
            stdout=_new_stdout(),
        )

        assert result.action == "retry_feedback"
        assert result.feedback == (
            "The Stage 7A task file is authoritative.\n\n"
            "Do not mark T0 incomplete because roadmap docs drifted.\n\n"
            "Proceed with T1-T4:\n"
            "- run architecture preflight\n"
            "- run MCP checkpoint with CORE_UNDER_TEST"
        )
        assert result.note == "orcho-cli tty retry_feedback"
        assert stdin.read() == ""

    def test_abort_after_too_many_invalid_attempts(self) -> None:
        out = _new_stdout()
        # Three short, non-paste, invalid tokens exhaust the retry budget.
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin("zzz", "nope", "bad"),
            stdout=out,
        )
        assert result is HANDOFF_PROMPT_ABORTED
        assert "Too many invalid attempts" in out.getvalue()


class TestFeedbackFileLoading:
    def test_load_feedback_file_returns_stripped(self, tmp_path) -> None:
        fb = tmp_path / "fb.txt"
        fb.write_text("\n  long operator verdict  \n\n", encoding="utf-8")
        assert load_feedback_file(str(fb)) == "long operator verdict"

    def test_load_feedback_file_empty_raises(self, tmp_path) -> None:
        fb = tmp_path / "empty.txt"
        fb.write_text("   \n", encoding="utf-8")
        with pytest.raises(FeedbackFileError):
            load_feedback_file(str(fb))

    def test_load_feedback_file_missing_raises(self, tmp_path) -> None:
        with pytest.raises(FeedbackFileError):
            load_feedback_file(str(tmp_path / "does-not-exist.txt"))


class TestFeedbackFileOverrideInPrompt:
    def test_retry_feedback_sourced_from_file(self, tmp_path) -> None:
        fb = tmp_path / "verdict.txt"
        long_verdict = "Rework the plan:\n" + ("detail line\n" * 50)
        fb.write_text(long_verdict, encoding="utf-8")
        set_feedback_file_override(str(fb))

        out = _new_stdout()
        # No feedback lines on stdin — only the action and the note. The
        # feedback must come from the file.
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin("2", ""),
            stdout=out,
        )
        assert result.action == "retry_feedback"
        assert result.feedback == long_verdict.strip()
        assert "from file" in out.getvalue()

    def test_continue_with_waiver_sourced_from_file(self, tmp_path) -> None:
        fb = tmp_path / "waiver.txt"
        fb.write_text("Accepted under operator waiver: docs-only.", encoding="utf-8")
        set_feedback_file_override(str(fb))

        result = prompt_phase_handoff_action(
            _signal(available_actions=_WAIVER_SET),
            stdin=_scripted_stdin("4", ""),
            stdout=_new_stdout(),
        )
        assert result.action == "continue_with_waiver"
        assert result.feedback == "Accepted under operator waiver: docs-only."

    def test_empty_feedback_file_aborts_prompt(self, tmp_path) -> None:
        fb = tmp_path / "empty.txt"
        fb.write_text("\n  \n", encoding="utf-8")
        set_feedback_file_override(str(fb))

        out = _new_stdout()
        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin("2", ""),
            stdout=out,
        )
        assert result is HANDOFF_PROMPT_ABORTED
        assert "is empty" in out.getvalue()

    def test_override_not_used_for_non_feedback_action(self, tmp_path) -> None:
        # ``continue`` requires no feedback; the override must not inject
        # any, and the file is never consulted.
        fb = tmp_path / "verdict.txt"
        fb.write_text("should not be used", encoding="utf-8")
        set_feedback_file_override(str(fb))

        result = prompt_phase_handoff_action(
            _signal(),
            stdin=_scripted_stdin("1", ""),
            stdout=_new_stdout(),
        )
        assert result.action == "continue"
        assert result.feedback is None
