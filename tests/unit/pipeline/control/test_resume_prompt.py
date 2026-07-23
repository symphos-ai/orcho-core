"""Tests for ``pipeline.control.resume_prompt``.

The prompt adapter is pure I/O over injectable ``stdin`` / ``stdout``
streams so the tests can drive it deterministically without touching
real terminals.
"""
from __future__ import annotations

import io

from pipeline.control.resume_context import ResumeIntentOptions, ResumeMode
from pipeline.control.resume_prompt import (
    PromptedResumeIntent,
    prompt_resume_intent,
    should_prompt_for_resume_intent,
)


def _io(
    input_text: str, *, stdout_isatty: bool = False,
) -> tuple[io.StringIO, io.StringIO]:
    stdin = io.StringIO(input_text)
    stdout = io.StringIO()
    stdout.isatty = lambda: stdout_isatty  # type: ignore[method-assign]
    return stdin, stdout


class _SelectableStdin:
    """stdin double exposing a real ``fileno`` so the follow-up-task paste
    drain (``select`` on stdin) is reachable. ``readline`` pops the scripted
    queue; an exhausted queue returns ``""`` (EOF).
    """

    def __init__(self, lines: list[str], fd: int = 0) -> None:
        self.queue = list(lines)
        self._fd = fd

    def fileno(self) -> int:
        return self._fd

    def readline(self) -> str:
        return self.queue.pop(0) if self.queue else ""


class TestPromptResumeIntent:
    def _incomplete_options(self) -> ResumeIntentOptions:
        return ResumeIntentOptions(
            can_checkpoint=True,
            can_followup=True,
            default_mode=ResumeMode.CHECKPOINT,
            parent_status="interrupted",
            reason="incomplete-parent",
        )

    def _terminal_options(self) -> ResumeIntentOptions:
        return ResumeIntentOptions(
            can_checkpoint=False,
            can_followup=True,
            default_mode=ResumeMode.FOLLOWUP,
            parent_status="done",
            reason="terminal-success",
        )

    def _empty_options(self) -> ResumeIntentOptions:
        return ResumeIntentOptions(
            can_checkpoint=False,
            can_followup=False,
            default_mode=None,
            parent_status=None,
            reason="missing-parent-meta",
        )

    def test_incomplete_default_is_checkpoint(self) -> None:
        stdin, stdout = _io("\n")  # empty line → default
        out = prompt_resume_intent(
            run_id="20260514_120000",
            options=self._incomplete_options(),
            stdin=stdin, stdout=stdout,
        )
        assert out == PromptedResumeIntent(mode=ResumeMode.CHECKPOINT)
        rendered = stdout.getvalue()
        assert "20260514_120000" in rendered
        assert "Resume from checkpoint" in rendered
        assert "fresh provider sessions with persisted run context" in rendered
        assert "resume parent provider sessions" in rendered
        # Incomplete parent prefix never claims the run is "done".
        assert "did not finish" in rendered
        assert "interrupted" in rendered  # status surfaced
        assert "is paused" not in rendered

    def test_incomplete_pick_one_is_checkpoint(self) -> None:
        stdin, stdout = _io("1\n")
        out = prompt_resume_intent(
            run_id="r", options=self._incomplete_options(),
            stdin=stdin, stdout=stdout,
        )
        assert out.mode == ResumeMode.CHECKPOINT
        assert out.task is None

    def test_incomplete_followup_prompts_task(self) -> None:
        stdin, stdout = _io("2\nfix the gate\n")
        out = prompt_resume_intent(
            run_id="r", options=self._incomplete_options(),
            stdin=stdin, stdout=stdout,
        )
        assert out.mode == ResumeMode.FOLLOWUP
        assert out.task == "fix the gate"

    def test_incomplete_followup_pasted_block_captured_whole(
        self, monkeypatch,
    ) -> None:
        # Pasting a multi-line block into the follow-up prompt must capture the
        # whole burst (incl. blank separators), not just the first line — and
        # must not leave lines buffered to leak to the shell (the reported bug).
        stdin = _SelectableStdin([
            "2\n",                    # choice: start a follow-up
            "Final acceptance\n",     # first follow-up line (via readline)
            "verdict REJECTED\n",     # …drained burst continues…
            "\n",
            "summary not ready\n",
        ])
        stdout = io.StringIO()
        stdout.isatty = lambda: False  # type: ignore[method-assign]
        # During the burst the continuation lines are "ready"; then drained.
        readies = iter([True, True, True, False])
        monkeypatch.setattr(
            "core.io.terminal_input.select.select",
            lambda r, w, x, timeout: (list(r) if next(readies, False) else [], [], []),
        )
        out = prompt_resume_intent(
            run_id="r", options=self._incomplete_options(),
            stdin=stdin, stdout=stdout,
        )
        assert out.mode == ResumeMode.FOLLOWUP
        assert out.task == "Final acceptance\nverdict REJECTED\n\nsummary not ready"
        # Whole burst consumed → nothing left to leak to the shell.
        assert stdin.queue == []

    def test_incomplete_exit_returns_none(self) -> None:
        stdin, stdout = _io("3\n")
        out = prompt_resume_intent(
            run_id="r", options=self._incomplete_options(),
            stdin=stdin, stdout=stdout,
        )
        assert out.mode is None

    def test_blocked_checkpoint_is_not_offered(self) -> None:
        options = ResumeIntentOptions(
            can_checkpoint=False,
            can_followup=True,
            default_mode=ResumeMode.FOLLOWUP,
            parent_status="failed",
            reason="incomplete-parent",
            checkpoint_blocked_reason="loop cursor is corrupt",
        )
        stdin, stdout = _io("2\n")

        out = prompt_resume_intent(
            run_id="r",
            options=options,
            stdin=stdin,
            stdout=stdout,
        )

        assert out.mode is None
        rendered = stdout.getvalue()
        assert "Checkpoint resume is unavailable" in rendered
        assert "loop cursor is corrupt" in rendered
        assert "Resume from checkpoint" not in rendered

    def test_incomplete_eof_returns_none(self) -> None:
        stdin, stdout = _io("")  # immediate EOF
        out = prompt_resume_intent(
            run_id="r", options=self._incomplete_options(),
            stdin=stdin, stdout=stdout,
        )
        assert out.mode is None

    def test_invalid_choice_reprompts(self) -> None:
        stdin, stdout = _io("9\n1\n")
        out = prompt_resume_intent(
            run_id="r", options=self._incomplete_options(),
            stdin=stdin, stdout=stdout,
        )
        assert out.mode == ResumeMode.CHECKPOINT
        assert "answer one of" in stdout.getvalue()

    def test_terminal_default_is_followup(self) -> None:
        stdin, stdout = _io("\nrefine error message\n")
        out = prompt_resume_intent(
            run_id="r", options=self._terminal_options(),
            stdin=stdin, stdout=stdout,
        )
        assert out.mode == ResumeMode.FOLLOWUP
        assert out.task == "refine error message"
        rendered = stdout.getvalue()
        # Single, non-contradictory headline — no "paused", no duplicate
        # "This run is already done." line.
        assert "has already completed" in rendered
        assert "resume parent provider sessions" in rendered
        assert "is paused" not in rendered
        assert rendered.count("already") == 1

    def test_tty_render_highlights_decisions_and_dims_help(self, monkeypatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        stdin, stdout = _io("2\n", stdout_isatty=True)
        out = prompt_resume_intent(
            run_id="r", options=self._terminal_options(),
            stdin=stdin, stdout=stdout,
        )

        assert out.mode is None
        rendered = stdout.getvalue()
        assert "\033[1mWhat do you want to do?\033[0m" in rendered
        assert "\033[1m1) Start a follow-up using this run as context\033[0m" in rendered
        assert "\033[92m\033[1m[default]\033[0m" in rendered
        assert (
            "\033[90m     Start a new run and resume parent provider sessions when "
            "\033[0m"
        ) in rendered
        assert "\033[1mChoice [1/2]: \033[0m" in rendered

    def test_no_color_disables_tty_highlighting(self, monkeypatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        stdin, stdout = _io("2\n", stdout_isatty=True)
        out = prompt_resume_intent(
            run_id="r", options=self._terminal_options(),
            stdin=stdin, stdout=stdout,
        )

        assert out.mode is None
        rendered = stdout.getvalue()
        assert "\033[" not in rendered
        assert "What do you want to do?" in rendered
        assert "     Start a new run and resume parent provider sessions" in rendered

    def test_terminal_exit_returns_none(self) -> None:
        stdin, stdout = _io("2\n")
        out = prompt_resume_intent(
            run_id="r", options=self._terminal_options(),
            stdin=stdin, stdout=stdout,
        )
        assert out.mode is None

    def test_terminal_empty_task_after_retry_returns_none(self) -> None:
        # Choose follow-up (default), then submit two empty task lines —
        # the loop should give up cleanly rather than spin or accept "".
        stdin, stdout = _io("\n\n\n")
        out = prompt_resume_intent(
            run_id="r", options=self._terminal_options(),
            stdin=stdin, stdout=stdout,
        )
        assert out.mode is None

    def test_no_options_returns_none(self) -> None:
        stdin, stdout = _io("")
        out = prompt_resume_intent(
            run_id="r", options=self._empty_options(),
            stdin=stdin, stdout=stdout,
        )
        assert out.mode is None
        assert "Nothing to resume" in stdout.getvalue()


class TestShouldPromptForResumeIntent:
    def _tty_stdin(self, *, isatty: bool) -> io.StringIO:
        s = io.StringIO("")
        s.isatty = lambda: isatty  # type: ignore[method-assign]
        return s

    def test_no_resume_skips(self) -> None:
        assert not should_prompt_for_resume_intent(
            resume=None, explicit_task=None, explicit_task_file=None,
            no_interactive=False, stdin=self._tty_stdin(isatty=True),
        )

    def test_explicit_task_skips(self) -> None:
        assert not should_prompt_for_resume_intent(
            resume="r", explicit_task="X", explicit_task_file=None,
            no_interactive=False, stdin=self._tty_stdin(isatty=True),
        )

    def test_explicit_task_file_skips(self) -> None:
        assert not should_prompt_for_resume_intent(
            resume="r", explicit_task=None, explicit_task_file="task.md",
            no_interactive=False, stdin=self._tty_stdin(isatty=True),
        )

    def test_no_interactive_skips(self) -> None:
        assert not should_prompt_for_resume_intent(
            resume="r", explicit_task=None, explicit_task_file=None,
            no_interactive=True, stdin=self._tty_stdin(isatty=True),
        )

    def test_non_tty_skips(self) -> None:
        assert not should_prompt_for_resume_intent(
            resume="r", explicit_task=None, explicit_task_file=None,
            no_interactive=False, stdin=self._tty_stdin(isatty=False),
        )

    def test_resume_tty_no_task_prompts(self) -> None:
        assert should_prompt_for_resume_intent(
            resume="r", explicit_task=None, explicit_task_file=None,
            no_interactive=False, stdin=self._tty_stdin(isatty=True),
        )
